"""Integration tests for packages/core/responsible_gaming.py against a
real Postgres, and for its two enforcement points: RoundEngine.join()
(self-exclusion, cool-off, and the loss-cap gate) and
services/payments/deposits.py's create_deposit_intent() (self-exclusion,
ban, cool-off, and the per-user deposit cap).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from packages.core import ledger, responsible_gaming
from services.bot.registration import PhoneAlreadyRegistered, register_from_contact
from services.engine.round_engine import RoundEngine, load_room_config
from services.payments import deposits
from tests.integration.conftest import (
    create_funded_user,
    create_room,
    create_user,
    next_telegram_id,
    unique_phone,
)
from tests.integration.test_payments_deposits import FakePaymentProvider


class _NullProvider:
    name = "chapa"

    async def create_checkout(self, **kwargs):
        raise NotImplementedError

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, **kwargs):
        raise NotImplementedError


async def _deposit(pool, redis, conn, user_id, amount, *, provider=None, **overrides):
    kwargs = dict(
        user_id=user_id,
        amount=amount,
        phone_e164="+251911000000",
        return_url="https://app.test/return",
        callback_url="https://payments.test/webhooks/chapa",
        min_deposit=Decimal("1.00"),
        daily_cap=Decimal("1000000.00"),
    )
    kwargs.update(overrides)
    return await deposits.create_deposit_intent(pool, redis, provider or _NullProvider(), **kwargs)


# --- get_or_create_limits / set_deposit_limit / set_loss_limit -------------


async def test_get_or_create_limits_lazily_creates_a_row(pool, conn):
    user_id = await create_user(conn)
    limits = await responsible_gaming.get_or_create_limits(conn, user_id)
    assert limits.user_id == user_id
    assert limits.daily_deposit_cap is None

    row_count = await conn.fetchval(
        "SELECT count(*) FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert row_count == 1


async def test_set_deposit_limit_decrease_applies_instantly(pool, conn):
    user_id = await create_user(conn)
    await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("1000.00"))
    applied_now = await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("200.00"))
    assert applied_now is True

    limits = await responsible_gaming.get_or_create_limits(conn, user_id)
    assert responsible_gaming.effective_deposit_cap(limits) == Decimal("200.00")


async def test_set_deposit_limit_increase_is_deferred_24_hours(pool, conn):
    user_id = await create_user(conn)
    await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("200.00"))
    applied_now = await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("5000.00"))
    assert applied_now is False

    limits = await responsible_gaming.get_or_create_limits(conn, user_id)
    assert responsible_gaming.effective_deposit_cap(limits) == Decimal("200.00")  # still old cap
    assert responsible_gaming.effective_deposit_cap(
        limits, now=datetime.now(UTC) + timedelta(hours=25)
    ) == Decimal("5000.00")


async def test_set_loss_limit_same_instant_vs_deferred_rule(pool, conn):
    user_id = await create_user(conn)
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("500.00"))
    applied_now = await responsible_gaming.set_loss_limit(conn, user_id, Decimal("50.00"))
    assert applied_now is True
    limits = await responsible_gaming.get_or_create_limits(conn, user_id)
    assert responsible_gaming.effective_loss_cap(limits) == Decimal("50.00")


# --- self-exclusion ---------------------------------------------------------


async def test_self_exclusion_below_minimum_days_is_rejected(pool, conn):
    user_id = await create_user(conn)
    with pytest.raises(responsible_gaming.SelfExclusionTooShort):
        await responsible_gaming.self_exclude(pool, user_id, days=30)


async def test_self_exclusion_sets_status_and_until(pool, conn):
    user_id = await create_user(conn)
    await responsible_gaming.self_exclude(pool, user_id)

    status = await conn.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "self_excluded"

    until = await conn.fetchval(
        "SELECT self_excluded_until FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert until > datetime.now(UTC) + timedelta(days=179)


async def test_self_excluded_user_blocked_from_play(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.self_exclude(pool, user_id)

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        result = await engine.join(user_id, 1)
        assert result.ok is False
        assert result.reason == "self_excluded"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_self_excluded_users_phone_cannot_register_a_second_account(pool, conn):
    # Spec section 12: self-exclusion "blocks registration by the same
    # phone". There's no special-case code for this anywhere -- it falls
    # naturally out of phone_e164's UNIQUE constraint plus
    # register_from_contact()'s own existing-user lookup being keyed on
    # telegram_id, not phone: a brand new Telegram account (a different
    # telegram_id) trying to register with a phone that's already tied to
    # a (self-excluded or not) user hits the same UniqueViolationError ->
    # PhoneAlreadyRegistered path every duplicate-phone registration does.
    shared_phone = unique_phone()
    original_telegram_id = next_telegram_id()
    excluded_user = await register_from_contact(
        pool,
        sender_telegram_id=original_telegram_id,
        contact_user_id=original_telegram_id,
        contact_phone=shared_phone,
        display_name="Original Account",
    )
    await responsible_gaming.self_exclude(pool, excluded_user.id)

    new_telegram_id = next_telegram_id()
    with pytest.raises(PhoneAlreadyRegistered):
        await register_from_contact(
            pool,
            sender_telegram_id=new_telegram_id,
            contact_user_id=new_telegram_id,
            contact_phone=shared_phone,
            display_name="Second Attempt",
        )


# --- cool-off ----------------------------------------------------------


async def test_cool_off_blocks_play_while_active(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        result = await engine.join(user_id, 1)
        assert result.ok is False
        assert result.reason == "cooling_off"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_cool_off_lifts_itself_after_expiry(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)
    # Directly backdate cooloff_until into the past -- no scheduled job is
    # involved in lifting a cool-off, the timestamp itself is authoritative.
    await conn.execute(
        "UPDATE responsible_gaming_limits SET cooloff_until = now() - interval '1 minute' "
        "WHERE user_id = $1",
        user_id,
    )

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        result = await engine.join(user_id, 1)
        assert result.ok is True
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


# --- loss cap ------------------------------------------------------------


async def test_loss_cap_blocks_a_stake_that_would_exceed_it(pool, redis, conn, card_pool):
    room_id = await create_room(conn, stake=Decimal("100.00"), min_players=2)
    room = await load_room_config(pool, room_id)
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("50.00"))

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        result = await engine.join(user_id, 1)
        assert result.ok is False
        assert result.reason == "loss_limit_reached"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_loss_cap_holds_under_two_concurrent_joins_in_different_rooms(pool, redis, conn, card_pool):
    """A code review pass caught that check_stake_allowed()'s loss-cap
    check ran before RoundEngine.join()'s own transaction started, with no
    lock covering the gap between the check and the stake actually
    committing. join()'s per-room self._join_lock doesn't help here --
    it's a plain asyncio.Lock scoped to one RoundEngine instance, so it
    does nothing to serialize the SAME user joining two DIFFERENT rooms at
    once. Without a fix, two concurrent joins can both read today's net
    loss as 0 before either stake lands, and both pass a cap that either
    one alone would have correctly blocked.
    """
    cap = Decimal("100.00")
    stake = Decimal("60.00")  # one stake is under the cap; two together exceed it
    room_a = await load_room_config(pool, await create_room(conn, stake=stake, min_players=2))
    room_b = await load_room_config(pool, await create_room(conn, stake=stake, min_players=2))
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, cap)

    engine_a = RoundEngine(pool, redis, room_a, card_pool)
    engine_b = RoundEngine(pool, redis, room_b, card_pool)
    task_a = asyncio.create_task(engine_a.run_forever())
    task_b = asyncio.create_task(engine_b.run_forever())
    try:
        result_a, result_b = await asyncio.gather(
            engine_a.join(user_id, 1), engine_b.join(user_id, 1)
        )

        # Exactly one of the two concurrent stakes may succeed -- never
        # both, since both together would put this player past their own
        # declared daily loss cap.
        outcomes = [result_a.ok, result_b.ok]
        assert outcomes.count(True) == 1, (result_a, result_b)
        failed = result_b if result_a.ok else result_a
        assert failed.reason == "loss_limit_reached"

        assert await responsible_gaming.today_net_loss(conn, user_id) <= cap
    finally:
        await engine_a.stop()
        await engine_b.stop()
        await asyncio.wait_for(task_a, timeout=10)
        await asyncio.wait_for(task_b, timeout=10)


async def test_today_net_loss_reflects_stakes_and_payouts(pool, conn):
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    house = await ledger.get_or_create_account(conn, None, "house_revenue")

    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-100.00")), ledger.Entry(pot.id, Decimal("100.00"))],
        idempotency_key=f"test-stake-{user_id}",
    )
    net_loss = await responsible_gaming.today_net_loss(conn, user_id)
    assert net_loss == Decimal("100.00")

    await ledger.post(
        conn, "payout", [ledger.Entry(pot.id, Decimal("-30.00")), ledger.Entry(cash.id, Decimal("30.00"))],
        idempotency_key=f"test-payout-{user_id}",
    )
    net_loss = await responsible_gaming.today_net_loss(conn, user_id)
    assert net_loss == Decimal("70.00")


async def test_today_net_loss_uses_the_ethiopian_calendar_day_not_utc(pool, conn):
    # today_net_loss() used a bare date_trunc('day', now()) -- the
    # Postgres session's ambient (UTC-by-default) day boundary, not the
    # Ethiopian one this loss cap is actually meant to reset at. EAT is
    # UTC+3, so EAT's midnight falls three hours before UTC's -- a stake
    # placed one hour before *UTC* midnight is still within *today's*
    # Ethiopian calendar day and must count toward the cap, even though a
    # UTC-ambient boundary would wrongly place it in "yesterday" and miss
    # it. This mismatch window exists every single night this platform
    # runs, not a contrived edge case (see DECISIONS.md's day-boundary
    # timezone entries for the admin dashboard's equivalent bug/fix).
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")

    before = await responsible_gaming.today_net_loss(conn, user_id)

    # A fixed "1 hour before UTC midnight" offset only lands in *today's*
    # EAT day when "now" itself isn't within the first ~3 hours of the EAT
    # day (EAT is UTC+3, so that window is UTC 21:00-23:59) -- run this
    # test during that window and the offset actually lands in *yesterday*
    # EAT, making the test itself wrong rather than the code (caught live:
    # this failed at a real 01:02 EAT run). Anchor to the start of today's
    # EAT day instead (the same boundary the production query itself
    # computes) plus a small positive offset -- always within today's EAT
    # day by construction, regardless of what wall-clock time this runs at,
    # while still landing on a different *UTC* calendar day than "now"
    # whenever EAT and UTC don't already agree -- the exact cross-boundary
    # case this test exists to prove. Computed *before* posting so the
    # entry can be inserted with this timestamp directly (ledger_entries
    # is append-only, migrations/versions/b8e4a1f0c3d7_ledger_append_only.py
    # -- there is no "insert now, backdate after" path any more).
    boundary = (
        await conn.fetchrow(
            "SELECT (date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') "
            "AT TIME ZONE 'Africa/Addis_Ababa') + interval '1 minute' AS ts"
        )
    )["ts"]
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-40.00")), ledger.Entry(pot.id, Decimal("40.00"))],
        idempotency_key=f"tz-boundary-loss-{user_id}",
        created_at=boundary,
    )

    after = await responsible_gaming.today_net_loss(conn, user_id)
    assert after - before == Decimal("40.00")


# --- deposit-side enforcement ----------------------------------------------


async def test_deposit_blocked_while_cooling_off(pool, redis, conn):
    user_id = await create_user(conn)
    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)
    with pytest.raises(deposits.DepositorCoolingOff):
        await _deposit(pool, redis, conn, user_id, Decimal("100.00"))


async def test_deposit_blocked_when_banned(pool, redis, conn):
    user_id = await create_user(conn)
    await conn.execute("UPDATE users SET status = 'banned' WHERE id = $1", user_id)
    with pytest.raises(deposits.DepositorBanned):
        await _deposit(pool, redis, conn, user_id, Decimal("100.00"))


async def test_per_user_deposit_cap_tighter_than_global_is_enforced(pool, redis, conn):
    user_id = await create_user(conn)
    await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("50.00"))
    with pytest.raises(deposits.DailyDepositCapExceeded):
        await _deposit(pool, redis, conn, user_id, Decimal("100.00"), daily_cap=Decimal("1000000.00"))


async def test_per_user_deposit_cap_under_global_still_allows_a_smaller_deposit(pool, redis, conn):
    user_id = await create_user(conn)
    await responsible_gaming.set_deposit_limit(conn, user_id, Decimal("50.00"))
    intent = await _deposit(
        pool, redis, conn, user_id, Decimal("30.00"), daily_cap=Decimal("1000000.00"), provider=FakePaymentProvider()
    )
    assert intent.our_ref.startswith("DEP-")


async def test_deposit_daily_cap_uses_the_ethiopian_calendar_day_not_utc(pool, redis, conn):
    # Same class of bug as today_net_loss()'s -- _check_deposit_eligibility()
    # also used a bare date_trunc('day', now()). A payment created one hour
    # before *UTC* midnight is still within *today's* Ethiopian calendar day
    # (EAT is UTC+3) and must count toward today's cap; a UTC-ambient
    # boundary would wrongly exclude it.
    user_id = await create_user(conn)
    first = await _deposit(
        pool, redis, conn, user_id, Decimal("60.00"),
        daily_cap=Decimal("100.00"), provider=FakePaymentProvider(),
    )
    # See test_today_net_loss_uses_the_ethiopian_calendar_day_not_utc's own
    # comment: a fixed "1 hour before UTC midnight" offset is only within
    # today's EAT day part of the time (it fails during EAT's first ~3
    # hours) -- anchor to today's EAT-day start instead, always correct
    # regardless of when this test actually runs.
    boundary = (
        await conn.fetchrow(
            "SELECT (date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') "
            "AT TIME ZONE 'Africa/Addis_Ababa') + interval '1 minute' AS ts"
        )
    )["ts"]
    await conn.execute("UPDATE payments SET created_at = $1 WHERE id = $2", boundary, first.payment_id)

    # A second 60.00 deposit only exceeds the 100.00 cap if the first
    # deposit above is still correctly counted as part of today's total --
    # FakePaymentProvider() (not the default _NullProvider) so that if the
    # cap check wrongly lets this through, the failure is a clean "no
    # exception raised" rather than an unrelated provider error masking it.
    with pytest.raises(deposits.DailyDepositCapExceeded):
        await _deposit(
            pool, redis, conn, user_id, Decimal("60.00"),
            daily_cap=Decimal("100.00"), provider=FakePaymentProvider(),
        )


# --- marketing audience query ------------------------------------------


async def test_marketing_eligible_excludes_self_excluded_banned_and_cooling_off(pool, conn):
    active_user = await create_user(conn)
    self_excluded_user = await create_user(conn)
    banned_user = await create_user(conn)
    cooling_off_user = await create_user(conn)

    await responsible_gaming.self_exclude(pool, self_excluded_user)
    await conn.execute("UPDATE users SET status = 'banned' WHERE id = $1", banned_user)
    await responsible_gaming.cool_off(conn, cooling_off_user, duration_hours=24)

    eligible = set(await responsible_gaming.marketing_eligible_user_ids(pool))
    assert active_user in eligible
    assert self_excluded_user not in eligible
    assert banned_user not in eligible
    assert cooling_off_user not in eligible


async def test_marketing_eligible_includes_a_user_whose_cooloff_has_expired(pool, conn):
    user_id = await create_user(conn)
    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)
    await conn.execute(
        "UPDATE responsible_gaming_limits SET cooloff_until = now() - interval '1 minute' "
        "WHERE user_id = $1",
        user_id,
    )
    eligible = set(await responsible_gaming.marketing_eligible_user_ids(pool))
    assert user_id in eligible
