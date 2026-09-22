"""packages/core/keno_tier_automation.py -- promotion (7-day hold),
immediate demotion, and the daily payout circuit breaker (keno.md Part
7.2/7.3). Real database, real ledger postings -- the 7-day hold is
proven by backdating candidate_since directly (there's no reason to
actually sleep 7 real days in a test), everything else by real time.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from packages.core import keno, keno_tier_automation, ledger, metrics
from services.admin import keno_queries
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _close_any_dangling_betting_open_round(pool):
    """Same fixture, same reasoning, as test_keno_tickets.py's own: this
    file also seeds keno_rounds directly and never advances most of them
    to a terminal status, so a prior test's still-open round would
    otherwise collide with the next test's own INSERT
    (ux_keno_rounds_single_betting_open)."""

    async def _close() -> None:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE keno_rounds SET status = 'completed' WHERE status = 'betting_open'")

    await _close()
    yield
    await _close()


async def _make_two_tiers(pool, conn, *, tier1_min=Decimal("0"), tier2_min=Decimal("1000")):
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    tier1 = await keno_queries.create_tier_admin(
        pool, admin_id=admin_id, tier_number=1, min_reserve=tier1_min, max_pick_count=5,
        max_top_multiplier=Decimal("16"), stake_options=[Decimal("10"), Decimal("20"), Decimal("50")],
        max_win_per_ticket=Decimal("800"), max_round_exposure_pct=Decimal("0.10"),
        paytable_profile="low_variance", reason="test tier 1",
    )
    tier2 = await keno_queries.create_tier_admin(
        pool, admin_id=admin_id, tier_number=2, min_reserve=tier2_min, max_pick_count=6,
        max_top_multiplier=Decimal("60"), stake_options=[Decimal("10"), Decimal("20"), Decimal("50")],
        max_win_per_ticket=Decimal("3000"), max_round_exposure_pct=Decimal("0.10"),
        paytable_profile="low_variance", reason="test tier 2",
    )
    await conn.execute(
        "INSERT INTO keno_tier_state (id, current_tier_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1, candidate_tier_id = NULL, candidate_since = NULL",
        tier1["id"],
    )
    return tier1, tier2


async def _fund_reserve(conn, amount: Decimal) -> None:
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -amount), ledger.Entry(reserve.id, amount)],
        idempotency_key=f"test-fund-{uuid.uuid4()}", created_by="test",
    )


async def test_reserve_above_next_tier_starts_a_candidate_not_an_immediate_promotion(pool, conn):
    tier1, tier2 = await _make_two_tiers(pool, conn)
    await _fund_reserve(conn, Decimal("1500"))  # above tier2's 1000 threshold

    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    assert result.changed is False

    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["candidate_tier_id"] == tier2["id"]
    assert state["candidate_since"] is not None
    # Still on tier 1 -- promotion needs the hold to actually elapse.
    assert state["current_tier_id"] == tier1["id"]


async def test_candidate_held_for_7_days_actually_promotes(pool, conn):
    tier1, tier2 = await _make_two_tiers(pool, conn)
    await _fund_reserve(conn, Decimal("1500"))

    await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    # Backdate the candidate window past the 7-day hold -- no reason to
    # actually wait 7 real days to prove this rule.
    await conn.execute(
        "UPDATE keno_tier_state SET candidate_since = now() - interval '8 days' WHERE id = 1"
    )

    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    assert result.changed is True
    assert result.trigger == "automated_promotion"
    assert result.to_tier_number == 2

    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["current_tier_id"] == tier2["id"]
    assert state["candidate_tier_id"] is None
    assert state["candidate_since"] is None

    change = await conn.fetchrow("SELECT * FROM keno_tier_changes ORDER BY id DESC LIMIT 1")
    assert change["trigger"] == "automated_promotion"
    assert change["from_tier_id"] == tier1["id"]
    assert change["to_tier_id"] == tier2["id"]


async def test_candidate_window_resets_if_reserve_dips_back_down(pool, conn):
    tier1, tier2 = await _make_two_tiers(pool, conn)
    await _fund_reserve(conn, Decimal("1500"))
    await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    await conn.execute(
        "UPDATE keno_tier_state SET candidate_since = now() - interval '8 days' WHERE id = 1"
    )

    # Reserve dips back to exactly tier 1's own range before the sweep
    # that would have promoted it -- a real "lucky day" wobble.
    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("500"))
    assert result.changed is False
    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["current_tier_id"] == tier1["id"]
    assert state["candidate_tier_id"] is None  # cleared, not left stale

    # Reserve climbs back above the threshold again -- must restart the
    # full 7-day window, not resume the old (now-cleared) one.
    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    assert result.changed is False
    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["candidate_tier_id"] == tier2["id"]
    assert state["candidate_since"] is not None


async def test_demotion_is_immediate_no_waiting_period(pool, conn):
    tier1, tier2 = await _make_two_tiers(pool, conn)
    await _fund_reserve(conn, Decimal("1500"))
    await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    await conn.execute("UPDATE keno_tier_state SET candidate_since = now() - interval '8 days' WHERE id = 1")
    await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["current_tier_id"] == tier2["id"]  # now on tier 2

    # Reserve craters well below tier 2's own 1000 floor -- must demote
    # on this exact evaluation, no hold period (unlike promotion).
    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("100"))
    assert result.changed is True
    assert result.trigger == "automated_demotion"
    assert result.to_tier_number == 1

    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["current_tier_id"] == tier1["id"]

    change = await conn.fetchrow(
        "SELECT * FROM keno_tier_changes WHERE trigger = 'automated_demotion' ORDER BY id DESC LIMIT 1"
    )
    assert change["from_tier_id"] == tier2["id"]
    assert change["to_tier_id"] == tier1["id"]


async def test_editing_one_tier_never_breaks_evaluation_against_the_others(pool, conn):
    """The real bug this test exists for: version is scoped per
    tier_number independently (create_tier_admin's own version =
    MAX(version) WHERE tier_number = $1) -- editing tier 2 alone bumps
    only tier 2's version, and evaluation must still correctly compare
    against tier 1's own (unbumped, older-version) row rather than only
    "seeing" tiers that happen to share tier 2's new version number."""
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    tier1, tier2 = await _make_two_tiers(pool, conn)

    # Re-edit tier 2 -- this creates a NEW row for tier_number=2 with a
    # higher version, while tier 1's own row is untouched at its
    # original version.
    tier2_v2 = await keno_queries.create_tier_admin(
        pool, admin_id=admin_id, tier_number=2, min_reserve=Decimal("1200"), max_pick_count=6,
        max_top_multiplier=Decimal("60"), stake_options=[Decimal("10"), Decimal("20"), Decimal("50")],
        max_win_per_ticket=Decimal("3000"), max_round_exposure_pct=Decimal("0.10"),
        paytable_profile="low_variance", reason="raise tier 2's own threshold",
    )
    assert tier2_v2["id"] != tier2["id"]

    await _fund_reserve(conn, Decimal("1500"))
    # Above the OLD tier 2 threshold (1000) but the current evaluation
    # must use the NEW one (1200) -- still eligible either way here, but
    # proves evaluation reads the latest row, not a stale one.
    result = await keno_tier_automation.evaluate_tier_transition(pool, reserve_balance=Decimal("1500"))
    assert result.changed is False  # still just a candidate (7-day hold)
    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["candidate_tier_id"] == tier2_v2["id"]  # the latest row, not the superseded one


