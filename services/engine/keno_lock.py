"""Single-owner election for the (one, global) Keno round stream, via Redis.

Deliberately a separate, independent class from services/engine/room_lock.py
rather than a reuse-with-a-fake-room-id: Bingo's RoomLock is parameterized
per room_id because Bingo genuinely has many concurrently-owned rooms;
Keno has exactly one continuous round stream, not a per-room concept at
all, so forcing it through room_lock.py's room_id-shaped API would be a
worse fit than this focused equivalent -- and per the project's own
"purely additive, don't touch Bingo files unless genuinely required"
rule, this reuses the identical, already-reviewed compare-and-clear Lua
-script safety pattern without importing or modifying anything in
services/engine/room_lock.py. See packages/core/keno.py's own docstring
for the same reasoning applied to its RNG primitive.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger()

LOCK_TTL_SECONDS = 15
REFRESH_INTERVAL_SECONDS = 5
LOCK_KEY = "keno:round:lock"

_REFRESH_IF_OWNER = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

_DELETE_IF_OWNER = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class KenoRoundLock:
    """Exactly one keno-engine-worker process may advance the Keno round
    stream at a time (build spec Part 5.2). Same safety shape as
    services/engine/room_lock.py's RoomLock: a Redis key with a TTL,
    refreshed periodically; a worker that stops refreshing loses
    ownership automatically; refresh/release both use compare-and-clear
    Lua scripts so a worker can never touch a lock it no longer owns."""

    def __init__(
        self,
        redis: Redis,
        worker_id: str | None = None,
        *,
        ttl_seconds: int = LOCK_TTL_SECONDS,
        refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._redis = redis
        self._worker_id = worker_id or str(uuid.uuid4())
        self._key = LOCK_KEY
        self._ttl_seconds = ttl_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._refresh_task: asyncio.Task[None] | None = None
        self._held = False
        self._last_refreshed_at = 0.0

    @property
    def key(self) -> str:
        return self._key

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def is_held(self) -> bool:
        """Best-effort local view, fresh to within one refresh interval --
        callers on the hot path should trust this rather than
        round-tripping to Redis on every check."""
        return self._held

    async def acquire(self) -> bool:
        ok = await self._redis.set(self._key, self._worker_id, nx=True, ex=self._ttl_seconds)
        self._held = bool(ok)
        if self._held:
            self._last_refreshed_at = asyncio.get_running_loop().time()
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        return self._held

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_seconds)
            try:
                refreshed = await self._redis.eval(_REFRESH_IF_OWNER, 1, self._key, self._worker_id, self._ttl_seconds)
            except Exception:
                # Same recoverable-blip-vs-real-outage distinction as
                # RoomLock's own _refresh_loop: only give up once elapsed
                # time since the last confirmed refresh could plausibly
                # have exceeded the real Redis-side TTL.
                elapsed = asyncio.get_running_loop().time() - self._last_refreshed_at
                retry_margin = self._ttl_seconds - self._refresh_interval_seconds
                if elapsed < retry_margin:
                    logger.warning("keno_round_lock_refresh_failed_retrying", elapsed_seconds=elapsed, exc_info=True)
                    continue
                logger.warning("keno_round_lock_refresh_failed", exc_info=True)
                self._held = False
                return
            if not refreshed:
                self._held = False
                return
            self._last_refreshed_at = asyncio.get_running_loop().time()

    async def release(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        try:
            await self._redis.eval(_DELETE_IF_OWNER, 1, self._key, self._worker_id)
        finally:
            self._held = False
