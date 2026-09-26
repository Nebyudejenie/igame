"""A bonus's playthrough must be real, at-risk play (platform audit,
2026-09-25). wagering_progress_for_user_since() summed every Bingo stake
since the grant and never subtracted refunds, so taking and dropping a card,
or sitting alone in a lobby that is refunded for being underfilled, cleared
a bonus at zero risk -- and a cleared bonus converts to withdrawable cash.
Real Bingo engine, real sweep.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import ledger
from packages.core.bonuses import grant_bonus
from services.engine.round_engine import RoundEngine, load_room_config
from services.payments.bonus_sweep import sweep_bonus_wagering
from tests.integration.conftest import create_funded_user, create_room

pytestmark = pytest.mark.asyncio


async def _grant(conn: asyncpg.Connection, user_id: int, wagering_required: str) -> int:
    bonus = await grant_bonus(
        conn, user_id=user_id, idempotency_key=f"test-wagering-{uuid.uuid4()}", amount=Decimal("50.00"),
        wagering_required=Decimal(wagering_required),
    )
    return bonus.id


async def _bonus(conn: asyncpg.Connection, bonus_id: int) -> tuple[str, Decimal]:
    row = await conn.fetchrow("SELECT status, wagering_progress FROM bonuses WHERE id = $1", bonus_id)
    return row["status"], row["wagering_progress"]


async def test_taking_and_dropping_bingo_cards_does_not_clear_a_bonus(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(
        pool, await create_room(conn, stake=Decimal("20.00"), max_cards_per_player=4, lobby_seconds=60)
    )
    user_id = await create_funded_user(conn, Decimal("100.00"))
    bonus_id = await _grant(conn, user_id, "60.00")

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        for card_no in (1, 2, 3):  # 60 staked, all 60 given back
            assert (await engine.join(user_id, card_no)).ok
            assert (await engine.drop_card(user_id, card_no)).ok
        assert (await engine.join(user_id, 4)).ok  # one real, at-risk 20
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)

    await sweep_bonus_wagering(pool, redis)
    assert await _bonus(conn, bonus_id) == ("active", Decimal("20.00"))


async def test_an_underfilled_lobby_refund_does_not_count_as_wagering(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(
        pool, await create_room(conn, stake=Decimal("20.00"), min_players=2, max_cards_per_player=4, lobby_seconds=2)
    )
    user_id = await create_funded_user(conn, Decimal("100.00"))
    bonus_id = await _grant(conn, user_id, "60.00")

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        for card_no in (1, 2, 3):
            assert (await engine.join(user_id, card_no)).ok
        for _ in range(200):
            if await conn.fetchval(
                "SELECT count(*) FROM ledger_transactions WHERE idempotency_key LIKE $1", f"refund-%-{user_id}-%"
            ) == 3:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the underfilled lobby was never refunded")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)

    await sweep_bonus_wagering(pool, redis)
    assert await _bonus(conn, bonus_id) == ("active", Decimal("0.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("100.00")


async def test_real_wagering_still_clears_a_bonus(pool: asyncpg.Pool, redis, conn: asyncpg.Connection) -> None:
    user_id = await create_funded_user(conn, Decimal("100.00"))
    bonus_id = await _grant(conn, user_id, "30.00")
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-30")), ledger.Entry(pot.id, Decimal("30"))],
        idempotency_key=f"test-real-stake-{uuid.uuid4()}", created_by="test",
    )

    await sweep_bonus_wagering(pool, redis)
    assert (await _bonus(conn, bonus_id))[0] == "converted"