async def test_circuit_breaker_is_a_noop_with_no_ticket_activity(pool, conn):
    await _make_two_tiers(pool, conn)
    await keno_queries.create_config_admin(
        pool,
        admin_id=(await create_test_admin(pool, role="superadmin"))[0],
        round_cycle_seconds=45, betting_seconds=25, draw_seconds=12, result_seconds=8,
        min_picks=1, max_picks=6, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=True,
        reason="circuit breaker test",
    )
    result = await keno_tier_automation.check_circuit_breaker(pool)
    assert result.changed is False


async def test_circuit_breaker_trips_when_actual_payouts_far_exceed_expected(pool, conn):
    tier1, tier2 = await _make_two_tiers(pool, conn, tier1_min=Decimal("0"), tier2_min=Decimal("1000"))
    # Start on tier 2 so there's genuinely somewhere lower to demote to.
    await conn.execute("UPDATE keno_tier_state SET current_tier_id = $1 WHERE id = 1", tier2["id"])
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    config = await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=6, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=True,
        reason="circuit breaker trip test",
    )
    paytable = await conn.fetchrow(
        "SELECT id FROM keno_paytables WHERE pick_count = 1 AND profile = 'low_variance' "
        "ORDER BY effective_from DESC LIMIT 1"
    )
    user_id = await create_funded_user(conn, Decimal("1000"))
    round_id = await conn.fetchval(
        "INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at) "
        "VALUES ((SELECT COALESCE(MAX(seq), 0) + 1 FROM keno_rounds), 'betting_open', $1, $2, $3, now()) "
        "RETURNING id",
        config["id"], tier2["id"], keno.server_seed_hash(keno.generate_server_seed()),
    )
    # A ticket whose own recorded expected_payout_contribution is small
    # (10 ETB) -- this is the "expected" side of the ratio.
    await conn.execute(
        "INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, "
        "expected_payout_contribution, status, idempotency_key) "
        "VALUES ($1, $2, $3, 1, 10, 10, 'pending', $4)",
        round_id, user_id, paytable["id"], f"test-cb-ticket-{uuid.uuid4()}",
    )
    # This is a shared dev database -- other test files' own tickets
    # placed in the last 24h already contribute real expected_payout_
    # contribution to the same trailing window this function reads, so
    # the actual payout below is sized against whatever that real
    # baseline is *right now*, not a fixed guess (an earlier version of
    # this test used a fixed 500 ETB and flaked hard: 1,130+ ETB of real
    # expected-payout activity from the rest of today's test runs was
    # already sitting in the same 24h window).
    baseline_expected = await conn.fetchval(
        "SELECT COALESCE(SUM(kt.expected_payout_contribution), 0) FROM keno_tickets kt "
        "JOIN keno_rounds kr ON kr.id = kt.round_id WHERE kr.betting_opened_at >= now() - interval '24 hours'"
    )
    multiple = Decimal("3.0")
    payout_amount = (Decimal(baseline_expected) + Decimal("10")) * multiple + Decimal("1000")

    # A real keno_payout ledger transaction, structured exactly like a
    # genuine settlement would (reserve debited, player credited).
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -payout_amount * 2), ledger.Entry(reserve.id, payout_amount * 2)],
        idempotency_key=f"test-cb-fund-{uuid.uuid4()}", created_by="test",
    )
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    await ledger.post(
        conn, "keno_payout",
        [ledger.Entry(reserve.id, -payout_amount), ledger.Entry(cash.id, payout_amount)],
        idempotency_key=f"test-cb-payout-{uuid.uuid4()}", created_by="test",
    )

    result = await keno_tier_automation.check_circuit_breaker(pool)
    assert result.changed is True
    assert result.trigger == "circuit_breaker"
    assert result.from_tier_number == 2
    assert result.to_tier_number == 1

    state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1")
    assert state["current_tier_id"] == tier1["id"]

    change = await conn.fetchrow(
        "SELECT * FROM keno_tier_changes WHERE trigger = 'circuit_breaker' ORDER BY id DESC LIMIT 1"
    )
    assert change["from_tier_id"] == tier2["id"]
    assert change["to_tier_id"] == tier1["id"]
    assert "exceeded" in change["reason"] and "expected" in change["reason"]  # sanity: a real, specific reason


