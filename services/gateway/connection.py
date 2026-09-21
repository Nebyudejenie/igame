"""One instance per WebSocket: the auth handshake, inbound message dispatch,
and the outbound writer loop draining this connection's fan-out mailbox.

Money-moving actions (take_card, drop_card, set_auto, claim) never touch the
database directly from here -- they go over services/engine/commands.py to
whichever engine process actually owns the room, which is the only writer
of that room's state. This handler only ever reads (queries.py) or forwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import asyncpg
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from packages.core import metrics, rate_limit, telegram_auth
from packages.core.ledger import user_balance_snapshot
from packages.core.telegram_auth import InvalidInitData
from services.admin.simulated_players_queries import active_simulated_player_count
from services.engine import commands
from services.engine.commands import CommandTimeout
from services.gateway import queries
from services.gateway.fanout import ConnectionQueue, FanoutHub

logger = structlog.get_logger()

AUTH_TIMEOUT_SECONDS = 5.0
GOING_AWAY_RECONNECT_CODE = 1012  # "service restart" -- reconnect now, don't back off
# Spec 3.4, verbatim: "three false claims in a session triggers a soft
# rate-limit." The trigger count is the one number spec actually gives;
# the exact throttle shape isn't specified, so this mirrors spec's own
# language for the (already-implemented) round-level lockout -- "locks
# their BINGO button for the rest of that round" -- one scope level up:
# the button locks for the rest of this connection instead of inventing
# an arbitrary cooldown duration or refill rate.
FALSE_CLAIM_SESSION_LIMIT = 3


def _is_real_int(value: object) -> bool:
    """isinstance(value, int) alone accepts Python's bool (a real int
    subclass) -- true for the client-sent card_no checks below, where a
    frame carrying card_no: true would otherwise skip server-side
    resolution and be used directly (True == 1, same hash), silently
    acting on whatever card is keyed 1 for that user instead of the
    intended "resolve this old/malformed client's card for them" path.
    """
    return isinstance(value, int) and not isinstance(value, bool)


class ConnectionHandler:
    def __init__(
        self,
        websocket: WebSocket,
        pool: asyncpg.Pool,
        redis: Redis,
        hub: FanoutHub,
        bot_token: str,
        connections: set[ConnectionHandler],
    ) -> None:
        self._ws = websocket
        self._pool = pool
        self._redis = redis
        self._hub = hub
        self._bot_token = bot_token
        # The same set services/gateway/app.py's ws_endpoint adds/removes
        # this handler from -- a live headcount of real people with the
        # app open right now (never bots: simulated players act purely
        # through services.engine.commands.send_command(), no WebSocket
        # of their own), read on demand rather than tracked separately,
        # so it can never drift from the actual set of open connections.
        self._connections = connections

        self._user_id: int | None = None
        self._auto_mark_preference: bool = True
        self._false_claim_count: int = 0
        self._joined_rooms: set[int] = set()
        self._cq = ConnectionQueue()
        self._writer_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        await self._ws.accept()
        try:
            if not await self._handshake():
                return
            self._writer_task = asyncio.create_task(self._writer_loop())
            await self._message_loop()
        except WebSocketDisconnect:
            pass
        finally:
            await self._cleanup()

    async def close_for_shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close(code=GOING_AWAY_RECONNECT_CODE, reason="restarting")

    # --- handshake -------------------------------------------------------

    async def _handshake(self) -> bool:
        # Every rejection here also gets a server-side log line -- a
        # production incident (every real player's Mini App stuck on
        # "session expired") caught that _safe_close()'s reason string
        # only ever reached the *client's* WS close frame, never this
        # process's own logs, leaving no way to tell a genuinely stale
        # session apart from every player hitting the exact same
        # rejection (a misconfigured TELEGRAM_BOT_TOKEN, most commonly --
        # every hash check fails identically for every user) without
        # production browser devtools access. Only the short reason
        # string is logged, per telegram_auth.py's own module docstring
        # -- never the raw init_data or the bot token.
        try:
            raw = await asyncio.wait_for(self._ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        except (TimeoutError, WebSocketDisconnect):
            logger.warning("ws_handshake_rejected", reason="auth_timeout")
            await self._safe_close(4001, "auth_timeout")
            return False

        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ws_handshake_rejected", reason="bad_frame")
            await self._safe_close(4000, "bad_frame")
            return False

        if not isinstance(frame, dict) or frame.get("t") != "auth":
            logger.warning("ws_handshake_rejected", reason="expected_auth")
            await self._safe_close(4000, "expected_auth")
            return False

        try:
            data = telegram_auth.validate_init_data(
                frame.get("init_data", ""), self._bot_token
            )
        except InvalidInitData as exc:
            logger.warning("ws_handshake_rejected", reason=f"invalid_init_data:{exc.reason}")
            await self._safe_close(4003, f"invalid_init_data:{exc.reason}")
            return False

        display_name = data.user.first_name or data.user.username or str(data.user.id)
        user_id = await queries.get_or_create_user_by_telegram_id(
            self._pool, data.user.id, display_name
        )
        self._user_id = user_id
        metrics.gateway_connections.inc()
        # Independent reads, same reasoning as build_state_sync()'s own
        # asyncio.gather: none of these three depend on each other's result.
        self._auto_mark_preference, language, balance = await asyncio.gather(
            queries.get_auto_mark_preference(self._pool, user_id),
            queries.get_user_language(self._pool, user_id),
            user_balance_snapshot(self._pool, user_id),
        )
        self._hub.subscribe_user(user_id, self._cq)
        # Every connection gets the Keno broadcast channel -- unlike Bingo
        # rooms (subscribed only once a player actually joins one), Keno
        # has exactly one continuous round stream and the Game Center /
        # Keno screen needs to reflect it the moment the socket is up,
        # the same reasoning the user:{id} balance channel is subscribed
        # unconditionally here rather than waiting for the wallet screen
        # to open.
        self._hub.subscribe_keno(self._cq)
        metrics.keno_ws_connections.inc()

        await self._ws.send_text(
            json.dumps(
                {
                    "t": "authed",
                    "user": {
                        "id": user_id,
                        "name": display_name,
                        "balance": balance["cash"],
                        "language": language,
                    },
                    "server_time": int(time.time() * 1000),
                }
            )
        )
        return True

    async def _safe_close(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close(code=code, reason=reason)

    # --- message loop ------------------------------------------------------

    async def _message_loop(self) -> None:
        assert self._user_id is not None
        while True:
            raw = await self._ws.receive_text()
            allowed = await rate_limit.allow(
                self._redis, "ws", str(self._user_id), **rate_limit.WS_MESSAGES
            )
            if not allowed:
                await self._send_error("rate_limited", "Slow down.", "እባክዎ ትንሽ ይታገሱ።")
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error("bad_frame", "Malformed message.", "የተሳሳተ መልእክት።")
                continue
            if not isinstance(frame, dict):
                await self._send_error("bad_frame", "Malformed message.", "የተሳሳተ መልእክት።")
                continue
            await self._dispatch(frame)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        t = frame.get("t")

        if t == "ping":
            await self._ws.send_text(
                json.dumps(
                    {"t": "pong", "ts": frame.get("ts"), "server_time": int(time.time() * 1000)}
                )
            )
        elif t == "rooms":
            rooms = await queries.list_rooms(self._pool)
            # Real WebSocket connections plus active-but-idle-or-playing
            # simulated players -- a bot never opens a WebSocket of its
            # own, so without this it stayed invisible in "online" the
            # entire time it wasn't actually seated in a round (see
            # active_simulated_player_count()'s own docstring).
            simulated_online = await active_simulated_player_count(self._pool)
            await self._ws.send_text(
                json.dumps(
                    {
                        "t": "rooms",
                        "rooms": rooms,
                        "online_count": len(self._connections) + simulated_online,
                    }
                )
            )
        elif t == "join":
            await self._handle_join(frame)
        elif t == "leave":
            await self._handle_leave(frame)
        elif t == "take_card":
            await self._run_action(
                frame.get("room_id"),
                ack_name="take_card",
                action="join",
                payload={
                    "card_no": frame.get("card_no"),
                    "auto_mark": self._auto_mark_preference,
                },
                bucket=rate_limit.TAKE_CARD,
            )
        elif t == "drop_card":
            room_id = frame.get("room_id")
            card_no = frame.get("card_no")
            # Every Mini App build before multi-card frontend support never
            # sends card_no at all -- resolve it server-side (a read, not a
            # write, matching this file's own "reads go through queries.py"
            # architecture) so those clients keep working exactly as
            # before through this transition. A client that does send an
            # explicit card_no (once the frontend catches up) is trusted
            # as-is; join()/drop_card() on the engine side still validate
            # it's really this user's own card either way.
            #
            # bool excluded explicitly -- Python's bool is an int subclass
            # (isinstance(True, int) is True, and True == 1), so a
            # malformed/malicious frame sending card_no: true would
            # otherwise skip resolution entirely and silently act on
            # whichever card is keyed 1 for this user, a genuine (if
            # narrow) type-confusion gap in a real-money command path a
            # code review pass caught.
            if not _is_real_int(card_no) and isinstance(room_id, int):
                assert self._user_id is not None
                card_no = await queries.held_card_no_for_room(self._pool, room_id, self._user_id)
            await self._run_action(
                room_id, ack_name="drop_card", action="drop_card", payload={"card_no": card_no}
            )
        elif t == "set_auto":
            # Mini App spec: "Persist the choice per user" -- round_entries
            # .auto_mark (round-scoped, written by the engine) and this
            # user-scoped default (written directly here, same as
            # get_or_create_user_by_telegram_id's own direct writes) are
            # updated together so the player's next take_card picks up
            # whatever they last chose instead of resetting to AUTO on.
            assert self._user_id is not None
            auto = bool(frame.get("auto", True))
            self._auto_mark_preference = auto
            await asyncio.gather(
                self._run_action(
                    frame.get("room_id"),
                    ack_name="set_auto",
                    action="set_auto",
                    payload={"auto": auto},
                ),
                queries.set_auto_mark_preference(self._pool, self._user_id, auto),
            )
        elif t == "claim":
            if self._false_claim_count >= FALSE_CLAIM_SESSION_LIMIT:
                await self._send_error(
                    "rate_limited",
                    "Too many false claims this session.",
                    "በዚህ ክፍለ ጊዜ ውስጥ በጣም ብዙ የተሳሳቱ የቢንጎ ጥያቄዎች ነበሩ።",
                )
                return
            round_id = frame.get("round_id")
            room_id = await self._room_id_for_round(round_id)
            if room_id is None:
                await self._send_error("bad_round_id", "Unknown round.", "ያልታወቀ ዙር።")
                return
            card_no = frame.get("card_no")
            # Same resolution as drop_card above -- every pre-multi-card
            # client never sends card_no at all. See that comment for why
            # bool is explicitly excluded from "a real int".
            if not _is_real_int(card_no):
                assert self._user_id is not None and isinstance(round_id, int)
                card_no = await queries.held_card_no_for_round(self._pool, round_id, self._user_id)
            await self._run_action(
                room_id,
                ack_name="claim",
                action="claim",
                # round_id rides along in the payload purely so the
                # claim_result reply below can echo it back -- the engine's
                # own claim() only ever reads card_no from this dict
                # (round_engine.py's _handle_command), so this extra key is
                # inert to it. Without it, claim_result carried no
                # identifying field at all: card_no alone isn't unique
                # across rooms (the card pool is a shared 1..N range every
                # room deals from independently), so a stale false-claim
                # reply for a card just left behind in one room could
                # match, and incorrectly shake/reject, an identically
                # -numbered card genuinely held in whatever room the
                # player has since switched to.
                payload={"card_no": card_no, "round_id": round_id},
                bucket=rate_limit.CLAIM,
            )
        elif t == "mark":
            pass  # advisory only -- the server never trusts a client-reported mark
        else:
            await self._send_error(
                "unknown_message", f"Unknown message type: {t}", "ያልታወቀ መልእክት።"
            )

    async def _room_id_for_round(self, round_id: Any) -> int | None:
        if not isinstance(round_id, int):
            return None
        row = await self._pool.fetchrow("SELECT room_id FROM rounds WHERE id = $1", round_id)
        return int(row["room_id"]) if row is not None else None

    async def _handle_join(self, frame: dict[str, Any]) -> None:
        room_id = frame.get("room_id")
        if not isinstance(room_id, int):
            await self._send_error("bad_room_id", "room_id required.", "የክፍል መለያ ያስፈልጋል።")
            return
        assert self._user_id is not None
        # Structurally enforce "one connection watches at most one room at a
        # time" here, at the one place a subscription is actually created,
        # rather than leaving it to every client-side navigation path to
        # remember to send its own "leave" first. A real production bug
        # (a player browsing several rooms in one session saw a fast,
        # unattended room's own calls bleed into whatever room they were
        # actually looking at) traced back to exactly this: nothing here
        # ever stopped self._joined_rooms from accumulating more than one
        # room, so a client that forgot -- or, as later found, one that
        # simply couldn't yet, because it hadn't finished its first join
        # before starting a second one -- left this connection subscribed
        # to both forever. No real caller (client or test) ever relies on
        # one connection being subscribed to more than one room at once.
        for stale_room_id in list(self._joined_rooms - {room_id}):
            self._joined_rooms.discard(stale_room_id)
            self._hub.unsubscribe_room(stale_room_id, self._cq)
        self._joined_rooms.add(room_id)
        self._hub.subscribe_room(room_id, self._cq)
        state = await queries.build_state_sync(self._pool, room_id, self._user_id)
        await self._ws.send_text(json.dumps(state))

    async def _handle_leave(self, frame: dict[str, Any]) -> None:
        room_id = frame.get("room_id")
        if isinstance(room_id, int) and room_id in self._joined_rooms:
            self._joined_rooms.discard(room_id)
            self._hub.unsubscribe_room(room_id, self._cq)

    async def _run_action(
        self,
        room_id: Any,
        *,
        ack_name: str,
        action: str,
        payload: dict[str, Any],
        bucket: dict[str, float] | None = None,
    ) -> None:
        if not isinstance(room_id, int):
            await self._send_error("bad_room_id", "room_id required.", "የክፍል መለያ ያስፈልጋል።")
            return
        assert self._user_id is not None

        if bucket is not None:
            allowed = await rate_limit.allow(
                self._redis, action, str(self._user_id), **bucket
            )
            if not allowed:
                await self._send_error(
                    "rate_limited", "Too many requests.", "በጣም ብዙ ጥያቄዎች።"
                )
                return

        # ack_name, not action, labels the metric -- action is the actual
        # command name dispatched to the engine over Redis (round_engine
        # .py's _handle_command dispatches on it literally: take_card is
        # sent as action="join", matching RoundEngine.join()'s own method
        # name, an internal detail that has nothing to do with what a
        # Prometheus consumer should see). A code review pass caught the
        # metric using `action` directly: every take_card ack was
        # recorded under the "join" label, and the real "join" WS message
        # type (handled entirely separately by _handle_join(), which
        # never reaches this method at all) never recorded anything.
        with metrics.gateway_command_ack_seconds.labels(action=ack_name).time():
            try:
                result = await commands.send_command(
                    self._redis, room_id, action, self._user_id, payload
                )
            except CommandTimeout:
                await self._send_error(
                    "room_unavailable",
                    "This room isn't available right now.",
                    "ይህ ክፍል አሁን አይገኝም።",
                )
                return

            if action == "claim":
                # "no_pattern" specifically, matching RoundEngine.claim()'s
                # own scope for the round-level lockout it already applies
                # (self._locked_out) -- a rejection like "not_in_round" or
                # "round_already_settled" isn't a spam-tapped false BINGO
                # claim, so it shouldn't count toward this session escalation.
                if not result.ok and result.reason == "no_pattern":
                    self._false_claim_count += 1
                await self._ws.send_text(
                    json.dumps(
                        {
                            "t": "claim_result",
                            "valid": result.ok,
                            "reason": result.reason,
                            "card_no": payload.get("card_no"),
                            "round_id": payload.get("round_id"),
                        }
                    )
                )
            else:
                # room_id (already this method's own first parameter, no
                # extra plumbing needed) lets the client tell a take_card/
                # drop_card/set_auto ack for a room it has since navigated
                # away from apart from one for the room it's actually in --
                # same reasoning as claim_result's own round_id above.
                await self._ws.send_text(
                    json.dumps(
                        {
                            "t": "ack",
                            "for": ack_name,
                            "ok": result.ok,
                            "reason": result.reason,
                            "room_id": room_id,
                        }
                    )
                )

    async def _send_error(self, code: str, message_en: str, message_am: str) -> None:
        await self._ws.send_text(
            json.dumps(
                {"t": "error", "code": code, "message_en": message_en, "message_am": message_am}
            )
        )

    # --- outbound ----------------------------------------------------------

    async def _writer_loop(self) -> None:
        assert self._user_id is not None
        while True:
            if self._cq.needs_state_sync:
                self._cq.needs_state_sync = False
                for room_id in list(self._joined_rooms):
                    state = await queries.build_state_sync(self._pool, room_id, self._user_id)
                    await self._ws.send_text(json.dumps(state))
            # get_or_wake(), not a bare queue.get() -- a code review pass
            # caught that this loop only ever notices needs_state_sync at
            # the top of the loop, right before this line blocks. If the
            # flag flips while already parked here waiting (the queue was
            # empty at that exact moment), nothing woke it up until some
            # unrelated message happened to arrive later, which near a
            # quiet round boundary could leave a recovering client's
            # board stale indefinitely. get_or_wake() returns None
            # instead of a message when it was woken by the flag rather
            # than a real item, so there's nothing to send this iteration
            # -- the loop just goes straight back to the check above.
            raw = await self._cq.get_or_wake()
            if raw is not None:
                await self._ws.send_text(raw)

    async def _cleanup(self) -> None:
        if self._writer_task is not None:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
            self._writer_task = None
        for room_id in list(self._joined_rooms):
            self._hub.unsubscribe_room(room_id, self._cq)
        if self._user_id is not None:
            self._hub.unsubscribe_user(self._user_id, self._cq)
            self._hub.unsubscribe_keno(self._cq)
            metrics.gateway_connections.dec()
            metrics.keno_ws_connections.dec()
