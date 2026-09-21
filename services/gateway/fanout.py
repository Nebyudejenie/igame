"""Redis pub/sub -> local WebSocket fan-out.

One FanoutHub per gateway process, subscribing once to `room:*` and
`user:*`. Every message it receives from Redis was serialized exactly once
by whoever published it (the engine, via round_engine.py's `_publish_room`)
-- the hub's only job is handing that same string to every locally
connected socket that's interested, never re-serializing it per connection.
That's what makes the cost of one number call independent of how many
players are watching it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict

from redis.asyncio import Redis

MAX_QUEUE_SIZE = 100

# Message types safe to drop for a connection that's falling behind: the
# next state_sync fully supersedes them. Anything else (round_end, balance,
# card_taken) is kept -- losing a settlement message because a socket was
# briefly slow is not an acceptable trade.
DROPPABLE_TYPES = {"lobby_tick", "call"}


def _peek_type(raw_message: str) -> str | None:
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return parsed.get("t") if isinstance(parsed, dict) else None


class ConnectionQueue:
    """A bounded outbound mailbox for one WebSocket connection.

    Implements the spec's section 6.4 backpressure rule at the application
    level: Starlette/ASGI doesn't expose the raw socket send-buffer size the
    spec's "64 KB" language assumes, so queue depth is the equivalent signal
    here. When the queue is full, this connection is provably behind --
    dropping its pending droppable (tick-shaped) messages and flagging a
    fresh `state_sync` is strictly better than either blocking the whole
    room's fan-out on one slow reader or silently growing memory forever.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self.needs_state_sync = False
        # A code review pass caught that the writer loop only ever checks
        # needs_state_sync at the top of its own while-loop, immediately
        # before blocking on queue.get() -- if this flag flips *while*
        # that get() is already parked waiting (the queue was drained to
        # empty around the same moment the overflow below happened), the
        # bare boolean alone doesn't wake it up. Nothing else does either
        # until some unrelated message happens to arrive later, which
        # near a quiet round boundary (calls pausing before settlement)
        # could leave a recovering client's board stale for a real,
        # unbounded stretch. This event exists purely to interrupt that
        # wait -- see get_or_wake() below.
        self._wake_event = asyncio.Event()

    def offer(self, raw_message: str) -> None:
        try:
            self.queue.put_nowait(raw_message)
        except asyncio.QueueFull:
            self._handle_full(raw_message)

    def _handle_full(self, raw_message: str) -> None:
        if _peek_type(raw_message) in DROPPABLE_TYPES:
            self.needs_state_sync = True
            self._wake_event.set()
            return
        # A non-droppable message arrived while full: everything currently
        # queued is stale relative to it, so clear the backlog and keep
        # this one rather than lose it.
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queue.put_nowait(raw_message)

    async def get_or_wake(self) -> str | None:
        """Waits for either the next queued message or needs_state_sync
        being raised, whichever happens first. Returns the message, or
        None if it was woken by the flag instead -- the caller's own
        top-of-loop needs_state_sync check (unchanged) is what actually
        acts on that; this only exists to make sure that check runs
        promptly instead of waiting on whatever unrelated message
        happens to arrive next.

        Fast path first: a real load test caught that racing two freshly
        -created tasks (asyncio.wait() plus a cancel-and-await of
        whichever one loses) on *every single call* -- even when the
        queue already had a message sitting there ready -- was measurably
        slower than the plain queue.get() this replaced, enough to blow
        the p99 fan-out latency budget under a real multi-socket load
        test. The race is only actually needed while genuinely blocked on
        an empty queue -- that's the one moment a wake signal has
        anything to interrupt -- so check get_nowait() first and only
        pay for the race when there's truly nothing to return yet.
        """
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        get_task: asyncio.Task[str] = asyncio.ensure_future(self.queue.get())
        wake_task: asyncio.Task[bool] = asyncio.ensure_future(self._wake_event.wait())
        # Pre-populated so a cancellation of this coroutine itself (e.g.
        # the writer loop's task being torn down on disconnect) still
        # cleans up both tasks in `finally` -- if asyncio.wait() itself
        # never returns, `pending` must not be left referring to nothing.
        pending: set[asyncio.Task[object]] = {get_task, wake_task}
        try:
            done, pending = await asyncio.wait(
                {get_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if wake_task in done:
            self._wake_event.clear()
        if get_task in done:
            return get_task.result()
        return None


class FanoutHub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._pubsub = redis.pubsub()
        self._room_subscribers: dict[int, set[ConnectionQueue]] = defaultdict(set)
        self._user_subscribers: dict[int, set[ConnectionQueue]] = defaultdict(set)
        # Keno has exactly one continuous round stream, not a per-room
        # concept (services/engine/keno_round_engine.py publishes every
        # public round event to one literal channel, "keno:live") -- a
        # plain set, not a dict keyed by an id that doesn't exist here.
        self._keno_subscribers: set[ConnectionQueue] = set()
        self._listener_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._pubsub.psubscribe("room:*", "user:*", "keno:*")
        self._listener_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        await self._pubsub.punsubscribe("room:*", "user:*", "keno:*")
        await self._pubsub.aclose()  # type: ignore[no-untyped-call]

    def subscribe_room(self, room_id: int, cq: ConnectionQueue) -> None:
        self._room_subscribers[room_id].add(cq)

    def unsubscribe_room(self, room_id: int, cq: ConnectionQueue) -> None:
        subs = self._room_subscribers.get(room_id)
        if subs is None:
            return
        subs.discard(cq)
        if not subs:
            del self._room_subscribers[room_id]

    def subscribe_user(self, user_id: int, cq: ConnectionQueue) -> None:
        self._user_subscribers[user_id].add(cq)

    def unsubscribe_user(self, user_id: int, cq: ConnectionQueue) -> None:
        subs = self._user_subscribers.get(user_id)
        if subs is None:
            return
        subs.discard(cq)
        if not subs:
            del self._user_subscribers[user_id]

    def subscribe_keno(self, cq: ConnectionQueue) -> None:
        self._keno_subscribers.add(cq)

    def unsubscribe_keno(self, cq: ConnectionQueue) -> None:
        self._keno_subscribers.discard(cq)

    async def _listen(self) -> None:
        async for message in self._pubsub.listen():
            if not isinstance(message, dict) or message.get("type") != "pmessage":
                continue
            channel = message["channel"]
            data = message["data"]
            if channel.startswith("room:"):
                room_id_str = channel.removeprefix("room:")
                if not room_id_str.isdigit():
                    continue
                for cq in list(self._room_subscribers.get(int(room_id_str), ())):
                    cq.offer(data)
            elif channel.startswith("user:"):
                user_id_str = channel.removeprefix("user:")
                if not user_id_str.isdigit():
                    continue
                for cq in list(self._user_subscribers.get(int(user_id_str), ())):
                    cq.offer(data)
            elif channel == "keno:live":
                for cq in list(self._keno_subscribers):
                    cq.offer(data)
