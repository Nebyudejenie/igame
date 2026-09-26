"""Ledger idempotency keys are one global namespace (ledger_transactions.
idempotency_key is UNIQUE across every kind), and ledger.post() treats an
existing key as "already done". Found by the 2026-09-25 platform audit and
reproduced here: the Keno ticket endpoint passed the player's own
client-chosen idempotency_key straight into that namespace. A player could
reuse any existing key to get a ticket with no stake taken, or claim a key
some other operation hasn't posted yet (a Bingo settlement, a deposit) so
that operation silently moves no money.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno_tickets, ledger
from tests.integration.conftest import create_funded_user
from tests.integration.test_keno_autoplay import _seed_keno_round

pytestmark = pytest.mark.asyncio


async def _cash(conn: asyncpg.Connection, user_id: int) -> Decimal:
    return await ledger.balance(conn, (await ledger.get_or_create_account(conn, user_id, "user_cash")).id)


async def _ticket(pool, user_id: int, key: str) -> keno_tickets.PlacedTicket:
    return await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=key
    )


async def test_a_keno_ticket_cannot_reuse_another_operations_ledger_key_to_bet_for_free(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    victim = await create_funded_user(conn, Decimal("100.00"))
    attacker = await create_funded_user(conn, Decimal("0.00"))
    cash = await ledger.get_or_create_account(conn, victim, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    someone_elses_key = f"stake-{uuid.uuid4()}"
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-20")), ledger.Entry(pot.id, Decimal("20"))],
        idempotency_key=someone_elses_key, created_by="test",
    )

    with pytest.raises(keno_tickets.InsufficientBalance):
        await _ticket(pool, attacker, someone_elses_key)
    assert await conn.fetchval("SELECT count(*) FROM keno_tickets WHERE user_id = $1", attacker) == 0


async def test_a_keno_ticket_cannot_claim_a_key_another_operation_will_post_later(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """Shaped like Bingo settlement's own key (settle-{round_id}); round
    ids are visible to every player in the room."""
    await _seed_keno_round(conn)
    attacker = await create_funded_user(conn, Decimal("100.00"))
    winner = await create_funded_user(conn, Decimal("0.00"))
    future_key = f"settle-{uuid.uuid4()}"
    await _ticket(pool, attacker, future_key)

    winner_cash = await ledger.get_or_create_account(conn, winner, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "payout", [ledger.Entry(pot.id, Decimal("-50")), ledger.Entry(winner_cash.id, Decimal("50"))],
        idempotency_key=future_key, created_by="test",
    )

    assert await _cash(conn, winner) == Decimal("50.00")


async def test_one_players_keno_key_never_returns_another_players_ticket(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    a = await create_funded_user(conn, Decimal("100.00"))
    b = await create_funded_user(conn, Decimal("100.00"))
    key = f"keno-{uuid.uuid4()}"
    ticket_a = await _ticket(pool, a, key)

    ticket_b = await _ticket(pool, b, key)

    assert ticket_b.user_id == b
    assert ticket_b.id != ticket_a.id
    assert await _cash(conn, b) == Decimal("90.00")


async def test_a_genuine_keno_retry_still_returns_the_original_ticket(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    user_id = await create_funded_user(conn, Decimal("100.00"))
    key = f"keno-{uuid.uuid4()}"
    first = await _ticket(pool, user_id, key)

    again = await _ticket(pool, user_id, key)

    assert (again.id, again.idempotency_key) == (first.id, key)
    assert await _cash(conn, user_id) == Decimal("90.00")


async def test_ledger_post_refuses_to_treat_a_different_operation_as_a_replay(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """Defence in depth for every caller, not just Keno: a key collision
    between two different operations must fail loudly, never report
    'already done' for money that never moved."""
    user_id = await create_funded_user(conn, Decimal("100.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    key = f"collide-{uuid.uuid4()}"
    original = await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-20")), ledger.Entry(pot.id, Decimal("20"))],
        idempotency_key=key, created_by="test",
    )

    replay = await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-20")), ledger.Entry(pot.id, Decimal("20"))],
        idempotency_key=key, created_by="test",
    )
    assert replay.id == original.id

    with pytest.raises(getattr(ledger, "IdempotencyKeyConflict", AssertionError)):
        await ledger.post(
            conn, "payout", [ledger.Entry(pot.id, Decimal("-20")), ledger.Entry(cash.id, Decimal("20"))],
            idempotency_key=key, created_by="test",
        )
    assert await _cash(conn, user_id) == Decimal("80.00")
