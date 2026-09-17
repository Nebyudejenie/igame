"""Tests for services/admin/queries.py: every mutation must move money
through the ledger (never a direct balance write) and leave an audit trail.
"""

import asyncio
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from packages.core import ledger, responsible_gaming
from packages.core.phone_crypto import encrypt_phone, phone_lookup_hash
from services.admin import queries
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import (
    create_funded_user,
    create_room,
    create_user,
    next_telegram_id,
    unique_phone,
)
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_round_engine import wait_until


async def _create_user_with_phone(conn, phone: str, display_name: str = "Findable Person"):
    return await conn.fetchrow(
        "INSERT INTO users (telegram_id, display_name, phone_e164_encrypted, phone_lookup_hash) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        next_telegram_id(),
        display_name,
        encrypt_phone(phone),
        phone_lookup_hash(phone),
    )


async def test_search_users_finds_by_exact_phone_in_national_format(pool, conn):
    # phone[-9:] is the bare 9-digit national number (no country code) --
    # a different accepted *format* of the same complete number
    # (normalize_ethiopian_phone() accepts both), not a true substring
    # fragment. Phone search is exact-match only once numbers are
    # encrypted (see services/admin/queries.py's search_users) -- this
    # still matches because it's the whole number, just spelled
    # differently, and normalization makes both forms hash identically.
    phone = unique_phone()
    row = await _create_user_with_phone(conn, phone)
    results = await queries.search_users(pool, phone[-9:])
    assert any(r["id"] == row["id"] for r in results)
    match = next(r for r in results if r["id"] == row["id"])
    assert match["phone_e164"] == phone


async def test_search_users_does_not_match_a_genuine_partial_phone(pool, conn):
    # A real substring of the number (missing digits, not just a
    # differently-formatted whole number) must not match -- this is
    # exactly the capability lost by encrypting phone numbers at rest,
    # confirmed as an acceptable tradeoff with the user (see DECISIONS.md).
    phone = unique_phone()
    row = await _create_user_with_phone(conn, phone, display_name="Not Findable By Fragment")
    partial = phone[-5:]  # genuinely incomplete -- not a valid national number on its own
    results = await queries.search_users(pool, partial)
    assert not any(r["id"] == row["id"] for r in results)


async def test_get_user_detail_includes_real_balances(pool, conn):
    user_id = await create_funded_user(conn, Decimal("55.00"))
    detail = await queries.get_user_detail(pool, user_id)
    assert detail is not None
    assert detail["balances"]["cash"] == "55.00"


async def test_get_user_detail_returns_none_for_unknown_user(pool):
    assert await queries.get_user_detail(pool, 999_999_999) is None


async def _record_payment(conn, user_id: int, *, direction: str, amount: Decimal) -> None:
    await conn.execute(
        """
        INSERT INTO payments (user_id, direction, provider, our_ref, amount, status)
        VALUES ($1, $2, 'chapa', $3, $4, 'succeeded')
        """,
        user_id,
        direction,
        f"TEST-LTV-{next_telegram_id()}",
        amount,
    )


async def test_get_user_detail_includes_ltv(pool, conn):
    user_id = await create_funded_user(conn, Decimal("0.00"))
    await _record_payment(conn, user_id, direction="in", amount=Decimal("500.00"))
    await _record_payment(conn, user_id, direction="in", amount=Decimal("200.00"))
    await _record_payment(conn, user_id, direction="out", amount=Decimal("150.00"))

    detail = await queries.get_user_detail(pool, user_id)
    assert detail is not None
    assert detail["ltv"]["total_deposited"] == "700.00"
    assert detail["ltv"]["total_withdrawn"] == "150.00"
    assert detail["ltv"]["net_ltv"] == "550.00"


async def test_top_players_by_ltv_ranks_by_net_contribution(pool, conn):
    # This session's shared dev database accumulates real payment rows
    # across every test run, so a small limit isn't reliably big enough to
    # contain both test users -- a real cross-test-pollution risk this
    # session has hit before (reconcile_job's idempotency keys, for one).
    # A limit far larger than any realistic accumulated row count still
    # proves real DESC ordering; it just doesn't assume either user lands
    # in an arbitrarily small "top N" slice.
    high_value_user = await create_funded_user(conn, Decimal("0.00"))
    await _record_payment(conn, high_value_user, direction="in", amount=Decimal("10000.00"))

    low_value_user = await create_funded_user(conn, Decimal("0.00"))
    await _record_payment(conn, low_value_user, direction="in", amount=Decimal("50.00"))

    results = await queries.top_players_by_ltv(pool, limit=1_000_000)
    ids_in_order = [r["user_id"] for r in results]
    assert ids_in_order.index(high_value_user) < ids_in_order.index(low_value_user)


