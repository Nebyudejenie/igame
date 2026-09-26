"""Two Keno placement races from the platform audit (2026-09-25),
reproduced against real Postgres:

- A double-tap: two requests with the same key at once. Both passed the
  replay lookup (it ran before the per-player lock), and the second died
  on the keno_tickets unique index as a raw 500.
- Stop pressed during autoplay placement: place_for_active_sessions()
  reads the session list first, then places one ticket per session. A
  player who pressed Stop in between was still charged for that round.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno_autoplay, keno_tickets, ledger
from tests.integration.conftest import create_funded_user
from tests.integration.test_keno_autoplay import _seed_keno_round

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_autoplay_sessions(pool: asyncpg.Pool):
    async def clean() -> None:
        await pool.execute(
            "UPDATE keno_autoplay_sessions SET status = 'stopped', stop_reason = 'test_cleanup', updated_at = now() "
            "WHERE status = 'active'"
        )

    await clean()
    yield
    await clean()


async def _cash(conn: asyncpg.Connection, user_id: int) -> Decimal:
    return await ledger.balance(conn, (await ledger.get_or_create_account(conn, user_id, "user_cash")).id)


async def test_a_double_tapped_ticket_is_placed_once_and_both_taps_get_it_back(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    user_id = await create_funded_user(conn, Decimal("100.00"))
    key = f"keno-{uuid.uuid4()}"

    first, second = await asyncio.gather(
        *(keno_tickets.place_ticket(pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"),
                                    idempotency_key=key) for _ in range(2)),
        return_exceptions=True,
    )

    assert not isinstance(first, BaseException), first
    assert not isinstance(second, BaseException), second
    assert first.id == second.id
    assert await _cash(conn, user_id) == Decimal("90.00")


async def test_pressing_stop_during_autoplay_placement_is_never_charged(
    pool: asyncpg.Pool, conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Stop is injected at the real window: after the round's session
    list was read, before this session's ticket transaction starts."""
    round_id = await _seed_keno_round(conn)
    user_id = await create_funded_user(conn, Decimal("100.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)
    real_place = keno_tickets.place_ticket

    async def player_presses_stop_first(*args, **kwargs):
        await keno_autoplay.stop_session(pool, user_id=user_id)
        return await real_place(*args, **kwargs)

    monkeypatch.setattr(keno_tickets, "place_ticket", player_presses_stop_first)

    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)  # type: ignore[arg-type]

    row = await conn.fetchrow("SELECT status, stop_reason FROM keno_autoplay_sessions WHERE id = $1", session.id)
    assert (row["status"], row["stop_reason"]) == ("stopped", "manual")  # the player's own reason survives
    assert await conn.fetchval("SELECT count(*) FROM keno_tickets WHERE autoplay_session_id = $1", session.id) == 0
    assert await _cash(conn, user_id) == Decimal("100.00")
