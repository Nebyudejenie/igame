"""End-to-end gameplay over a real WebSocket connection: auth, room list,
join, take_card, and a full round settling -- proving the gateway, the
Redis command channel, and the engine all actually work together, not just
each piece in isolation.
"""

import asyncio
import json
import time
from decimal import Decimal

import websockets

from packages.core import ledger
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import (
    build_init_data,
    create_funded_user,
    create_room,
    fund_user,
    next_telegram_id,
)


async def wait_until(predicate, timeout: float = 15.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def recv_until(ws, message_type: str, *, attempts: int = 50, timeout: float = 5.0) -> dict:
    """Reads frames until one with t == message_type shows up.

    A single WebSocket carries messages from two independent paths -- the
    direct command-channel reply and this connection's own room
    broadcasts (a player sees their own card_taken/balance events the same
    as everyone else in the room) -- so a client must dispatch by message
    type rather than assume a fixed reply immediately follows a request.
    """
    for _ in range(attempts):
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if message.get("t") == message_type:
            return message
    raise AssertionError(f"never saw a '{message_type}' message after {attempts} frames")


async def test_full_gameplay_over_websocket(gateway_server, pool, redis, card_pool, conn):
    # is_active=True: the gateway's own "rooms" list command reads WHERE
    # is_active = true, unlike every other test using create_room() (see
    # its own docstring for why False is the right default there).
    room_id = await create_room(
        conn,
        stake=Decimal("10.00"),
        min_players=2,
        lobby_seconds=1,
        call_interval_ms=10,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        # A second player joins directly through the engine so the lobby
        # can actually fill -- this test is about the WebSocket path for
        # player one, not about running two browser sessions.
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            assert authed["t"] == "authed"
            user_id = authed["user"]["id"]
            assert authed["user"]["balance"] == "0.00"

            # Deposits aren't wired up until Phase 5-6 -- fund directly
            # through the ledger, the same real path a deposit would use.
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "rooms"}))
            rooms_msg = json.loads(await ws.recv())
            assert rooms_msg["t"] == "rooms"
            assert any(r["room_id"] == room_id for r in rooms_msg["rooms"])

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            state = json.loads(await ws.recv())
            assert state["t"] == "state_sync"
            assert state["room_id"] == room_id

            await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 1}))
            ack = await recv_until(ws, "ack")
            assert ack == {"t": "ack", "for": "take_card", "ok": True, "reason": None, "room_id": room_id}

            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            assert await ledger.balance(conn, cash.id) == Decimal("90.00")

            # A live call should arrive over the socket once the round
            # starts -- proving the Redis pub/sub fan-out path, not just
            # the request/response command channel.
            await recv_until(ws, "call")

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None)

        round_row = await pool.fetchrow(
            "SELECT status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_row["status"] in ("done", "voided")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_take_card_uses_the_players_persisted_auto_mark_preference(
    gateway_server, pool, redis, card_pool, conn
):
    # Mini App spec: "AUTO toggle ... Persist the choice per user." A
    # player who turns AUTO off must get AUTO off again on their very next
    # take_card, even in a different room and a brand-new WebSocket
    # connection -- not just for the rest of the connection that set it.
    room_a = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1,
        call_interval_ms=10, is_active=True,
    )
    room_b = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1,
        call_interval_ms=10, is_active=True,
    )
    engine_a = RoundEngine(pool, redis, await load_room_config(pool, room_a), card_pool)
    engine_b = RoundEngine(pool, redis, await load_room_config(pool, room_b), card_pool)
    task_a = asyncio.create_task(engine_a.run_forever())
    task_b = asyncio.create_task(engine_b.run_forever())
    try:
        other_a = await create_funded_user(conn)
        assert (await engine_a.join(other_a, 2)).ok
        other_b = await create_funded_user(conn)
        assert (await engine_b.join(other_b, 2)).ok

        telegram_id = next_telegram_id()

        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            user_id = authed["user"]["id"]
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "join", "room_id": room_a}))
            await recv_until(ws, "state_sync")
            await ws.send(json.dumps({"t": "take_card", "room_id": room_a, "card_no": 1}))
            ack = await recv_until(ws, "ack")
            assert ack == {"t": "ack", "for": "take_card", "ok": True, "reason": None, "room_id": room_a}

            round_a_id = await pool.fetchval(
                "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_a
            )
            assert await pool.fetchval(
                "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
                round_a_id,
                user_id,
            ) is True

            await ws.send(json.dumps({"t": "set_auto", "room_id": room_a, "auto": False}))
            set_auto_ack = await recv_until(ws, "ack")
            assert set_auto_ack == {"t": "ack", "for": "set_auto", "ok": True, "reason": None, "room_id": room_a}

        assert await pool.fetchval(
            "SELECT auto_mark_preference FROM users WHERE id = $1", user_id
        ) is False

        # A second, independent connection, joining a different room, with
        # no set_auto sent in this session at all -- proof this is a real
        # persisted default, not state kept in the first connection's own
        # ConnectionHandler instance.
        async with websockets.connect(gateway_server) as ws2:
            await ws2.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            await ws2.recv()  # authed

            await ws2.send(json.dumps({"t": "join", "room_id": room_b}))
            await recv_until(ws2, "state_sync")
            await ws2.send(json.dumps({"t": "take_card", "room_id": room_b, "card_no": 1}))
            ack2 = await recv_until(ws2, "ack")
            assert ack2 == {"t": "ack", "for": "take_card", "ok": True, "reason": None, "room_id": room_b}

        round_b_id = await pool.fetchval(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_b
        )
        assert await pool.fetchval(
            "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
            round_b_id,
            user_id,
        ) is False
    finally:
        await engine_a.stop()
        await engine_b.stop()
        await asyncio.wait_for(task_a, timeout=15)
        await asyncio.wait_for(task_b, timeout=15)