async def test_retention_cohorts_counts_a_user_active_in_their_signup_week(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    cohorts = await queries.retention_cohorts(pool, weeks=4)
    # date_trunc('week', ...) starts on Monday, not necessarily "today" --
    # find the one cohort that actually contains p1, rather than assuming
    # which calendar date that lands on.
    #
    # This shared, long-lived dev database was found (during an unrelated
    # verification pass) to hold real historical users/rounds from
    # earlier in this same session's own testing -- specifically an
    # already-fully-elapsed prior week's cohort that *also* shows week-0
    # activity (naturally: those users were active during their own,
    # now-past, signup week too). matching[0] used to just take
    # cohorts[0], which sorts oldest-cohort-first and silently picked
    # that old, already-elapsed cohort instead of p1's own brand-new one
    # -- a real, reproducible (not flaky) failure once enough historical
    # data had accumulated, caught by this same launch-readiness pass's
    # own required full-suite regression run. The row this test actually
    # means to check is the *newest* matching cohort -- p1 and p2 were
    # both just created moments ago, so their cohort_week cannot be
    # anything but the most recent one.
    matching = [c for c in cohorts if c["weeks"][0]["active_users"] >= 1]
    assert matching, f"no cohort showed any week-0 activity: {cohorts}"
    matching = [max(matching, key=lambda c: c["cohort_week"])]
    # This user's own signup week is still in progress (it only just
    # started) -- week 0 is real, honest activity-so-far, but not yet a
    # completed rate: more of this cohort could still become active
    # before the week ends. elapsed=False / retention_rate=None is the
    # fix for a real bug a code review pass caught (a still-in-progress
    # week was previously indistinguishable from 0% churn).
    assert matching[0]["weeks"][0]["elapsed"] is False
    assert matching[0]["weeks"][0]["retention_rate"] is None


async def test_retention_cohorts_buckets_signup_week_using_ethiopia_time_not_utc(pool, conn):
    # A code review pass caught date_trunc('week', created_at)::date --
    # created_at is timestamptz, so a bare date_trunc with no AT TIME ZONE
    # truncates using Postgres's ambient session timezone (unconfigured,
    # defaults to UTC), the same already-fixed bug DECISIONS.md documents
    # for dashboard_summary/daily_ggr's own day-bucketing, just via
    # date_trunc('week', ...) instead of a bare ::date cast. A signup at
    # 2024-03-10 22:00 UTC is a Sunday in UTC (still the week starting
    # Monday 2024-03-04) but already Monday 01:00 in Ethiopia (UTC+3) --
    # the start of the *next* week, 2024-03-11. Confirmed directly against
    # this project's own Postgres, not assumed: date_trunc('week', ...)
    # on this instant gives 2024-03-04 without the fix, 2024-03-11 with
    # it. A date safely in the past (nowhere near this session's own
    # "now") so no unrelated test's create_funded_user() ever lands in
    # either of these same two week buckets and pollutes the count.
    user_id = await create_funded_user(conn)
    await conn.execute(
        "UPDATE users SET created_at = $2 WHERE id = $1",
        user_id,
        datetime(2024, 3, 10, 22, 0, 0, tzinfo=UTC),
    )

    cohorts = await queries.retention_cohorts(pool, weeks=1)
    weeks_present = {c["cohort_week"] for c in cohorts}
    assert "2024-03-11" in weeks_present, (
        f"expected the Ethiopia-time Monday (2024-03-11), got cohort weeks {weeks_present}"
    )
    assert "2024-03-04" not in weeks_present, (
        "user was bucketed into the UTC week (2024-03-04) instead of the Ethiopia week (2024-03-11)"
    )


async def test_retention_cohorts_places_a_backdated_signup_in_a_later_week_offset(pool, redis, card_pool, conn):
    # Signed up 2 weeks before today, active today -- must land at
    # week_offset 2 in their own cohort's row, not week_offset 0. Exactly
    # 14 days, not e.g. 15 -- date_trunc('week', ...) is Monday-anchored,
    # so only a multiple of 7 days reliably shifts the truncated week by a
    # fixed, predictable number of weeks regardless of which day "now"
    # itself falls on (confirmed directly: 15 days landed 3 weeks back on
    # a day this was first run, not 2, because "now" was a Monday).
    user_id = await create_funded_user(conn)
    await conn.execute(
        "UPDATE users SET created_at = now() - interval '14 days' WHERE id = $1", user_id
    )

    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        other = await create_funded_user(conn)
        assert (await engine.join(user_id, 1)).ok
        assert (await engine.join(other, 2)).ok
        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    # Mirrors retention_cohorts()'s own AT TIME ZONE 'Africa/Addis_Ababa'
    # bucketing -- this used to be a bare date_trunc('week', created_at),
    # which happened to still agree with the fixed function on most days
    # but would silently mismatch (and flake this test) whenever "now"
    # fell near a UTC/Ethiopia week-boundary crossing.
    cohort_week = await conn.fetchval(
        "SELECT date_trunc('week', created_at AT TIME ZONE 'Africa/Addis_Ababa')::date "
        "FROM users WHERE id = $1",
        user_id,
    )
    cohorts = await queries.retention_cohorts(pool, weeks=4)
    cohort = next(c for c in cohorts if c["cohort_week"] == cohort_week.isoformat())
    assert cohort["weeks"][2]["active_users"] >= 1
    assert cohort["weeks"][0]["active_users"] == 0
    # This cohort's own signup week (week_offset 0) is fully in the past
    # by now (signed up 14 days ago) -- a real completed comparison, so
    # the fix's elapsed flag must say so and give a real numeric rate,
    # unlike the still-in-progress week 0 covered by the test above.
    assert cohort["weeks"][0]["elapsed"] is True
    assert cohort["weeks"][0]["retention_rate"] == 0.0


async def test_adjust_balance_credits_via_ledger_and_writes_audit_log(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("10.00"))

    txn_id = await queries.adjust_balance(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        amount=Decimal("25.00"),
        reason="goodwill credit for a support ticket",
        ip_address="127.0.0.1",
        request_id=str(uuid.uuid4()),
    )
    assert txn_id

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("35.00")

    txn_row = await conn.fetchrow(
        "SELECT kind, memo, created_by FROM ledger_transactions WHERE id = $1", txn_id
    )
    assert txn_row["kind"] == "adjustment"
    assert txn_row["created_by"] == f"admin:{admin_id}"

    audit_row = await conn.fetchrow(
        "SELECT action, reason, before, after FROM admin_audit_log "
        "WHERE admin_id = $1 AND target_id = $2 ORDER BY id DESC LIMIT 1",
        admin_id,
        str(user_id),
    )
    assert audit_row["action"] == "users.adjust_balance"
    assert "goodwill" in audit_row["reason"]

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []


async def test_adjust_balance_debit_cannot_overdraw(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("10.00"))

    with pytest.raises(ledger.InsufficientFunds):
        await queries.adjust_balance(
            pool,
            admin_id=admin_id,
            user_id=user_id,
            amount=Decimal("-50.00"),
            reason="correcting a duplicate credit",
            ip_address=None,
            request_id=str(uuid.uuid4()),
        )

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")


async def test_adjust_balance_is_idempotent_on_a_repeated_request_id(pool, conn):
    # An architecture audit caught that the idempotency key used to be
    # built from admin_id/user_id/datetime.now() -- unique on every call
    # by construction, so a double-click or a retried request created two
    # separate real-money ledger transactions instead of being recognized
    # as the same one. request_id (one per "Apply" click, generated
    # client-side) is what closes that: calling with the same request_id
    # twice must credit exactly once, not twice, and must return the same
    # ledger_transaction_id both times rather than raising or creating a
    # second row.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("10.00"))
    request_id = str(uuid.uuid4())

    first_txn_id = await queries.adjust_balance(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        amount=Decimal("25.00"),
        reason="goodwill credit for a support ticket",
        ip_address="127.0.0.1",
        request_id=request_id,
    )
    second_txn_id = await queries.adjust_balance(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        amount=Decimal("25.00"),
        reason="goodwill credit for a support ticket",
        ip_address="127.0.0.1",
        request_id=request_id,
    )

    assert second_txn_id == first_txn_id

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("35.00")  # credited once, not twice

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE created_by = $1", f"admin:{admin_id}"
    )
    assert txn_count == 1

    audit_count = await conn.fetchval(
        "SELECT count(*) FROM admin_audit_log WHERE admin_id = $1 AND action = 'users.adjust_balance'",
        admin_id,
    )
    assert audit_count == 1  # the replay must not also duplicate the audit trail


