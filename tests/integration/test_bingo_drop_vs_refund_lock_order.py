"""Lock-order sweep candidate (2026-09-26): a player drops a Bingo card at
the same moment the round is refunded (an underfilled lobby closing, or an
admin void). drop_card() inserts its refund's ledger_transactions row (a
FOR KEY SHARE on the rounds row, via round_id's foreign key), locks the
balance rows, and only then UPDATEs the rounds row. refund_round() takes
FOR UPDATE on the rounds row first. Whether a key-share holder upgrading
while a FOR UPDATE waiter is queued closes a cycle is Postgres behaviour,
so this tests it instead of reasoning about it. It also checks the stake
is returned exactly once, not by both paths.
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


async def test_dropping_a_card_while_the_round_is_refunded_neither_deadlocks_nor_double_refunds(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = await load_room_config(pool, await create_room(conn, stake=Decimal("20.00"), min_players=2, lobby_seconds=60))
    user_id = await create_funded_user(conn, Decimal("100.00"))
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    real_post = ledger.post
    drop_posted = asyncio.Event()
    release_drop = asyncio.Event()

    async def post_then_hold_the_drop(*args, **kwargs):
        txn = await real_post(*args, **kwargs)
        if str(kwargs.get("idempotency_key", "")).startswith("drop-"):
            drop_posted.set()
            await release_drop.wait()
        return txn

    try:
        assert (await engine.join(user_id, 1)).ok
        round_id = await conn.fetchval("SELECT round_id FROM round_entries WHERE user_id = $1", user_id)
        monkeypatch.setattr(ledger, "post", post_then_hold_the_drop)

        drop = asyncio.create_task(engine.drop_card(user_id, 1))
        await asyncio.wait_for(drop_posted.wait(), timeout=10)
        refund = asyncio.create_task(refunds.refund_round(pool, round_id, reason="test_void"))
        for _ in range(200):
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE '%FROM rounds WHERE id%FOR UPDATE%'"
            )
            if waiting:
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("the refund never queued behind the drop's lock on the round row")
        release_drop.set()

        drop_result, refunded = await asyncio.wait_for(asyncio.gather(drop, refund, return_exceptions=True), timeout=20)
        assert not isinstance(drop_result, BaseException), drop_result
        assert not isinstance(refunded, BaseException), refunded
        assert drop_result.ok
    finally:
        release_drop.set()
        monkeypatch.setattr(ledger, "post", real_post)
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("100.00")  # the 20 came back exactly once
    assert await conn.fetchval("SELECT status FROM rounds WHERE id = $1", round_id) == "voided"