async def test_claim_is_rate_limited_after_three_false_claims_in_one_session(
    gateway_server, pool, redis, card_pool, conn
):
    # Spec 3.4: "three false claims in a session triggers a soft
    # rate-limit." A pattern needs at least 4-5 specific numbers marked on
    # this specific card, so firing all four claims immediately once the
    # round is "running" (before more than a couple of calls, if any, can
    # possibly have landed) makes "no_pattern" the only realistic outcome
    # every time -- no need for an artificially long call_interval_ms,
    # which would only make engine.stop()'s teardown below hang (stop()
    # can't interrupt a round that's already calling; run_forever()'s
    # outer loop only checks it between rounds, see round_engine.py's own
    # stop()/run_forever()).
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1,
        call_interval_ms=15, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        other_player = await create_funded_user(conn)

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            user_id = authed["user"]["id"]
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            await recv_until(ws, "state_sync")

            round_id = None
            # round_engine.py's own per-round lockout already blocks a
            # second claim attempt within the same round (the second one
            # would come back "locked_out", not "no_pattern") -- reaching
            # three counted false claims genuinely takes three separate
            # rounds, one manual false claim each, exactly the cross-round
            # escalation spec 3.4 describes.
            #
            # Nobody ever wins these rounds (the false claim is deliberate,
            # and other_player never claims), so each one calls all 75
            # numbers before settling voided -- broadcasting ~75 unread
            # "call" frames into this connection's own receive buffer
            # between rounds, since wait_until() below polls engine.status
            # directly rather than draining the socket. recv_until()'s
            # default 50-attempt cap was tuned for a single round with no
            # backlog; a higher budget here is what actually drains that
            # accumulated backlog on round 2 and 3, not a flaky timeout --
            # confirmed by the first draft of this test failing exactly
            # this way (~60% of runs, "never saw a 'ack' message after 50
            # frames") with the default before this was raised.
            for attempt in range(3):
                assert (await engine.join(other_player, 2)).ok
                await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 1}))
                await recv_until(ws, "ack", attempts=300)

                await wait_until(lambda: engine.status == "running", timeout=10)
                round_id = engine.round_id
                assert round_id is not None

                await ws.send(json.dumps({"t": "claim", "round_id": round_id}))
                result = await recv_until(ws, "claim_result", attempts=300)
                # card_no is the gateway's own server-side resolution (this
                # frame deliberately sends none, matching every Mini App
                # build before multi-card support) -- always this session's
                # one held card, card 1.
                assert result == {
                    "t": "claim_result",
                    "valid": False,
                    "reason": "no_pattern",
                    "card_no": 1,
                    "round_id": round_id,
                }, f"attempt {attempt}: {result}"

                await wait_until(lambda: engine.status == "idle", timeout=15)

            # The 4th claim in this connection must never even reach the
            # engine -- rejected locally as rate_limited, not as another
            # claim_result -- even against a round_id from an already-
            # finished round, since the check fires before any round
            # lookup at all.
            await ws.send(json.dumps({"t": "claim", "round_id": round_id}))
            blocked = await recv_until(ws, "error", attempts=300)
            assert blocked["code"] == "rate_limited"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_rooms_list_reports_a_real_lobby_deadline(gateway_server, pool, redis, card_pool, conn):
    # Mini App spec 2.1: the room list's own countdown ("0:18") for a room
    # still filling its lobby. An architecture audit found the SQL behind
    # list_rooms() already selected lobby_deadline but the Python code
    # silently dropped it before it ever reached a client -- this proves
    # a real client actually receives a real, correctly-bounded deadline,
    # not just that the backend query includes the column.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5,
        call_interval_ms=10, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        # One direct join is enough to open the lobby (min_players=2, so
        # the round won't actually start calling numbers yet) -- this
        # test is about the room list's own reported deadline, not full
        # gameplay.
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok
        await wait_until(lambda: engine.status == "lobby", timeout=5)

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            await ws.recv()  # authed

            await ws.send(json.dumps({"t": "rooms"}))
            rooms_msg = json.loads(await ws.recv())
            assert rooms_msg["t"] == "rooms"

            room_entry = next(r for r in rooms_msg["rooms"] if r["room_id"] == room_id)
            assert room_entry["status"] == "lobby"
            assert room_entry["lobby_deadline_ms"] is not None

            now_ms = time.time() * 1000
            seconds_left = (room_entry["lobby_deadline_ms"] - now_ms) / 1000
            # Real bound, not a coincidence: lobby_seconds=5 above, so a
            # correct deadline is somewhere under 5s away, comfortably
            # above 0 (the lobby only just opened).
            assert 0 < seconds_left <= 5

        # Let the round actually finish (short lobby_seconds and fast
        # call_interval_ms above, so this is quick) before tearing down --
        # stop() only takes effect between rounds, matching every other
        # test in this file's own pattern.
        await wait_until(lambda: engine.status == "idle", timeout=15)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_zero_player_room_still_appears_live_in_the_room_list(
    gateway_server, pool, redis, card_pool, conn
):
    # Launch-readiness audit item: confirm a room with nobody in it yet
    # doesn't disappear from, or error out of, the lobby list a player
    # sees before joining anything. list_rooms() (services/gateway/
    # queries.py) has no player-count filter and defends the aggregate
    # count with `or 0`, and run_forever() proactively opens a room's very
    # first round the instant its engine claims it (never leaving a fresh
    # room sitting at a bare, round-less "idle") -- this proves both hold
    # for a real client over the real WS path, not just by reading the
    # code.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=30, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.status == "lobby", timeout=5)

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            await ws.recv()  # authed

            await ws.send(json.dumps({"t": "rooms"}))
            rooms_msg = json.loads(await ws.recv())
            assert rooms_msg["t"] == "rooms"

            room_entry = next(r for r in rooms_msg["rooms"] if r["room_id"] == room_id)
            assert room_entry["players"] == 0
            assert room_entry["status"] == "lobby"
            assert room_entry["lobby_deadline_ms"] is not None
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_state_sync_reports_the_rooms_real_configured_winning_condition(
    gateway_server, pool, redis, card_pool, conn
):
    """Backend/frontend consistency for the new per-room min_winning_lines
    rule (item 12 of this feature's own test checklist): a real client
    joining a room configured for something other than the product
    default must receive that room's *actual* value over the wire, not
    the old hardcoded assumption -- this is exactly the payload web/
    miniapp/js/app.v6.js's own state_sync handler reads into
    minWinningLines, which render/card.js's hasCompletePattern() then
    uses instead of its own former hardcoded constant.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, min_winning_lines=3, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    assert room.min_winning_lines == 3
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            await ws.recv()  # authed

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            state = json.loads(await ws.recv())
            assert state["t"] == "state_sync"
            assert state["room_id"] == room_id
            assert state["min_winning_lines"] == 3
            assert state["win_patterns"] == room.win_patterns
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_malformed_claim_is_rejected_gracefully_not_crashed(gateway_server, pool, redis):
    # Launch-readiness audit gap: no existing test sent a genuinely
    # malformed claim frame over the real WS path. connection.py's
    # `_room_id_for_round()` already guards with `isinstance(round_id,
    # int)` -- this proves that guard actually reaches a real client as a
    # clean `bad_round_id` error, not an exception that drops the
    # connection or a silent no-op the client can never distinguish from
    # a dropped message.
    telegram_id = next_telegram_id()
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
        await ws.recv()  # authed

        await ws.send(json.dumps({"t": "claim", "round_id": "not-a-real-round-id", "card_no": 1}))
        reply = json.loads(await ws.recv())
        assert reply["t"] == "error"
        assert reply["code"] == "bad_round_id"

        # The connection itself must still be alive and usable afterward --
        # a malformed message must never silently kill the socket.
        await ws.send(json.dumps({"t": "rooms"}))
        rooms_reply = json.loads(await ws.recv())
        assert rooms_reply["t"] == "rooms"


async def test_drop_card_true_is_not_treated_as_a_real_card_number(
    gateway_server, pool, redis, card_pool, conn
):
    """A code review pass caught that connection.py's own `isinstance(
    card_no, int)` check accepts Python's bool (a real int subclass), so
    a frame carrying `"card_no": true` would skip the server-side "old/
    malformed client, resolve their card for them" fallback entirely and
    be used directly -- `True == 1` under Python's own equality/hashing,
    so it would silently act on whichever card is keyed 1 for that user
    instead of triggering resolution. Proven here with a user who does
    NOT hold card 1 at all: the buggy behavior would reject this as
    "not_in_round" (no entry keyed (user, 1) exists); the fixed behavior
    resolves `true` as "not a real card_no" and falls back to the user's
    actual lowest held card.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5, max_cards_per_player=2,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            user_id = authed["user"]["id"]
            await fund_user(conn, user_id, Decimal("100.00"))

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            await recv_until(ws, "state_sync")

            # Deliberately doesn't hold card 1 -- cards 5 and 10 only.
            await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 5}))
            await recv_until(ws, "ack")
            await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 10}))
            await recv_until(ws, "ack")

            await ws.send(json.dumps({"t": "drop_card", "room_id": room_id, "card_no": True}))
            result = await recv_until(ws, "ack")
            assert result["ok"] is True, (
                f"expected card_no: true to resolve via the same-client fallback and "
                f"succeed (dropping card 5, the lowest held), got: {result}"
            )

        row = await pool.fetchrow(
            "SELECT card_no FROM round_entries WHERE round_id = $1 AND user_id = $2",
            engine.round_id,
            user_id,
        )
        assert row is not None and row["card_no"] == 10, (
            "card 5 (the lowest held) should have been the one dropped via resolution; "
            "only card 10 should remain"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