async def test_set_user_status_writes_audit_log_with_before_and_after(pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn)

    await queries.set_user_status(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        status="banned",
        reason="fraud investigation",
        ip_address="127.0.0.1",
    )

    status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "banned"

    audit_row = await conn.fetchrow(
        "SELECT before, after FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(user_id),
    )
    # asyncpg returns jsonb columns as raw JSON text by default (no type
    # codec registered) -- same as every other jsonb read in this codebase.
    before = json.loads(audit_row["before"])
    after = json.loads(audit_row["after"])
    assert before["status"] == "active"
    assert after["status"] == "banned"


async def test_set_user_status_refuses_to_directly_set_self_excluded(pool, conn):
    # A real bug a code review pass caught: admins setting status =
    # "self_excluded" directly through this generic endpoint would produce
    # a broken half-exclusion (the users.status flag set, but none of
    # responsible_gaming.self_exclude()'s own bookkeeping --
    # self_excluded_until, the 180-day minimum -- ever applied). Real
    # self-exclusion must only ever go through that function.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn)

    with pytest.raises(queries.InvalidStatusTransition):
        await queries.set_user_status(
            pool,
            admin_id=admin_id,
            user_id=user_id,
            status="self_excluded",
            reason="attempting to self-exclude via the generic endpoint",
            ip_address="127.0.0.1",
        )
    status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "active"


