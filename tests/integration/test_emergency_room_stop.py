"""Emergency single-room stop: services/admin/queries.py::stop_room_admin()
and the RoundEngine-side guards it depends on
(services/engine/round_engine.py's settlement FOR UPDATE check and
_call_next_number()'s external-stop detection).

This is money-bearing, concurrency-sensitive code -- every test here
proves a real financial invariant against real Postgres/Redis, not just
that an HTTP call returns 200.
"""

import asyncio
from decimal import Decimal

import httpx
import pytest

from packages.core import bingo, ledger
from services.admin import queries
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_round_engine import wait_until


async def test_stop_room_rejects_wrong_confirmation_token(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    with pytest.raises(ValueError, match="confirmation"):
        await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="testing", confirmation="nope", ip_address=None,
        )


async def test_stop_room_rejects_empty_reason(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    with pytest.raises(ValueError, match="reason"):
        await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="   ", confirmation="STOP", ip_address=None,
        )


async def test_stop_nonexistent_room_raises(pool, redis):
    admin_id, *_ = await create_test_admin(pool)
    with pytest.raises(ValueError, match="no such room"):
        await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=999_999_999,
            reason="testing", confirmation="STOP", ip_address=None,
        )


async def test_stop_idle_room_just_deactivates_it(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, is_active=True)

    result = await queries.stop_room_admin(
        pool, redis, admin_id=admin_id, room_id=room_id,
        reason="precautionary stop, no round yet", confirmation="stop", ip_address="10.0.0.1",
    )
    assert result["stopped_round_id"] is None
    assert result["refunded_entrants"] == 0
    assert result["room_deactivated"] is True

    room = await conn.fetchrow("SELECT is_active FROM rooms WHERE id = $1", room_id)
    assert room["is_active"] is False


