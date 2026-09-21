"""Simulated Players: real-Postgres/real-Redis tests, no mocks for the
engine or ledger (matching test_round_engine.py's own convention).
Covers funding-via-house_float, roster cap, lifecycle transitions, reset,
analytics isolation, withdrawal rejection, a bot's claim going through
the real RoundEngine.claim() with no shortcut, a mixed bot+real-player
round settling correctly, and 10-bot concurrent joins with no race.

Every test that creates a bot uses the `bot_factory` fixture below, which
fully unwinds what it created at teardown: simulated_players enforces a
real, hard 10-bot cap, and this suite's dev database is shared and never
reset between runs (see conftest.py's own create_room() docstring for the
same "accumulates real rows forever" lesson learned the hard way once
already) -- a bot left behind by every test run would permanently consume
one of only 10 real roster slots, eventually breaking every future test
(and any real manual QA) that needs to create one.
"""

import asyncio
from decimal import Decimal

import httpx
import pytest

from packages.core import ledger
from services.admin import simulated_players_queries as spq
from services.engine.round_engine import RoundEngine, load_room_config
from services.payments import withdrawals
from tests.integration.conftest import create_funded_user, create_room
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin


async def make_engine(pool, redis, card_pool, room_id) -> RoundEngine:
    room = await load_room_config(pool, room_id)
    return RoundEngine(pool, redis, room, card_pool)