async def test_set_user_status_refuses_to_reverse_a_real_self_exclusion(pool, conn):
    # The actual bug: self-exclusion (packages.core.responsible_gaming
    # .self_exclude()) is deliberately, permanently irreversible for its
    # duration -- "there is deliberately no 'lift my own self-exclusion'
    # function anywhere in this codebase" per that module's own docstring.
    # This generic status endpoint was exactly such a function in
    # disguise: any ops/finance admin holding users:suspend (not just
    # superadmin) could previously call this with status="active" and
    # silently undo a legally-mandated exclusion. Confirmed as a real,
    # reachable path before this fix -- not a hypothetical.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn)
    await responsible_gaming.self_exclude(pool, user_id)

    for attempted_status in ("active", "limited", "banned"):
        with pytest.raises(queries.InvalidStatusTransition):
            await queries.set_user_status(
                pool,
                admin_id=admin_id,
                user_id=user_id,
                status=attempted_status,
                reason="trying to reverse a self-exclusion",
                ip_address="127.0.0.1",
            )
        status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
        assert status == "self_excluded"


async def test_set_kyc_level_writes_audit_log_with_before_and_after(pool, conn):
    # A code review pass caught that users.kyc_level had a real consumer
    # (withdrawals.py's own threshold gate) but no writer anywhere in the
    # codebase at all -- this is that writer, the manual half of KYC
    # verification (see set_kyc_level()'s own docstring for what stays a
    # real, separate, not-yet-made product decision: the actual document
    # -collection method).
    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn)

    await queries.set_kyc_level(
        pool,
        admin_id=admin_id,
        user_id=user_id,
        kyc_level=2,
        reason="ID documents reviewed and verified",
        ip_address="127.0.0.1",
    )

    kyc_level = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert kyc_level == 2

    audit_row = await conn.fetchrow(
        "SELECT before, after FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(user_id),
    )
    before = json.loads(audit_row["before"])
    after = json.loads(audit_row["after"])
    assert before["kyc_level"] == 0
    assert after["kyc_level"] == 2


async def test_set_kyc_level_can_also_revoke_a_previously_granted_level(pool, conn):
    # Promotions and demotions go through the same accountable path -- a
    # level can be revoked (fraud discovered, documents later found
    # invalid) exactly the same way it was granted, not just a one-way
    # ratchet.
    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn)
    await queries.set_kyc_level(
        pool, admin_id=admin_id, user_id=user_id, kyc_level=2,
        reason="initial verification", ip_address="127.0.0.1",
    )

    await queries.set_kyc_level(
        pool, admin_id=admin_id, user_id=user_id, kyc_level=0,
        reason="documents found to be fraudulent", ip_address="127.0.0.1",
    )

    kyc_level = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert kyc_level == 0


async def test_set_kyc_level_rejects_an_out_of_range_level(pool, conn):
    admin_id, *_ = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn)

    with pytest.raises(queries.InvalidKycLevel):
        await queries.set_kyc_level(
            pool, admin_id=admin_id, user_id=user_id, kyc_level=3,
            reason="not a real level", ip_address="127.0.0.1",
        )

    kyc_level = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert kyc_level == 0


async def test_shared_payout_account_clusters_finds_users_sharing_a_payout_destination(pool, conn):
    # spec 8.4: "Same payout account across multiple accounts -> Link
    # accounts, flag cluster." Risk screen data, built entirely from
    # payment_methods -- no new instrumentation needed, unlike the
    # device-fingerprint half of the same spec rule (see
    # shared_payout_account_clusters()'s own docstring).
    shared_ref = f"09{next_telegram_id()}"
    user_a = await create_user(conn)
    user_b = await create_user(conn)
    solo_user = await create_user(conn)

    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) VALUES "
        "($1, 'telebirr', $3, 'Holder A'), ($2, 'telebirr', $3, 'Holder B')",
        user_a,
        user_b,
        shared_ref,
    )
    await conn.execute(
        "INSERT INTO payment_methods (user_id, kind, account_ref, holder_name) VALUES ($1, 'telebirr', $2, 'Solo Holder')",
        solo_user,
        f"09{next_telegram_id()}",
    )

    clusters = await queries.shared_payout_account_clusters(pool)
    match = next((c for c in clusters if c["account_ref"] == shared_ref), None)
    assert match is not None
    assert match["user_count"] == 2
    assert {u["user_id"] for u in match["users"]} == {user_a, user_b}
    assert all(c["user_count"] > 1 for c in clusters)  # solo_user's ref never appears