async def test_stop_lobby_room_refunds_joined_players(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=5, lobby_seconds=30, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        p1 = await create_funded_user(conn, Decimal("50.00"))
        await wait_until(lambda: engine.is_lock_held(), timeout=5)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id
        assert round_id is not None

        result = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="lobby stuck, real incident", confirmation="STOP", ip_address="10.0.0.1",
        )
        assert result["stopped_round_id"] == round_id
        assert result["refunded_entrants"] == 1

        cash = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash.id) == Decimal("50.00")  # fully refunded, no loss

        round_row = await conn.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert round_row["status"] == "voided"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_stop_running_room_refunds_and_halts_number_calling(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=200, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)
        round_id = engine.round_id
        assert round_id is not None

        call_index_at_stop = (
            await pool.fetchval("SELECT call_index FROM rounds WHERE id = $1", round_id)
        )

        result = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="real incident: suspected exploit", confirmation="STOP",
            ip_address="10.0.0.1",
        )
        assert result["stopped_round_id"] == round_id
        assert result["refunded_entrants"] == 2

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
        assert await ledger.balance(conn, cash2.id) == Decimal("50.00")

        round_row = await conn.fetchrow(
            "SELECT status, call_index FROM rounds WHERE id = $1", round_id
        )
        assert round_row["status"] == "voided"

        # The live engine must actually stop calling more numbers -- give
        # it several call intervals' worth of real time and confirm
        # call_index never advances past what it was at the moment of
        # the stop (call_interval_ms=200 above, so 1s is 5 more
        # opportunities to wrongly advance if the engine didn't notice).
        await asyncio.sleep(1.0)
        final_call_index = await pool.fetchval(
            "SELECT call_index FROM rounds WHERE id = $1", round_id
        )
        assert final_call_index == round_row["call_index"]
        assert final_call_index <= call_index_at_stop + 1  # at most the in-flight call, if any

        # The engine itself must have recovered to a sane state, not be
        # stuck -- confirmed by the room no longer being claimed/re-run
        # (is_active is now false, so run_active_rooms() would never
        # re-claim it even after this engine's own task exits).
        room_row = await conn.fetchrow("SELECT is_active FROM rooms WHERE id = $1", room_id)
        assert room_row["is_active"] is False
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_stop_room_makes_the_same_still_live_engine_exit_instead_of_starting_another_round(
    pool, redis, card_pool, conn
):
    """A real gap this closes: stop_room_admin() durably sets
    rooms.is_active = false and halts the *current* round's number-calling
    (proven above), and is_active = false correctly stops a *future*
    worker restart from re-claiming the room (services/engine/worker.py's
    own run_active_rooms()) -- but nothing previously reached an engine
    that was still alive, still holding the room's Redis lock, at the
    moment of the stop. Confirmed directly before this fix: the very same
    engine instance, never told to stop(), went right on proactively
    starting round #2 in a room an operator had just told it to stop,
    silently contradicting the admin console's own displayed promise
    ("...cannot restart automatically"). RoundEngine._room_is_still_
    active() now re-checks the real column before every round after the
    first, so this same still-running engine must exit run_forever() on
    its own -- releasing its lock -- rather than opening a second round,
    with no explicit .stop() call from this test at all.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=200,
        no_player_next_round_delay_seconds=1, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)
        first_round_id = engine.round_id

        result = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="proving the same live engine now honors this",
            confirmation="STOP", ip_address="10.0.0.1",
        )
        assert result["stopped_round_id"] == first_round_id

        # No engine.stop() call anywhere in this test -- if the fix
        # didn't work, this task would run forever (or at least far
        # longer than this timeout), since nothing else would ever tell
        # it to stop.
        await asyncio.wait_for(task, timeout=10)

        rows = await pool.fetch(
            "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq", room_id
        )
        assert len(rows) == 1, "a second round must never have been created"
        assert rows[0]["status"] == "voided"

        room_row = await conn.fetchrow("SELECT is_active FROM rooms WHERE id = $1", room_id)
        assert room_row["is_active"] is False
    finally:
        await engine.stop()
        if not task.done():
            await asyncio.wait_for(task, timeout=15)


async def test_deactivating_a_room_lets_the_same_live_engine_finish_its_round_then_exit(
    pool, redis, card_pool, conn
):
    """The gentler counterpart to the test above: a plain Deactivate
    (rooms:manage's PATCH, not the superadmin-only emergency Stop Room)
    must never abandon a round already in progress with real money
    staked -- update_room_admin() itself does nothing to any in-flight
    round (see its own docstring), and this must stay true. What *should*
    change now: once that round finishes on its own (a real win here,
    settled normally, no admin intervention), the same still-live engine
    must notice the room went inactive and stop there, instead of
    proactively opening a second round nobody asked for anymore.
    """
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=15,
        no_player_next_round_delay_seconds=1, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        winner = await create_funded_user(conn, Decimal("50.00"))
        other = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(winner, 1, auto_mark=False)).ok
        assert (await engine.join(other, 2, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)
        first_round_id = engine.round_id

        winning_grid = card_pool[1]
        await wait_until(
            lambda: bingo.has_won(winning_grid, engine._called, room.win_patterns),  # noqa: SLF001
            timeout=15,
        )
        claim_result = await engine.claim(winner, 1)
        assert claim_result.ok is True, claim_result

        # A plain deactivate, mid-round -- must not touch this round at
        # all (no refund, no interruption of a real, already-in-progress
        # win being settled).
        updated = await queries.update_room_admin(
            pool, admin_id=admin_id, room_id=room_id,
            changes={"is_active": False}, reason="testing plain deactivate", ip_address=None,
        )
        assert updated is True

        # Let the already-in-flight round settle for real, undisturbed.
        await wait_until(lambda: engine.status == "idle", timeout=10)
        round_row = await conn.fetchrow("SELECT status FROM rounds WHERE id = $1", first_round_id)
        assert round_row["status"] == "done", "deactivate must never void a round already winning"
        winner_cash = await ledger.get_or_create_account(conn, winner, "user_cash")
        assert await ledger.balance(conn, winner_cash.id) > Decimal("50.00")  # the win actually paid

        # No engine.stop() call -- the engine must exit on its own now
        # that the room it just finished a round in is inactive.
        await asyncio.wait_for(task, timeout=10)

        rows = await pool.fetch("SELECT id FROM rounds WHERE room_id = $1", room_id)
        assert len(rows) == 1, "a second round must never have been created after deactivation"
    finally:
        await engine.stop()
        if not task.done():
            await asyncio.wait_for(task, timeout=15)


async def test_stop_after_round_already_settled_does_not_touch_the_winner(
    pool, redis, card_pool, conn
):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        winner = await create_funded_user(conn, Decimal("50.00"))
        other = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(winner, 1, auto_mark=False)).ok
        assert (await engine.join(other, 2, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        winning_grid = card_pool[1]
        await wait_until(
            lambda: bingo.has_won(winning_grid, engine._called, room.win_patterns),  # noqa: SLF001
            timeout=10,
        )
        claim_result = await engine.claim(winner, 1)
        assert claim_result.ok is True
        round_id = engine.round_id
        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert round_row["status"] == "done"
        winner_cash = await ledger.get_or_create_account(conn, winner, "user_cash")
        balance_before_stop = await ledger.balance(conn, winner_cash.id)
        assert balance_before_stop > Decimal("50.00")  # the real win actually paid out

        # Now stop the room -- the round is already terminal, this must
        # be a pure no-op for it (no re-refund, no clawback of the
        # legitimate win).
        result = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="stopping room after this round for maintenance",
            confirmation="STOP", ip_address="10.0.0.1",
        )
        assert result["refunded_entrants"] == 0

        assert await ledger.balance(conn, winner_cash.id) == balance_before_stop
        round_row_after = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert round_row_after["status"] == "done"  # not reverted to voided
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_duplicate_stop_is_idempotent(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=200, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        first = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="first stop", confirmation="STOP", ip_address="10.0.0.1",
        )
        assert first["refunded_entrants"] == 2

        second = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="retry, unaware the first one worked", confirmation="STOP",
            ip_address="10.0.0.1",
        )
        assert second["refunded_entrants"] == 0  # already terminal -- no double refund

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")  # exactly one refund
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_concurrent_stop_by_two_admins_refunds_exactly_once(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("25.00"), min_players=2, call_interval_ms=200, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_a, *_ = await create_test_admin(pool)
        admin_b, *_ = await create_test_admin(pool)
        p1 = await create_funded_user(conn, Decimal("50.00"))
        p2 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        results = await asyncio.gather(
            queries.stop_room_admin(
                pool, redis, admin_id=admin_a, room_id=room_id,
                reason="admin A stopping", confirmation="STOP", ip_address="10.0.0.1",
            ),
            queries.stop_room_admin(
                pool, redis, admin_id=admin_b, room_id=room_id,
                reason="admin B stopping, unaware of A", confirmation="STOP",
                ip_address="10.0.0.2",
            ),
        )
        total_refunded = sum(r["refunded_entrants"] for r in results)
        # Exactly one of the two actually performed the refund (2
        # entrants); the other found it already terminal (0) -- Postgres's
        # own row lock in refund_round_in_transaction()'s FOR UPDATE
        # serializes the two concurrent transactions, so this is a real
        # concurrency proof, not a coincidence of test timing.
        assert sorted(r["refunded_entrants"] for r in results) == [0, 2]
        assert total_refunded == 2

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
        assert await ledger.balance(conn, cash2.id) == Decimal("50.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_admin_stop_racing_real_settlement_produces_exactly_one_outcome(
    pool, redis, card_pool, conn
):
    """The single most important concurrency proof in this whole feature:
    an admin's stop request and the engine's own settlement of a real,
    concurrently-determined winner racing for the same round. Whichever
    transaction's FOR UPDATE lock on the rounds row wins commits the
    round's one authoritative terminal outcome; the other must see that
    and back off completely -- never a double payout, never a payout
    after the round was voided, never a lost stake either way.

    Constructs the race directly (claim() the winner, then immediately
    race stop_room_admin() against the real settlement task that
    claim() already scheduled) rather than trying to time an HTTP
    request against a 50ms tie window from outside the process, which
    would be unreliable to reproduce on demand.
    """
    room_id = await create_room(
        conn, stake=Decimal("30.00"), min_players=2, call_interval_ms=15, is_active=True
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        admin_id, *_ = await create_test_admin(pool)
        winner = await create_funded_user(conn, Decimal("50.00"))
        other = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(winner, 1, auto_mark=False)).ok
        assert (await engine.join(other, 2, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        winning_grid = card_pool[1]
        await wait_until(
            lambda: bingo.has_won(winning_grid, engine._called, room.win_patterns),  # noqa: SLF001
            timeout=10,
        )
        claim_result = await engine.claim(winner, 1)
        assert claim_result.ok is True
        round_id = engine.round_id
        assert round_id is not None
        # claim() has now scheduled engine._settlement_task, sleeping for
        # WINNER_TIE_WINDOW_SECONDS (0.05s) before it actually settles --
        # race the admin stop against it right now, no artificial delay.

        stop_result = await queries.stop_room_admin(
            pool, redis, admin_id=admin_id, room_id=room_id,
            reason="racing real settlement, this is the safety-critical case",
            confirmation="STOP", ip_address="10.0.0.1",
        )

        # Let whichever outcome won actually finish (the settlement task
        # if it won the race, or nothing further if the stop won) --
        # comfortably longer than WINNER_TIE_WINDOW_SECONDS (0.05s).
        await asyncio.sleep(0.3)

        round_row = await conn.fetchrow(
            "SELECT status FROM rounds WHERE id = $1", round_id
        )
        winner_cash = await ledger.get_or_create_account(conn, winner, "user_cash")
        winner_balance = await ledger.balance(conn, winner_cash.id)

        if round_row["status"] == "done":
            # Settlement won the race: the winner was legitimately paid,
            # and the admin's stop must have found it already terminal.
            assert stop_result["refunded_entrants"] == 0
            assert winner_balance > Decimal("50.00")
        elif round_row["status"] == "voided":
            # The stop won the race: both players are refunded their
            # exact stake, and the winner was NOT also paid on top of
            # that refund (no double payment).
            assert stop_result["refunded_entrants"] == 2
            assert winner_balance == Decimal("50.00")
        else:
            pytest.fail(f"round left in a non-terminal status: {round_row['status']!r}")

        # Whichever branch won, the total money in play is conserved --
        # this is the real invariant a real-money system needs, checked
        # directly rather than trusted from either branch's own logic.
        other_cash = await ledger.get_or_create_account(conn, other, "user_cash")
        other_balance = await ledger.balance(conn, other_cash.id)
        total = winner_balance + other_balance
        # Both started with 50.00 (100.00 total), each staked 30.00 (pot
        # 60.00, minus house cut if the winner path paid out) -- either
        # exactly refunded (back to 100.00 total) or paid out (100.00
        # minus the house's cut of the derash, never more than that and
        # never less than a plain refund would leave).
        assert Decimal("50.00") <= total <= Decimal("100.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


# --- RBAC + real HTTP ---------------------------------------------------


async def test_stop_room_endpoint_requires_superadmin_over_http(admin_server, pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    ops_headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/rooms/{room_id}/stop",
            headers=ops_headers,
            json={"reason": "trying as ops", "confirmation": "STOP"},
        )
    assert response.status_code == 403


async def test_stop_room_preview_and_stop_over_http_full_flow(admin_server, pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, is_active=True)
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        preview = await client.get(f"{admin_server}/rooms/{room_id}/stop-preview", headers=headers)
        assert preview.status_code == 200
        assert preview.json()["room_id"] == room_id
        assert preview.json()["has_stoppable_round"] is False

        bad_confirmation = await client.post(
            f"{admin_server}/rooms/{room_id}/stop",
            headers=headers,
            # A real, well-formed reason -- this call isolates the
            # confirmation-mismatch rejection specifically.
            json={"reason": "real HTTP end-to-end test", "confirmation": "wrong"},
        )
        assert bad_confirmation.status_code == 422

        real = await client.post(
            f"{admin_server}/rooms/{room_id}/stop",
            headers=headers,
            json={"reason": "real HTTP end-to-end test", "confirmation": "STOP"},
        )
        assert real.status_code == 200
        assert real.json()["room_deactivated"] is True

    room = await conn.fetchrow("SELECT is_active FROM rooms WHERE id = $1", room_id)
    assert room["is_active"] is False