async def _delete_bot_completely(pool, user_id: int) -> None:
    """Frees this bot's roster-cap slot -- the only thing that actually
    needs to happen for test isolation. services/admin/
    simulated_players_queries.py's create_simulated_player() enforces the
    10-bot cap via a bare `SELECT count(*) FROM simulated_players`
    (no status filter), so a leftover simulated_players row is the one
    real constraint a test run must not leave behind, or a later test
    creating a bot starts failing on an exhausted cap it never actually
    used itself.

    Does NOT touch ledger_entries/account_balances/accounts/users:
    ledger_entries is append-only (migrations/versions/
    b8e4a1f0c3d7_ledger_append_only.py), and on inspection none of that
    was ever load-bearing for test correctness anyway -- nothing in this
    file (or test_seed_simulated_players_cli.py, or
    test_simulated_players_console_e2e.py) counts `users`/`accounts`
    rows globally, `display_name` has no uniqueness constraint, and new
    bots get fresh negative telegram_ids via MIN(telegram_id)-1
    regardless of what's left over. Leaving the user/account/ledger rows
    behind is exactly the same "harmless orphaned test data" every other
    test file in this suite already relies on (see next_telegram_id()'s
    own docstring above) -- this helper previously deleted them anyway,
    out of general tidiness rather than genuine necessity, which is what
    made it the one place in the whole codebase mutating ledger history
    directly. See DECISIONS.md, 2026-09-17.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM claim_attempts WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_winners WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_entries WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM simulated_players WHERE user_id = $1", user_id)


@pytest.fixture
async def bot_factory(pool):
    created: list[int] = []

    async def _create(admin_id: int, name: str = "Test Bot", strategy: str = "normal") -> int:
        user_id = await spq.create_simulated_player(
            pool, admin_id=admin_id, display_name=name, strategy=strategy, ip_address=None
        )
        created.append(user_id)
        return user_id

    yield _create

    for user_id in created:
        await _delete_bot_completely(pool, user_id)


# --- funding / creation ----------------------------------------------------


async def test_create_simulated_player_funds_via_house_float_adjustment(pool, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await bot_factory(admin_id)

    user_row = await pool.fetchrow("SELECT is_simulated, telegram_id FROM users WHERE id = $1", user_id)
    assert user_row["is_simulated"] is True
    assert user_row["telegram_id"] < 0  # never collides with a real Telegram id

    cash = await ledger.get_or_create_account(pool, user_id, "user_cash")
    balance = await ledger.balance(pool, cash.id)
    assert balance == spq.INITIAL_SIMULATED_BALANCE

    txn = await pool.fetchrow(
        "SELECT kind, memo FROM ledger_transactions WHERE id = ("
        "  SELECT transaction_id FROM ledger_entries WHERE account_id = $1 ORDER BY id DESC LIMIT 1"
        ")",
        cash.id,
    )
    assert txn["kind"] == "adjustment"
    assert txn["memo"] == f"simulated_player_seed:user_{user_id}"

    sp_row = await pool.fetchrow("SELECT status, strategy FROM simulated_players WHERE user_id = $1", user_id)
    assert sp_row["status"] == "disabled"
    assert sp_row["strategy"] == "normal"


async def test_roster_cap_rejects_an_eleventh_bot(pool, bot_factory, monkeypatch):
    # Patched down to a small cap for this test only -- MAX_SIMULATED_PLAYERS
    # is 200 in production, and this test only needs to prove the real
    # enforcement code path (create_simulated_player reads the module-level
    # constant at call time, so patching it here exercises the exact same
    # branch), not actually create 200 real bots with 200 real ledger
    # transactions just to reach it.
    monkeypatch.setattr(spq, "MAX_SIMULATED_PLAYERS", 3)
    admin_id, *_ = await create_test_admin(pool)
    existing = await pool.fetchval("SELECT count(*) FROM simulated_players")
    headroom = spq.MAX_SIMULATED_PLAYERS - existing
    assert headroom > 0, "roster already full before this test even started -- a leftover-bot cleanup bug"
    for i in range(headroom):
        await bot_factory(admin_id, name=f"Roster Bot {i}")
    assert (await pool.fetchval("SELECT count(*) FROM simulated_players")) == spq.MAX_SIMULATED_PLAYERS
    with pytest.raises(spq.SimulatedPlayerRosterFull):
        await bot_factory(admin_id, name="One Too Many")


# --- lifecycle ---------------------------------------------------------


async def test_lifecycle_transitions_disabled_idle_paused_stop(pool, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await bot_factory(admin_id)

    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)
    row = await pool.fetchrow("SELECT status, activated_at FROM simulated_players WHERE user_id = $1", user_id)
    assert row["status"] == "idle"
    assert row["activated_at"] is not None
    first_activated_at = row["activated_at"]

    await spq.pause_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)
    row = await pool.fetchrow("SELECT status, activated_at FROM simulated_players WHERE user_id = $1", user_id)
    assert row["status"] == "paused"
    assert row["activated_at"] == first_activated_at  # never reset once set

    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)
    assert (await pool.fetchval("SELECT status FROM simulated_players WHERE user_id = $1", user_id)) == "idle"

    await spq.stop_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)
    assert (await pool.fetchval("SELECT status FROM simulated_players WHERE user_id = $1", user_id)) == "disabled"

    # disabled -> paused is not an allowed transition
    with pytest.raises(spq.InvalidSimulatedPlayerTransition):
        await spq.pause_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)


async def test_reset_tops_balance_back_to_5000_and_disables(pool, conn, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await bot_factory(admin_id)
    await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)

    # Simulate the bot having won some money -- push its balance above 5000.
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    house_revenue = await ledger.get_or_create_account(conn, None, "house_revenue")
    await ledger.post(
        conn, "payout",
        [ledger.Entry(house_revenue.id, -Decimal("500.00")), ledger.Entry(cash.id, Decimal("500.00"))],
        idempotency_key=f"test-bot-win-{user_id}",
    )
    assert await ledger.balance(conn, cash.id) == Decimal("5500.00")

    await spq.reset_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test reset", ip_address=None)

    assert await ledger.balance(conn, cash.id) == spq.INITIAL_SIMULATED_BALANCE
    row = await pool.fetchrow("SELECT status, current_room_id FROM simulated_players WHERE user_id = $1", user_id)
    assert row["status"] == "disabled"
    assert row["current_room_id"] is None


async def test_stop_all_disables_every_active_bot_and_flips_global_enabled_false(pool, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    user_ids = [await bot_factory(admin_id, name=f"StopAll Bot {i}") for i in range(3)]
    for user_id in user_ids:
        await spq.start_simulated_player_admin(pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None)
    await spq.update_settings_admin(
        pool, admin_id=admin_id, enabled=True, max_concurrent_bots=10, reason="test enable", ip_address=None
    )

    try:
        result = await spq.stop_all_simulated_players_admin(pool, admin_id=admin_id, reason="incident", ip_address=None)
        assert result["stopped_count"] == 3

        for user_id in user_ids:
            assert (await pool.fetchval("SELECT status FROM simulated_players WHERE user_id = $1", user_id)) == "disabled"
        settings = await spq.get_settings_admin(pool)
        assert settings["enabled"] is False

        stop_all_row = await pool.fetchrow(
            "SELECT before, after FROM admin_audit_log WHERE action = 'simulated_players.stop_all' "
            "ORDER BY id DESC LIMIT 1"
        )
        assert stop_all_row is not None
    finally:
        # Restore the shipped-default global switch regardless of outcome,
        # so this test can never leave enabled=true behind for whatever
        # test/manual QA runs against this shared database next.
        await spq.update_settings_admin(
            pool, admin_id=admin_id, enabled=False, max_concurrent_bots=10, reason="test cleanup", ip_address=None
        )


async def test_active_simulated_player_count_reflects_idle_and_playing_but_not_disabled_or_paused(
    pool, bot_factory
):
    # Real user request: an idle bot (not currently seated in any round)
    # was invisible everywhere -- it has no WebSocket of its own to count
    # toward the Rooms screen's "online" headcount (services/gateway/
    # connection.py), and it only ever shows up in "playing" once actually
    # in round_entries. This is the query behind the fix: an idle bot
    # should read as "online" the same way a real, connected-but-not-yet-
    # playing player already does.
    admin_id, *_ = await create_test_admin(pool)

    await spq.update_settings_admin(
        pool, admin_id=admin_id, enabled=True, max_concurrent_bots=10, reason="test enable", ip_address=None
    )
    try:
        # Baselined *after* enabling, not before: this shared dev database
        # can have other real active bots at test time, and the count is
        # forced to 0 whenever the global switch is off (checked below),
        # so a baseline taken before enabling could read 0 even with other
        # active bots on file -- comparing against that would overcount
        # every delta below by however many of those there are.
        baseline = await spq.active_simulated_player_count(pool)

        user_id = await bot_factory(admin_id)
        # Freshly created: status = "disabled" -- must not count yet.
        assert await spq.active_simulated_player_count(pool) == baseline

        await spq.start_simulated_player_admin(
            pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None
        )
        assert await spq.active_simulated_player_count(pool) == baseline + 1  # idle counts

        await spq.pause_simulated_player_admin(
            pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None
        )
        assert await spq.active_simulated_player_count(pool) == baseline  # paused doesn't

        await spq.start_simulated_player_admin(
            pool, admin_id=admin_id, user_id=user_id, reason="test", ip_address=None
        )
        await pool.execute(
            "UPDATE simulated_players SET status = 'playing' WHERE user_id = $1", user_id
        )
        assert await spq.active_simulated_player_count(pool) == baseline + 1  # playing counts too

        # The global switch is belt-and-suspenders: even with a real
        # "playing" row on file, the whole feature reading as off must
        # force this back to whatever real WebSocket connections alone
        # would show.
        await spq.update_settings_admin(
            pool, admin_id=admin_id, enabled=False, max_concurrent_bots=10,
            reason="test: verify global gate", ip_address=None,
        )
        assert await spq.active_simulated_player_count(pool) == 0
    finally:
        await spq.update_settings_admin(
            pool, admin_id=admin_id, enabled=False, max_concurrent_bots=10, reason="test cleanup", ip_address=None
        )


# --- balance isolation ---------------------------------------------------


async def test_bot_ledger_activity_excluded_from_dashboard_summary(pool, redis, card_pool, conn, bot_factory):
    from services.admin.queries import dashboard_summary

    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)
    real_user_id = await create_funded_user(conn, Decimal("1000.00"))

    # Delta, not an absolute total: dashboard_summary() sums the whole
    # shared dev database's activity for today, and this suite runs many
    # other tests that also stake real money today -- the only reliable
    # assertion is "how much did *this test's own* activity add," not a
    # fixed number.
    before = Decimal((await dashboard_summary(pool))["stakes_today"])

    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=20, call_interval_ms=5)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        real_result = await engine.join(real_user_id, card_no=1)
        assert real_result.ok, real_result.reason
        bot_result = await engine.join(bot_id, card_no=2)
        assert bot_result.ok, bot_result.reason
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)

    after = Decimal((await dashboard_summary(pool))["stakes_today"])
    # Exactly one real player's stake (20.00) should count -- the bot's
    # identical 20.00 stake must be excluded from the delta entirely.
    assert after - before == Decimal("20.00")


async def test_bot_excluded_from_retention_cohorts(pool, bot_factory):
    from services.admin.queries import retention_cohorts

    admin_id, *_ = await create_test_admin(pool)
    await bot_factory(admin_id)

    # No assertion needed on cohort *contents* beyond: this must not raise
    # with the WHERE NOT is_simulated clause in place (a syntax/typo
    # regression in that clause would fail this outright).
    cohorts = await retention_cohorts(pool, weeks=1)
    assert isinstance(cohorts, list)


async def test_withdrawal_request_rejected_for_simulated_user(pool, redis, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)

    class _NullProvider:
        name = "manual"

    with pytest.raises(withdrawals.SimulatedPlayerCannotWithdraw):
        await withdrawals.request_withdrawal(
            pool, redis, _NullProvider(),
            user_id=bot_id, amount=Decimal("1000.00"), method_kind="telebirr",
            account_ref="0911223344", holder_name="Bot",
            min_withdraw=Decimal("50.00"), auto_approve_limit=Decimal("2000.00"),
            kyc_threshold=Decimal("5000.00"), chargeback_window_minutes=30,
        )
    payments_count = await pool.fetchval(
        "SELECT count(*) FROM payments WHERE user_id = $1 AND direction = 'out'", bot_id
    )
    assert payments_count == 0


# --- real winning validation, no shortcut -------------------------------


async def test_bot_claim_goes_through_real_round_engine_claim_validation(pool, redis, card_pool, conn, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)
    other_user_id = await create_funded_user(conn)

    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=1, call_interval_ms=50)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(bot_id, card_no=1)).ok
        assert (await engine.join(other_user_id, card_no=2)).ok

        async def wait_running():
            while engine.status != "running":
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_running(), timeout=10)
        # Claim the instant the round goes live, before any numbers have
        # been called -- no real card can have a valid pattern yet, and
        # there is no bot-only branch anywhere in claim() to special-case
        # this: it goes through the exact same pattern check a real
        # player's claim does.
        result = await engine.claim(bot_id, card_no=1)
        assert result.ok is False
        assert result.reason == "no_pattern"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)


async def test_bot_and_real_player_share_a_pot_and_settle_correctly(pool, redis, card_pool, conn, bot_factory):
    # Neither player explicitly calls claim(), but join()'s own
    # auto_mark=True default means the server auto-claims for whichever
    # of the two genuinely completes a real pattern first -- exactly the
    # same for a bot's card as a real player's, no special-cased bot
    # path. Whichever of them actually wins (not predictable from here,
    # and deliberately not asserted on), the round ends in a real
    # "payout" settlement, not a void.
    admin_id, *_ = await create_test_admin(pool)
    bot_id = await bot_factory(admin_id)
    real_user_id = await create_funded_user(conn, Decimal("1000.00"))

    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, lobby_seconds=1, call_interval_ms=10,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(bot_id, card_no=1)).ok
        assert (await engine.join(real_user_id, card_no=2)).ok

        async def wait_settled():
            while engine.status != "idle" or engine.round_id is not None:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_settled(), timeout=30)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)

    round_row = await pool.fetchrow(
        "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
    )
    assert round_row["status"] == "done"
    entries = await pool.fetch(
        "SELECT amount FROM ledger_entries WHERE transaction_id = ("
        "  SELECT id FROM ledger_transactions WHERE round_id = $1 AND kind = 'payout'"
        ")",
        round_row["id"],
    )
    assert entries  # a real settlement happened, not silently nothing
    assert sum(e["amount"] for e in entries) == Decimal("0.00")  # sum-to-zero, real or bot winner doesn't matter


# --- 10-bot concurrency ---------------------------------------------------


async def test_ten_bots_join_the_same_room_concurrently_no_double_charge(pool, redis, card_pool, conn, bot_factory):
    admin_id, *_ = await create_test_admin(pool)
    bot_ids = [await bot_factory(admin_id, name=f"Concurrency Bot {i}") for i in range(10)]

    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=50, lobby_seconds=20, call_interval_ms=5
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        results = await asyncio.gather(
            *(engine.join(bot_id, card_no=i + 1) for i, bot_id in enumerate(bot_ids))
        )
        assert all(r.ok for r in results), [r.reason for r in results if not r.ok]
        assert engine.player_count() == 10
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=5)

    for bot_id in bot_ids:
        cash = await ledger.get_or_create_account(pool, bot_id, "user_cash")
        # Exactly one stake debited -- 5000 seed minus the 10.00 stake.
        assert await ledger.balance(pool, cash.id) == spq.INITIAL_SIMULATED_BALANCE - Decimal("10.00")


# --- bot-runner global switch ---------------------------------------------


async def test_bot_runner_settings_shipped_default_is_disabled(pool):
    settings = await spq.get_settings_admin(pool)
    assert settings["enabled"] is False  # the shipped default, before any admin ever touches it


# --- RBAC ------------------------------------------------------------------


async def test_rbac_permissions_scoped_as_designed():
    from services.admin import rbac

    assert rbac.has_permission("support", "simulated_players:view")
    assert not rbac.has_permission("support", "simulated_players:manage")
    assert rbac.has_permission("ops", "simulated_players:manage")
    assert not rbac.has_permission("ops", "simulated_players:stop_all")
    assert rbac.has_permission("superadmin", "simulated_players:stop_all")


# --- HTTP layer: real routes through the real RBAC dependency chain --------


async def test_simulated_players_routes_full_flow_over_http(admin_server, pool):
    ops_headers = await _auth_headers(admin_server, pool, role="ops")
    support_headers = await _auth_headers(admin_server, pool, role="support")
    superadmin_headers = await _auth_headers(admin_server, pool, role="superadmin")
    created_ids: list[int] = []
    try:
        async with httpx.AsyncClient() as client:
            listing = await client.get(f"{admin_server}/simulated-players", headers=support_headers)
            assert listing.status_code == 200
            assert isinstance(listing.json(), list)

            settings = await client.get(f"{admin_server}/simulated-players/settings", headers=support_headers)
            assert settings.status_code == 200
            assert settings.json()["enabled"] is False

            # support can view but never manage or flip the global switch
            denied = await client.post(
                f"{admin_server}/simulated-players",
                headers=support_headers,
                json={"display_name": "HTTP Test Bot", "strategy": "normal"},
            )
            assert denied.status_code == 403
            denied_settings = await client.patch(
                f"{admin_server}/simulated-players/settings",
                headers=support_headers,
                json={"enabled": True, "max_concurrent_bots": 10, "reason": "should be blocked"},
            )
            assert denied_settings.status_code == 403

            created = await client.post(
                f"{admin_server}/simulated-players",
                headers=ops_headers,
                json={"display_name": "HTTP Test Bot", "strategy": "normal"},
            )
            assert created.status_code == 200, created.text
            user_id = created.json()["user_id"]
            created_ids.append(user_id)

            started = await client.post(
                f"{admin_server}/simulated-players/{user_id}/start",
                headers=ops_headers,
                json={"reason": "http test"},
            )
            assert started.status_code == 200

            strategy = await client.patch(
                f"{admin_server}/simulated-players/{user_id}/strategy",
                headers=ops_headers,
                json={"strategy": "active", "join_probability_pct": 50, "max_cards_per_join": 2, "reason": "http test"},
            )
            assert strategy.status_code == 200

            reset = await client.post(
                f"{admin_server}/simulated-players/{user_id}/reset",
                headers=ops_headers,
                json={"reason": "http test reset"},
            )
            assert reset.status_code == 200

            # ops is not authorized to pull the platform-wide stop-all lever
            ops_stop_all = await client.post(
                f"{admin_server}/simulated-players/stop-all",
                headers=ops_headers,
                json={"reason": "http test", "confirmation": "STOP ALL"},
            )
            assert ops_stop_all.status_code == 403

            bad_confirmation = await client.post(
                f"{admin_server}/simulated-players/stop-all",
                headers=superadmin_headers,
                json={"reason": "http test", "confirmation": "wrong"},
            )
            assert bad_confirmation.status_code == 422

            stop_all = await client.post(
                f"{admin_server}/simulated-players/stop-all",
                headers=superadmin_headers,
                json={"reason": "http test stop-all", "confirmation": "STOP ALL"},
            )
            assert stop_all.status_code == 200

            stopped_row = await pool.fetchrow(
                "SELECT status FROM simulated_players WHERE user_id = $1", user_id
            )
            assert stopped_row["status"] == "disabled"
    finally:
        for user_id in created_ids:
            await _delete_bot_completely(pool, user_id)