async def test_repeat_room_pairings_flags_a_lopsided_recurring_pair(pool, conn, card_pool):
    # spec 8.4: "Winner and loser in the same room repeatedly, same pairs
    # -> Collusion investigation." Three rounds where the same two users
    # always play together and the same one always wins should surface as
    # a pairing; a single shared round with a third user shouldn't clear
    # the min_shared_rounds threshold at all.
    room_id = await create_room(conn)
    winner_id = await create_user(conn)
    loser_id = await create_user(conn)
    other_id = await create_user(conn)

    async def _make_shared_round(seq: int, card_a: int, card_b: int, *, record_win: bool) -> None:
        row = await conn.fetchrow(
            "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash) "
            "VALUES ($1, $2, 'done', 20.00, 2000, 'test-hash') RETURNING id",
            room_id,
            seq,
        )
        round_id = row["id"]
        await conn.execute(
            "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, $2, $3), ($1, $4, $5)",
            round_id,
            card_a,
            winner_id,
            card_b,
            loser_id,
        )
        if record_win:
            await conn.execute(
                "INSERT INTO round_winners (round_id, user_id, card_no, pattern, won_on_call, amount) "
                "VALUES ($1, $2, $3, 'row', 10, 32.00)",
                round_id,
                winner_id,
                card_a,
            )

    for seq, (card_a, card_b) in enumerate([(1, 2), (3, 4), (5, 6)], start=1):
        await _make_shared_round(seq, card_a, card_b, record_win=True)

    single_round = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash) "
        "VALUES ($1, 4, 'done', 20.00, 2000, 'test-hash') RETURNING id",
        room_id,
    )
    await conn.execute(
        "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, 7, $2), ($1, 8, $3)",
        single_round["id"],
        winner_id,
        other_id,
    )

    pairings = await queries.repeat_room_pairings(pool, min_shared_rounds=3, since_days=30)
    matches = [p for p in pairings if {p["user_a"], p["user_b"]} == {winner_id, loser_id}]
    assert len(matches) == 1
    pairing = matches[0]
    assert pairing["shared_rounds"] == 3
    winner_wins = pairing["user_a_wins"] if pairing["user_a"] == winner_id else pairing["user_b_wins"]
    loser_wins = pairing["user_a_wins"] if pairing["user_a"] == loser_id else pairing["user_b_wins"]
    assert winner_wins == 3
    assert loser_wins == 0
    assert not any({p["user_a"], p["user_b"]} == {winner_id, other_id} for p in pairings)


async def test_repeat_room_pairings_does_not_inflate_shared_rounds_for_multi_card_players(
    pool, conn, card_pool
):
    # A player can hold several cards in the same round now -- joining
    # round_entries to itself on round_id alone (as the query used to)
    # means two users each holding 3 cards in ONE shared round produce
    # 3*3 = 9 raw pair rows for that round, not 1. Two users sharing
    # exactly one round, each with 3 cards, two of user_a's cards
    # winning, must score shared_rounds == 1 (not 9) and each side's
    # *_wins == 1 (one round won, not two winning card-entries counted
    # as two rounds).
    room_id = await create_room(conn)
    user_a = await create_user(conn)
    user_b = await create_user(conn)

    row = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash) "
        "VALUES ($1, 1, 'done', 20.00, 2000, 'test-hash') RETURNING id",
        room_id,
    )
    round_id = row["id"]
    await conn.execute(
        """
        INSERT INTO round_entries (round_id, card_no, user_id) VALUES
            ($1, 1, $2), ($1, 2, $2), ($1, 3, $2),
            ($1, 4, $3), ($1, 5, $3), ($1, 6, $3)
        """,
        round_id,
        user_a,
        user_b,
    )
    await conn.execute(
        "INSERT INTO round_winners (round_id, user_id, card_no, pattern, won_on_call, amount) "
        "VALUES ($1, $2, 1, 'row', 10, 16.00), ($1, $2, 2, 'col', 12, 16.00)",
        round_id,
        user_a,
    )

    pairings = await queries.repeat_room_pairings(pool, min_shared_rounds=1, since_days=30)
    matches = [p for p in pairings if {p["user_a"], p["user_b"]} == {user_a, user_b}]
    assert len(matches) == 1, matches
    pairing = matches[0]
    assert pairing["shared_rounds"] == 1, pairing
    a_wins = pairing["user_a_wins"] if pairing["user_a"] == user_a else pairing["user_b_wins"]
    b_wins = pairing["user_a_wins"] if pairing["user_a"] == user_b else pairing["user_b_wins"]
    assert a_wins == 1, "two winning cards in one round must count as one round won, not two"
    assert b_wins == 0


