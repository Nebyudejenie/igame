"""Chaos: the actual Redis container goes away and comes back mid-round,
for Keno's own engine -- the same real (not simulated) infrastructure
failure test_chaos_redis_restart.py already proves for Bingo's
RoundEngine, exercised against KenoRoundEngine/KenoRoundLock instead.

Restarts the shared docker-compose Redis container -- reversible, no
data anything depends on is expected to survive it (packages/core/
redis_conn.py's own docstring: "if Redis is wiped, the platform must
recover fully from Postgres"). Marked chaos_infra, not load, for the
identical reason test_chaos_redis_restart.py's own file docstring
already documents: it breaks the shared session-scoped `redis` fixture
for every test that runs after it in the same pytest process. Always run
alone: `pytest -m chaos_infra`.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from packages.core import keno_tickets, ledger
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from services.engine.keno_round_engine import KenoRoundEngine
from tests.integration.conftest import create_funded_user
from tests.integration.test_keno_round_engine import _seed_fast_config_and_tier

pytestmark = pytest.mark.chaos_infra

COMPOSE_FILE = Path(__file__).resolve().parent.parent.parent / "deploy" / "docker-compose.yml"


async def _wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.1) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def _run_compose(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", str(COMPOSE_FILE), *args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0


async def _wait_redis_ready() -> None:
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    from packages.core.config import get_settings

    settings = get_settings()
    for _ in range(100):
        probe = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await probe.ping()
            await probe.aclose()
            return
        except RedisConnectionError:
            await probe.aclose()
            await asyncio.sleep(0.1)
    raise RuntimeError("redis did not become ready in time")


async def _outage_redis(down_seconds: float) -> float:
    """A plain `docker compose restart` comes back in well under a
    second on this stack -- fast enough that KenoRoundLock's own refresh
    loop (every 5s, up to a 10s retry-with-backoff margin before it
    actually gives up ownership) can silently succeed on its very next
    attempt without ever losing the lock at all. That's not a bug (a
    sub-second blip genuinely shouldn't need to kill and restart the
    whole engine), but it also doesn't exercise recovery -- a real
    "Redis is gone for a while" outage needs an actual stop/start gap
    comfortably longer than that 10s margin."""
    started = time.monotonic()
    await _run_compose("stop", "redis")
    await asyncio.sleep(down_seconds)
    await _run_compose("start", "redis")
    await _wait_redis_ready()
    return time.monotonic() - started


async def test_redis_restart_mid_round_recovers_cleanly(pool, conn):
    from packages.core.redis_conn import get_redis

    # betting_seconds comfortably outlasts the planned outage below, so
    # the round is still reliably in betting_open, not already mid-draw,
    # for the whole time Redis is down -- a controlled, predictable
    # scenario rather than a race against the engine's own round clock.
    await _seed_fast_config_and_tier(conn, betting_seconds=25, draw_seconds=1, result_seconds=1)

    engine_redis = get_redis()
    await engine_redis.delete(KENO_LOCK_KEY)
    engine = KenoRoundEngine(pool, engine_redis, worker_id="chaos-redis-keno-engine")
    task = asyncio.create_task(engine.run_forever())

    async def _round_is_betting_open() -> bool:
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
        return row is not None

    await _wait_until(_round_is_betting_open)
    async with pool.acquire() as c:
        round_row = await c.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
    round_id = round_row["id"]

    players = [await create_funded_user(conn, Decimal("100.00")) for _ in range(5)]
    for i, user_id in enumerate(players):
        ticket = await keno_tickets.place_ticket(
            pool, redis=engine_redis, user_id=user_id, picks=[i + 1], stake=Decimal("10"),
            idempotency_key=f"chaos-redis-ticket-{uuid.uuid4()}",
        )
        assert ticket.round_id == round_id

    # 15s down: comfortably past KenoRoundLock's own ~10s retry-with
    # -backoff give-up margin (LOCK_TTL_SECONDS - REFRESH_INTERVAL_
    # SECONDS), so this outage genuinely outlasts what the lock's own
    # refresh loop can silently ride out -- see _outage_redis()'s own
    # docstring for why a plain fast `restart` (sub-second) doesn't
    # actually exercise this path at all.
    outage_seconds = await _outage_redis(15)
    print(f"\n[chaos keno redis-outage] Redis was down for {outage_seconds:.1f}s")

    # The engine's own connection was broken for the whole outage -- it
    # should stop believing it owns the lock once its own give-up margin
    # elapses, not spin forever against what was, for a while, a Redis
    # with no memory of it.
    async def _lock_released_or_task_done() -> bool:
        return task.done() or not engine.is_lock_held()

    await _wait_until(_lock_released_or_task_done, timeout=30)

    if not task.done():
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
    await engine_redis.aclose()

    recovery_redis = get_redis()
    await recovery_redis.delete(KENO_LOCK_KEY)
    recovery_engine = KenoRoundEngine(pool, recovery_redis, worker_id="chaos-redis-recovery-keno-engine")
    recovery_task = asyncio.create_task(recovery_engine.run_forever())
    try:
        async def _round_is_terminal() -> bool:
            async with pool.acquire() as c:
                status = await c.fetchval("SELECT status FROM keno_rounds WHERE id = $1", round_id)
            return status in ("completed", "failed", "voided")

        await _wait_until(_round_is_terminal, timeout=30.0)

        final_status = await pool.fetchval("SELECT status FROM keno_rounds WHERE id = $1", round_id)
        assert final_status == "completed", f"expected a resumed, real settlement, got {final_status!r}"

        ticket_statuses = await pool.fetch("SELECT status FROM keno_tickets WHERE round_id = $1", round_id)
        assert len(ticket_statuses) == 5
        assert all(row["status"] in ("won", "lost") for row in ticket_statuses)

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        recovery_engine.stop()
        await asyncio.wait_for(recovery_task, timeout=20.0)
        await recovery_redis.aclose()
