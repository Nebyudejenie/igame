"""Chaos: kill the Keno engine mid-round with real money already staked
by several real players, and prove a fresh engine's recover_on_startup()
brings the round to a correct, fully-settled terminal state with the
ledger reconciling -- the same property test_chaos_engine_crash.py
already proves for Bingo, but exercising Keno's own, deliberately
different recovery design.

Unlike Bingo (which always voids and refunds a crashed round), Keno's
recover_on_startup() *resumes* a crashed round rather than refunding it
whenever the round is younger than STUCK_ROUND_THRESHOLD_SECONDS (300s,
services/engine/keno_round_engine.py) -- the draw is a deterministic
commit-reveal function of data already persisted before betting even
closes, so there is no reason to give every ticket its stake back just
because the process that would have continued the round died. This test
proves that resume path is correct under a real crash, not simulated:
every ticket reaches a real terminal status (won or lost, never left
pending), and no money is created or destroyed across the crash
boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno_tickets, ledger
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from services.engine.keno_round_engine import KenoRoundEngine
from tests.integration.conftest import create_funded_user
from tests.integration.test_keno_round_engine import _seed_fast_config_and_tier

pytestmark = pytest.mark.load

PLAYERS = 40
STAKE = Decimal("10.00")
STARTING_BALANCE = Decimal("1000.00")


@pytest.fixture(autouse=True)
async def _clean_keno_lock_and_rounds(pool: asyncpg.Pool, redis):
    async def _clean() -> None:
        await redis.delete(KENO_LOCK_KEY)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed', 'failed', 'voided')"
            )

    await _clean()
    yield
    await _clean()


async def _wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.1) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def test_crashed_engine_with_40_staked_players_recovers_to_a_correct_settlement(pool, redis, conn):
    # Long-ish betting window so the crash genuinely lands mid-betting,
    # and so 40 concurrent place_ticket() calls (each serialized behind
    # the round row's own FOR UPDATE lock) have comfortable headroom to
    # all finish before the engine's own timer closes betting -- a real,
    # observed flake: at 3s, occasional DB/pool contention let a handful
    # of placements still be in flight when betting closed, raising
    # RoundNotAcceptingBets. draw_seconds/result_seconds stay fast so
    # recovery+completion doesn't itself take long once the fresh engine
    # resumes.
    await _seed_fast_config_and_tier(conn, betting_seconds=10, draw_seconds=1, result_seconds=1)

    doomed_engine = KenoRoundEngine(pool, redis, worker_id="chaos-doomed-keno-engine")
    task = asyncio.create_task(doomed_engine.run_forever())
    try:
        async def _round_is_betting_open() -> bool:
            async with pool.acquire() as c:
                row = await c.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
            return row is not None

        await _wait_until(_round_is_betting_open)
        async with pool.acquire() as c:
            round_row = await c.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
        round_id = round_row["id"]

        players = [await create_funded_user(conn, STARTING_BALANCE) for _ in range(PLAYERS)]
        tickets = await asyncio.gather(
            *(
                keno_tickets.place_ticket(
                    pool, redis=redis, user_id=user_id, picks=[(i % 80) + 1], stake=STAKE,
                    idempotency_key=f"chaos-ticket-{uuid.uuid4()}",
                )
                for i, user_id in enumerate(players)
            )
        )
        assert all(t.round_id == round_id for t in tickets)

        stake_total_before_crash = await pool.fetchval("SELECT total_stake FROM keno_rounds WHERE id = $1", round_id)
        assert stake_total_before_crash == STAKE * PLAYERS

        # The crash: no graceful shutdown, task just dies mid-betting-phase
        # -- the room lock is never released the way a clean stop() would,
        # so a fresh worker sees it as stale/gone the same way it would
        # after a real process kill.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        # Whatever happened above -- including the RoundNotAcceptingBets
        # race described near betting_seconds, which used to raise before
        # the deliberate task.cancel() a few lines up ever ran -- the
        # doomed engine must never be left running unsupervised. Orphaned,
        # it keeps creating and settling its own real rounds against the
        # shared keno_reserve for the rest of the pytest session, silently
        # corrupting whatever runs after this test (first traced via a
        # downstream reserve-balance mismatch in
        # test_keno_load_concurrent_tickets.py).
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
    await redis.delete(KENO_LOCK_KEY)

    recovery_engine = KenoRoundEngine(pool, redis, worker_id="chaos-recovery-keno-engine")
    recovery_task = asyncio.create_task(recovery_engine.run_forever())
    try:
        async def _round_is_terminal() -> bool:
            async with pool.acquire() as c:
                status = await c.fetchval("SELECT status FROM keno_rounds WHERE id = $1", round_id)
            return status in ("completed", "failed", "voided")

        await _wait_until(_round_is_terminal, timeout=30.0)

        final_status = await pool.fetchval("SELECT status FROM keno_rounds WHERE id = $1", round_id)
        # A round this fresh (well under the 300s stuck-round threshold)
        # must be *resumed*, not refunded -- Keno's own, deliberately
        # different-from-Bingo recovery design (see this file's own
        # module docstring). 'voided'/'failed' here would mean recovery
        # took the wrong path for a crash this recent.
        assert final_status == "completed", f"expected a resumed, real settlement, got {final_status!r}"

        # Every ticket reached a real terminal status -- never left
        # dangling as 'pending' by the crash.
        ticket_statuses = await pool.fetch(
            "SELECT status FROM keno_tickets WHERE round_id = $1", round_id
        )
        assert len(ticket_statuses) == PLAYERS
        assert all(row["status"] in ("won", "lost") for row in ticket_statuses), (
            f"a ticket was left non-terminal after recovery: "
            f"{[row['status'] for row in ticket_statuses if row['status'] not in ('won', 'lost')]}"
        )

        # No money created or destroyed across the crash boundary: total
        # player payout given out must exactly equal total_payout on the
        # round row, and the ledger as a whole still balances.
        total_payout_from_tickets = await pool.fetchval(
            "SELECT COALESCE(SUM(payout), 0) FROM keno_tickets WHERE round_id = $1", round_id
        )
        round_total_payout = await pool.fetchval("SELECT total_payout FROM keno_rounds WHERE id = $1", round_id)
        assert total_payout_from_tickets == round_total_payout

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        recovery_engine.stop()
        await asyncio.wait_for(recovery_task, timeout=20.0)