async def test_void_round_admin_refunds_and_is_idempotent(pool, redis, card_pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await create_room(conn, stake=Decimal("15.00"), min_players=5, lobby_seconds=1)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        # Lobby will time out and void itself (only 1 of 5 min_players) --
        # but exercise the admin void path directly and immediately instead,
        # which is the realistic "something's stuck, an ops admin steps in"
        # scenario, and confirm it's a safe no-op once already terminal.
        refunded_first = await queries.void_round_admin(
            pool, admin_id=admin_id, round_id=round_id, reason="stuck room", ip_address="10.0.0.1"
        )
        assert refunded_first is True

        cash = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash.id) == Decimal("50.00")

        refunded_second = await queries.void_round_admin(
            pool, admin_id=admin_id, round_id=round_id, reason="retry", ip_address="10.0.0.1"
        )
        assert refunded_second is False  # already terminal, no double refund
        assert await ledger.balance(conn, cash.id) == Decimal("50.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_round_fairness_verification_matches_a_real_round(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        fairness = await queries.get_round_fairness(pool, round_row["id"])
        assert fairness is not None
        assert fairness["revealed"] is True
        assert fairness["verified"] is True
        assert len(fairness["draw_order"]) == 75
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_fairness_not_revealed_before_round_finishes(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=5)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        fairness = await queries.get_round_fairness(pool, round_id)
        assert fairness is not None
        assert fairness["revealed"] is False
        assert "server_seed_hash" in fairness
        assert "server_seed" not in fairness
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_create_and_update_room_admin_writes_audit_log(pool):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("50.00"),
        house_cut_bps=1500,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )

    row = await pool.fetchrow("SELECT stake, house_cut_bps FROM rooms WHERE id = $1", room_id)
    assert row["stake"] == Decimal("50.00")
    assert row["house_cut_bps"] == 1500

    updated = await queries.update_room_admin(
        pool,
        admin_id=admin_id,
        room_id=room_id,
        changes={"house_cut_bps": 2500},
        reason="adjusting margin",
        ip_address="127.0.0.1",
    )
    assert updated is True

    row = await pool.fetchrow("SELECT house_cut_bps FROM rooms WHERE id = $1", room_id)
    assert row["house_cut_bps"] == 2500

    audit_rows = await pool.fetch(
        "SELECT action FROM admin_audit_log WHERE target_id = $1 ORDER BY id", str(room_id)
    )
    actions = [r["action"] for r in audit_rows]
    assert "rooms.create" in actions
    assert "rooms.update" in actions


async def test_max_cards_per_player_settable_at_creation_and_via_update(pool):
    # Without this, rooms.max_cards_per_player's DEFAULT 1 (migration
    # deeff3c6228e) can never actually be raised by anyone -- the engine/
    # gateway support for multiple cards per player has no operational
    # path to turn on for a real room otherwise.
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("10.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        max_cards_per_player=4,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )
    row = await pool.fetchrow("SELECT max_cards_per_player FROM rooms WHERE id = $1", room_id)
    assert row["max_cards_per_player"] == 4

    rooms = await queries.list_rooms(pool)
    listed = next(r for r in rooms if r["id"] == room_id)
    assert listed["max_cards_per_player"] == 4

    updated = await queries.update_room_admin(
        pool,
        admin_id=admin_id,
        room_id=room_id,
        changes={"max_cards_per_player": 2},
        reason="lowering the cap",
        ip_address="127.0.0.1",
    )
    assert updated is True
    row = await pool.fetchrow("SELECT max_cards_per_player FROM rooms WHERE id = $1", room_id)
    assert row["max_cards_per_player"] == 2


async def test_min_winning_lines_settable_at_creation_and_via_update(pool):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("10.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row", "col", "diag"],
        min_winning_lines=3,
        ip_address="127.0.0.1",
    )
    row = await pool.fetchrow("SELECT min_winning_lines FROM rooms WHERE id = $1", room_id)
    assert row["min_winning_lines"] == 3

    rooms = await queries.list_rooms(pool)
    listed = next(r for r in rooms if r["id"] == room_id)
    assert listed["min_winning_lines"] == 3

    updated = await queries.update_room_admin(
        pool,
        admin_id=admin_id,
        room_id=room_id,
        changes={"min_winning_lines": 1},
        reason="switching this room to the classic single-line game",
        ip_address="127.0.0.1",
    )
    assert updated is True
    row = await pool.fetchrow("SELECT min_winning_lines FROM rooms WHERE id = $1", room_id)
    assert row["min_winning_lines"] == 1


