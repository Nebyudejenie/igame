"""A Bingo round voided from outside the engine -- an admin void or
emergency stop, or the engine's own underfilled-lobby refund racing a
player -- while the engine's in-memory state still says 'lobby'
(platform audit, 2026-09-25). Three paths trusted that in-memory state
over the round's real status in Postgres, and each moved real money:

- drop_card() refunded a stake the void had already refunded;
- the lobby's transition to running flipped the voided round back to
  'running', so it played out and settlement paid winners from a pot that
  had already been refunded;
- join() took a new stake into the already-voided round, which nothing
  would ever refund.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest

from packages.core import ledger
from services.engine import refunds
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room

pytestmark = pytest.mark.asyncio


async def _cash(conn: asyncpg.Connection, user_id: int) -> Decimal:
    return await ledger.balance(conn, (await ledger.get_or_create_account(conn, user_id, "user_cash")).id)


async def test_dropping_a_card_after_the_round_was_voided_does_not_refund_it_again(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(pool, await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=60))
    user_id = await create_funded_user(conn, Decimal("100.00"))
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(user_id, 1)).ok
        round_id = await conn.fetchval("SELECT round_id FROM round_entries WHERE user_id = $1", user_id)
        assert await refunds.refund_round(pool, round_id, reason="test_admin_void") == 1

        result = await engine.drop_card(user_id, 1)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    assert await _cash(conn, user_id) == Decimal("100.00")  # refunded once, not 120
    assert result.ok is False


async def test_joining_a_round_that_was_voided_is_refused(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(pool, await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=60))
    first = await create_funded_user(conn, Decimal("100.00"))
    late = await create_funded_user(conn, Decimal("100.00"))
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(first, 1)).ok
        round_id = await conn.fetchval("SELECT round_id FROM round_entries WHERE user_id = $1", first)
        await refunds.refund_round(pool, round_id, reason="test_admin_void")

        result = await engine.join(late, 2)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    assert await _cash(conn, late) == Decimal("100.00")  # nothing staked into a round no one will refund
    assert (result.ok, result.reason) == (False, "not_joinable")


async def test_a_voided_lobby_round_never_runs_or_pays_out(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(pool, await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=2))
    a = await create_funded_user(conn, Decimal("100.00"))
    b = await create_funded_user(conn, Decimal("100.00"))
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(a, 1)).ok
        assert (await engine.join(b, 2)).ok
        round_id = await conn.fetchval("SELECT round_id FROM round_entries WHERE user_id = $1", a)
        assert await refunds.refund_round(pool, round_id, reason="test_admin_void") == 2

        # Lobby closes at 2s; 75 calls at 20ms each and settlement would be
        # done well inside this window if the round were (wrongly) run.
        for _ in range(100):
            if await conn.fetchval("SELECT count(*) FROM rounds WHERE room_id = $1 AND id > $2", room.id, round_id):
                break  # the engine has moved on to a fresh round
            await asyncio.sleep(0.1)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    assert await conn.fetchval("SELECT status FROM rounds WHERE id = $1", round_id) == "voided"
    assert await conn.fetchval("SELECT count(*) FROM round_winners WHERE round_id = $1", round_id) == 0
    assert await _cash(conn, a) + await _cash(conn, b) == Decimal("200.00")