async def test_circuit_breaker_at_floor_tier_cannot_demote_further_but_does_not_crash(pool, conn):
    tier1, _tier2 = await _make_two_tiers(pool, conn)
    # Already on tier 1 -- nothing lower exists.
    admin_id, *_ = await create_test_admin(pool, role="superadmin")
    config = await keno_queries.create_config_admin(
        pool, admin_id=admin_id, round_cycle_seconds=45, betting_seconds=25, draw_seconds=12,
        result_seconds=8, min_picks=1, max_picks=6, max_tickets_per_user_per_round=3,
        per_user_round_capacity_share_bps=2000, jackpot_diversion_bps=150, keno_enabled=True,
        reason="floor tier test",
    )
    paytable = await conn.fetchrow(
        "SELECT id FROM keno_paytables WHERE pick_count = 1 AND profile = 'low_variance' "
        "ORDER BY effective_from DESC LIMIT 1"
    )
    user_id = await create_funded_user(conn, Decimal("1000"))
    round_id = await conn.fetchval(
        "INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at) "
        "VALUES ((SELECT COALESCE(MAX(seq), 0) + 1 FROM keno_rounds), 'betting_open', $1, $2, $3, now()) "
        "RETURNING id",
        config["id"], tier1["id"], keno.server_seed_hash(keno.generate_server_seed()),
    )
    await conn.execute(
        "INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, "
        "expected_payout_contribution, status, idempotency_key) "
        "VALUES ($1, $2, $3, 1, 10, 10, 'pending', $4)",
        round_id, user_id, paytable["id"], f"test-cb-floor-{uuid.uuid4()}",
    )
    # Sized against the real current baseline, same reasoning as the
    # "trips" test above -- must genuinely exceed the trip threshold so
    # this test exercises the "tripped, but already at the floor" branch,
    # not accidentally the ordinary "didn't trip" one.
    baseline_expected = await conn.fetchval(
        "SELECT COALESCE(SUM(kt.expected_payout_contribution), 0) FROM keno_tickets kt "
        "JOIN keno_rounds kr ON kr.id = kt.round_id WHERE kr.betting_opened_at >= now() - interval '24 hours'"
    )
    payout_amount = (Decimal(baseline_expected) + Decimal("10")) * Decimal("3.0") + Decimal("1000")
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -payout_amount * 2), ledger.Entry(reserve.id, payout_amount * 2)],
        idempotency_key=f"test-cb-floor-fund-{uuid.uuid4()}", created_by="test",
    )
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    await ledger.post(
        conn, "keno_payout",
        [ledger.Entry(reserve.id, -payout_amount), ledger.Entry(cash.id, payout_amount)],
        idempotency_key=f"test-cb-floor-payout-{uuid.uuid4()}", created_by="test",
    )
    trips_before = metrics.keno_tier_changes_total.labels(trigger="circuit_breaker")._value.get()

    result = await keno_tier_automation.check_circuit_breaker(pool)
    assert result.changed is False  # nothing lower to demote to -- must not crash
    # But the breaker DID genuinely trip (not just "didn't reach the
    # threshold") -- the metric distinguishes the two, per this module's
    # own comment on why a floor-tier trip still increments it.
    trips_after = metrics.keno_tier_changes_total.labels(trigger="circuit_breaker")._value.get()
    assert trips_after == trips_before + 1