async def test_create_room_admin_defaults_min_winning_lines_to_two(pool):
    # The product default this whole feature is built around -- a caller
    # (like the admin console's own create-room form, before an operator
    # touches the new field) that doesn't specify it must get the exact
    # same rule every room has always run under.
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("10.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )
    row = await pool.fetchrow("SELECT min_winning_lines FROM rooms WHERE id = $1", room_id)
    assert row["min_winning_lines"] == 2


@pytest.mark.parametrize("bad_value", [0, -1, 5, 100])
async def test_create_room_admin_rejects_invalid_min_winning_lines(pool, bad_value):
    admin_id, *_ = await create_test_admin(pool)
    with pytest.raises(ValueError, match="min_winning_lines"):
        await queries.create_room_admin(
            pool,
            admin_id=admin_id,
            code=f"admin-test-{room_id_suffix()}",
            stake=Decimal("10.00"),
            house_cut_bps=2000,
            min_players=2,
            max_players=100,
            lobby_seconds=30,
            call_interval_ms=4000,
            result_seconds=10,
            win_patterns=["row"],
            min_winning_lines=bad_value,
            ip_address="127.0.0.1",
        )


@pytest.mark.parametrize("bad_value", [0, -1, 5])
async def test_update_room_admin_rejects_invalid_min_winning_lines(pool, bad_value):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("10.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )
    with pytest.raises(ValueError, match="min_winning_lines"):
        await queries.update_room_admin(
            pool,
            admin_id=admin_id,
            room_id=room_id,
            changes={"min_winning_lines": bad_value},
            reason="trying an invalid value",
            ip_address="127.0.0.1",
        )
    # Rejected before ever reaching the database -- the room's real value
    # must be completely untouched, not partially applied.
    row = await pool.fetchrow("SELECT min_winning_lines FROM rooms WHERE id = $1", room_id)
    assert row["min_winning_lines"] == 2


async def test_update_room_rejects_unknown_field(pool):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("20.00"),
        house_cut_bps=2000,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address=None,
    )
    with pytest.raises(ValueError):
        await queries.update_room_admin(
            pool,
            admin_id=admin_id,
            room_id=room_id,
            changes={"code": "sneaky rename"},
            reason=None,
            ip_address=None,
        )


async def test_room_config_edit_does_not_affect_an_in_flight_round(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=5, lobby_seconds=5
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        admin_id, *_ = await create_test_admin(pool)
        await queries.update_room_admin(
            pool,
            admin_id=admin_id,
            room_id=room_id,
            changes={"house_cut_bps": 9000, "stake": "999.00"},
            reason="test: must not retroactively affect the live round",
            ip_address=None,
        )

        round_row = await pool.fetchrow(
            "SELECT stake, house_cut_bps FROM rounds WHERE id = $1", round_id
        )
        assert round_row["stake"] == Decimal("20.00")
        assert round_row["house_cut_bps"] == 2000
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_update_room_admin_audit_log_stores_win_patterns_as_a_real_array(pool, conn):
    # A code review pass caught that update_room_admin()'s audit before/
    # after values ran every changed field through a blanket str(...),
    # including win_patterns -- asyncpg returns a jsonb column as a raw
    # JSON string with no codec registered (same reason list_rooms() does
    # its own isinstance(..., str) + json.loads()), so str()-ing it was a
    # no-op that left a JSON string sitting as a dict value. audit.record
    # ()'s own json.dumps(before) then double-encoded that into an
    # escaped string inside the stored audit row -- readable only after
    # decoding twice, unlike every other field the audit log records.
    admin_id, *_ = await create_test_admin(pool)
    room_id = await queries.create_room_admin(
        pool,
        admin_id=admin_id,
        code=f"admin-test-{room_id_suffix()}",
        stake=Decimal("50.00"),
        house_cut_bps=1500,
        min_players=2,
        max_players=100,
        lobby_seconds=30,
        call_interval_ms=4000,
        result_seconds=10,
        win_patterns=["row"],
        ip_address="127.0.0.1",
    )

    updated = await queries.update_room_admin(
        pool,
        admin_id=admin_id,
        room_id=room_id,
        changes={"win_patterns": ["row", "column", "diagonal"]},
        reason="enabling more win patterns",
        ip_address="127.0.0.1",
    )
    assert updated is True

    audit_row = await conn.fetchrow(
        "SELECT before, after FROM admin_audit_log "
        "WHERE target_id = $1 AND action = 'rooms.update' ORDER BY id DESC LIMIT 1",
        str(room_id),
    )
    before = json.loads(audit_row["before"])
    after = json.loads(audit_row["after"])
    # A single json.loads() of the whole audit row must already produce a
    # real list for win_patterns -- if this were still a string (the
    # double-encoded bug), an admin (or this assertion) would need to
    # decode it a second time to get anything useful out of it.
    assert before["win_patterns"] == ["row"]
    assert after["win_patterns"] == ["row", "column", "diagonal"]


async def test_dashboard_summary_reflects_real_state(pool, redis, card_pool, conn):
    # is_active=True: dashboard_summary()'s own active_rooms count reads
    # WHERE is_active = true, unlike every other test using create_room()
    # (see its own docstring for why False is the right default there).
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=5, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        # stakes_today/payouts_today/house_revenue_today read from this
        # session's shared, ever-growing ledger_entries table -- a
        # before/after delta, not an absolute total, is what's actually
        # meaningful here, the same discipline this session's other
        # shared-ledger tests already settled on.
        stakes_before = Decimal((await queries.dashboard_summary(pool))["stakes_today"])

        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok

        summary = await queries.dashboard_summary(pool)
        assert summary["active_rounds"] >= 1
        assert summary["active_rooms"] >= 1
        # A code review pass consolidated this from three separate
        # queries into one FILTER-based query -- this confirms the
        # consolidated version still attributes a real stake to the
        # right bucket, not just that it runs without error.
        assert Decimal(summary["stakes_today"]) - stakes_before == Decimal("10.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_dashboard_summary_counts_withdrawals_awaiting_review(pool, conn):
    # spec 6226's Dashboard row: "...alerts" -- an admin needs to see
    # something needs attention without already knowing to click into
    # the Payments screen first. Delta, not an absolute count, for the
    # same reason stakes_today's own test uses one: this reads from the
    # whole shared session's ever-growing payments table.
    before = (await queries.dashboard_summary(pool))["pending_withdrawals_count"]

    user_id = await create_funded_user(conn, Decimal("500.00"))
    await conn.execute(
        "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status) "
        "VALUES ($1, 'out', 'chapa', $2, $3, 'review')",
        user_id,
        f"WD-test-dashboard-alert-{user_id}",
        Decimal("100.00"),
    )

    after = (await queries.dashboard_summary(pool))["pending_withdrawals_count"]
    assert after - before == 1


def test_ethiopia_tz_is_the_real_utc_plus_3_offset():
    # A regression guard against a typo'd IANA zone name silently
    # resolving to the wrong offset (or a future edit picking a DST
    # -observing zone by mistake) -- Ethiopia has never observed DST, so
    # this must hold for any real instant, not just the one this test
    # happens to run at.
    offset = queries.ETHIOPIA_TZ.utcoffset(datetime(2026, 1, 1))
    assert offset is not None
    assert offset.total_seconds() == 3 * 3600


async def test_daily_ggr_attributes_a_near_midnight_utc_entry_to_the_correct_ethiopian_calendar_day(
    pool, conn
):
    # A code review pass caught daily_ggr()/dashboard_summary() casting
    # created_at::date under whatever the Postgres session's own ambient
    # timezone setting happens to be, rather than the Ethiopian calendar
    # day these reports actually describe. 23:30 UTC on Aug 25 is 02:30
    # EAT on Aug 26 -- a real, everyday occurrence (this window exists
    # every single night), not a contrived edge case. A UTC-anchored cast
    # would attribute this to Aug 25; the fix must attribute it to Aug 26.
    # This session's shared test database accumulates real house_revenue
    # activity from every other test that's run against it -- both
    # candidate calendar days can already show nonzero GGR before this
    # test even starts. Snapshotting before/after and checking the delta
    # (not an absolute total) is what makes this robust against that,
    # the same discipline this session's other ambient-noise-prone tests
    # already settled on.
    before_correct_day = Decimal((await queries.daily_ggr(pool, date(2026, 8, 26)))["ggr"])
    before_wrong_day = Decimal((await queries.daily_ggr(pool, date(2026, 8, 25)))["ggr"])

    house = await ledger.get_or_create_account(conn, None, "house_revenue")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    boundary_utc = datetime(2026, 8, 25, 23, 30, 0, tzinfo=UTC)
    # ledger_entries is append-only (migrations/versions/
    # b8e4a1f0c3d7_ledger_append_only.py) -- this test needs an entry
    # that genuinely originates at a specific historical instant, which
    # ledger.post()'s own created_at parameter gives it directly (a fresh
    # INSERT, both legs sharing the identical timestamp) rather than
    # inserting at now() and mutating afterward.
    await ledger.post(
        conn,
        "payout",
        [ledger.Entry(provider.id, Decimal("-42.00")), ledger.Entry(house.id, Decimal("42.00"))],
        idempotency_key=f"tz-boundary-test-{house.id}-{provider.id}-{datetime.now(UTC).timestamp()}",
        created_at=boundary_utc,
    )

    after_correct_day = Decimal((await queries.daily_ggr(pool, date(2026, 8, 26)))["ggr"])
    after_wrong_day = Decimal((await queries.daily_ggr(pool, date(2026, 8, 25)))["ggr"])

    assert after_correct_day - before_correct_day == Decimal("42.00")
    assert after_wrong_day - before_wrong_day == Decimal("0.00")


_suffix_counter = [0]


def room_id_suffix() -> str:
    _suffix_counter[0] += 1
    return f"{next_telegram_id()}-{_suffix_counter[0]}"
