"""Keno engine worker process -- the real production entrypoint that was
missing entirely until now (a spec-compliance audit's own top-line
finding: "the Keno round engine is never wired into any deployed
process"; KenoRoundEngine.run_forever() was only ever invoked from test
code). Mirrors services/engine/worker.py's own shape one level down:
Keno has exactly one global round cycle, not one engine per room, so
there's no room-claiming layer to build -- just start, recover, and keep
KenoRoundEngine.run_forever() running until told to stop.

Single-owner enforcement (spec 5.2, "exactly one worker may advance a
round") is already handled inside run_forever() itself via the Redis
lock in services/engine/keno_lock.py -- a second replica of this same
process calling run_forever() gets acquired=False back immediately and
sits in this file's own retry-with-backoff loop below, ready to take
over the moment the current owner releases the lock (a clean stop, or a
crash), rather than ever running two round cycles at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from packages.core import metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.logging import configure_logging
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.engine.keno_round_engine import KenoRoundEngine

logger = structlog.get_logger()

METRICS_PORT = 8008

# How long to wait before retrying run_forever() after it returns
# (lock not acquired -- another replica already owns it) or raises (a
# genuine failure). Not tied to any round-specific timing, same
# reasoning as engine/worker.py's own CLAIM_POLL_INTERVAL_SECONDS:
# short enough that a newly-released lock is picked up quickly, long
# enough not to hammer Redis with acquire attempts from an idle standby
# replica.
RETRY_INTERVAL_SECONDS = 5


def main() -> None:
    """Real production entrypoint: on boot, KenoRoundEngine.run_forever()
    itself runs crash recovery (recover_on_startup(), Part 5.3) before
    resuming the round cycle. If run_forever() ever returns or raises --
    the lock was lost, this replica never held it, or a genuine error
    escaped the round loop -- this outer loop retries after a short
    backoff rather than letting the whole process (and every future
    round) die silently, until the container runtime sends
    SIGTERM/SIGINT, at which point the in-flight round gets a real
    chance to stop cleanly (engine.stop() lets the current round finish
    its current phase and release the lock, never abandoning it
    mid-transaction).
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing("keno-engine-worker", settings.otel_exporter_endpoint)

    async def _run() -> None:
        pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
        redis = get_redis()
        engine = KenoRoundEngine(pool, redis)
        metrics_runner = await metrics.start_metrics_server(METRICS_PORT)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _handle_stop_signal() -> None:
            # Both, not just stop_event: while a round is in progress,
            # this whole coroutine is blocked awaiting engine.run_forever()
            # itself, which only ever returns once its *own* internal
            # while loop notices self._stop_requested -- set only by
            # engine.stop(). Setting stop_event alone (a real bug this
            # session's own live smoke test caught: the process needed
            # SIGKILL to actually exit, and Redis's keno:round:lock was
            # left held afterward, never released) would sit unnoticed
            # until run_forever() happened to return on its own, which
            # under real production timings (a 45s round cycle, not this
            # test config's 1s one) could be tens of seconds away.
            engine.stop()
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_stop_signal)

        try:
            while not stop_event.is_set():
                try:
                    acquired = await engine.run_forever()
                    if not acquired:
                        logger.info("keno_engine_worker_lock_held_elsewhere")
                except Exception:
                    # Mirrors engine/worker.py's own per-iteration
                    # isolation: a transient failure (a pool.acquire()
                    # timeout, a Postgres/Redis blip) must not kill this
                    # whole process -- the next iteration re-acquires the
                    # lock and resumes via recover_on_startup(), same as
                    # a fresh process boot would.
                    logger.exception("keno_engine_worker_run_forever_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=RETRY_INTERVAL_SECONDS)
        finally:
            engine.stop()
            await metrics_runner.cleanup()
            await redis.aclose()
            await pool.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
