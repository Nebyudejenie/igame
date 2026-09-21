"""Real-WebSocket proof that a connected client receives Keno broadcast
events (build spec Part 11) -- same pattern as test_gateway_auth.py/
test_gateway_fanout.py's own Bingo-side coverage, against the actual
FanoutHub + ConnectionHandler wiring, not a mock."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from tests.integration.conftest import build_init_data, next_telegram_id

pytestmark = pytest.mark.asyncio


async def test_authenticated_connection_receives_a_keno_live_broadcast(gateway_server, redis):
    telegram_id = next_telegram_id()
    async with websockets.connect(gateway_server) as ws:
        await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
        authed = json.loads(await ws.recv())
        assert authed["t"] == "authed"

        # Real broadcast, over real Redis pub/sub, the exact channel
        # services/engine/keno_round_engine.py's own _publish() writes to
        # -- not a direct call into the connection handler.
        await redis.publish(
            "keno:live", json.dumps({"t": "keno.betting.open", "round_id": 999, "betting_seconds": 25})
        )

        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert message["t"] == "keno.betting.open"
        assert message["round_id"] == 999


async def test_two_connections_both_receive_the_same_broadcast(gateway_server, redis):
    ids = [next_telegram_id(), next_telegram_id()]
    sockets = []
    try:
        for telegram_id in ids:
            ws = await websockets.connect(gateway_server)
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            reply = json.loads(await ws.recv())
            assert reply["t"] == "authed"
            sockets.append(ws)

        await redis.publish("keno:live", json.dumps({"t": "keno.draw.completed", "round_id": 1234}))

        results = await asyncio.gather(*(asyncio.wait_for(ws.recv(), timeout=5) for ws in sockets))
        for raw in results:
            message = json.loads(raw)
            assert message["t"] == "keno.draw.completed"
            assert message["round_id"] == 1234
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)


async def test_keno_ticket_settled_is_private_to_the_owning_user(gateway_server, redis):
    """Part 11/12: private per-ticket outcomes (keno.ticket.settled) go
    only to that user's own user:{id} channel, never the public
    keno:live one -- proven by publishing to user:{id} directly (the
    real channel services/engine/keno_round_engine.py's
    _publish_private_ticket_settled uses) and confirming only that
    connection receives it, not an unrelated one listening on keno:live."""
    owner_telegram_id = next_telegram_id()
    other_telegram_id = next_telegram_id()

    async with websockets.connect(gateway_server) as owner_ws, websockets.connect(gateway_server) as other_ws:
        await owner_ws.send(json.dumps({"t": "auth", "init_data": build_init_data(owner_telegram_id)}))
        owner_authed = json.loads(await owner_ws.recv())
        await other_ws.send(json.dumps({"t": "auth", "init_data": build_init_data(other_telegram_id)}))
        await other_ws.recv()

        owner_user_id = owner_authed["user"]["id"]
        await redis.publish(
            f"user:{owner_user_id}",
            json.dumps({"t": "keno.ticket.settled", "round_id": 1, "ticket_id": 1, "matches": 5, "payout": "16.00"}),
        )

        owner_message = json.loads(await asyncio.wait_for(owner_ws.recv(), timeout=5))
        assert owner_message["t"] == "keno.ticket.settled"

        # The other connection must NOT receive it -- confirmed by racing
        # a short timeout against a real recv() that should never resolve.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(other_ws.recv(), timeout=1)
