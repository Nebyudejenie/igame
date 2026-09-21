"""Real Prometheus metrics (spec section 10.4) -- proven against the
actual running FastAPI apps and the actual RoundEngine/ledger/deposits
code, not by inspecting packages/core/metrics.py's definitions in
isolation. Every test here does a real thing (a real WebSocket connect, a
real ledger.post(), a real claim(), a real refund) and then scrapes the
real /metrics HTTP endpoint (or reads packages.core.metrics's own objects
directly, for signals no HTTP endpoint exposes in this test topology) to
confirm the number actually moved.

Assertions compare deltas (before vs after), never absolute values --
prometheus_client's registry is process-global and every other test in
this same pytest process shares it, so an absolute "== 1" assertion would
be flaky by construction (this file's own two gateway_connections tests,
run back to back, would collide on an absolute check).
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
import pytest
import websockets

from packages.core import ledger, metrics
from services.engine import refunds
from services.engine.round_engine import RoundEngine, load_card_pool, load_room_config
from services.payments import deposits
from services.payments.provider import CheckoutResult
from tests.integration.conftest import (
    build_init_data,
    create_funded_user,
    create_room,
    fund_user,
    next_telegram_id,
)


def _metric_value(text: str, name: str, label_substring: str | None = None) -> float:
    """Parses one sample out of real Prometheus exposition text -- the
    same format Prometheus itself scrapes, not a shortcut around it.

    A labeled metric's child (a Histogram's per-`action` series here) is
    only created, and only then appears in the output at all, the first
    time something calls .labels(...) with that exact label set -- unlike
    a bare Counter/Gauge, which emits a zero-value line from the moment the
    module is imported. So "no matching line yet" genuinely means the same
    thing Prometheus itself means by a missing series: 0. Every caller in
    this file only ever asserts a *delta* against this return value, so a
    genuinely wrong metric name still fails the test (0 before, 0 after,
    delta assertion fails) -- it just surfaces as a wrong-count failure
    rather than a separate "not found" error.
    """
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        head = line.split("{", 1)[0].split(" ", 1)[0]
        if head != name:
            continue
        if label_substring is not None and label_substring not in line:
            continue
        return float(line.rsplit(" ", 1)[-1])
    return 0.0


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_metrics_endpoint_serves_real_prometheus_exposition_format(gateway_server):
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base}/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    # Gauges/Counters emit a line at their zero value the moment the module
    # is imported -- present regardless of whether anything incremented
    # them yet in this process.
    assert "gateway_connections " in response.text
    assert "gateway_command_ack_seconds" in response.text


async def test_gateway_connections_gauge_tracks_a_real_websocket_lifecycle(gateway_server):
    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")

    async def scrape() -> str:
        async with httpx.AsyncClient() as client:
            return (await client.get(f"{http_base}/metrics")).text

    before = _metric_value(await scrape(), "gateway_connections")

    telegram_id = next_telegram_id()
    ws = await websockets.connect(gateway_server)
    try:
        await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
        authed = json.loads(await ws.recv())
        assert authed["t"] == "authed"

        during = _metric_value(await scrape(), "gateway_connections")
        assert during == before + 1
    finally:
        await ws.close()

    async def _dropped() -> bool:
        return _metric_value(await scrape(), "gateway_connections") == before

    async def _poll() -> None:
        while not await _dropped():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_poll(), timeout=5.0)


async def test_command_ack_histogram_records_a_real_take_card_action(
    gateway_server, pool, redis, card_pool, conn
):
    # Only one real player ever joins (via the WebSocket, below) -- the
    # room's min_players stays unmet, so the lobby runs out its own clock
    # and refunds rather than transitioning to "running". A short
    # lobby_seconds keeps that natural unwind (and this test's cleanup)
    # fast; engine.stop() alone wouldn't interrupt an in-progress lobby
    # wait, since _run_lobby()'s own loop is what checks the deadline.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=2, call_interval_ms=200
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        async with httpx.AsyncClient() as client:
            before_text = (await client.get(f"{http_base}/metrics")).text
        before = _metric_value(before_text, "gateway_command_ack_seconds_count", 'action="take_card"')

        telegram_id = next_telegram_id()
        async with websockets.connect(gateway_server) as ws:
            await ws.send(json.dumps({"t": "auth", "init_data": build_init_data(telegram_id)}))
            authed = json.loads(await ws.recv())
            await fund_user(conn, authed["user"]["id"], Decimal("100.00"))

            await ws.send(json.dumps({"t": "join", "room_id": room_id}))
            await ws.recv()  # state_sync

            await ws.send(json.dumps({"t": "take_card", "room_id": room_id, "card_no": 1}))
            for _ in range(20):
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if reply.get("t") == "ack":
                    assert reply == {
                        "t": "ack",
                        "for": "take_card",
                        "ok": True,
                        "reason": None,
                        "room_id": room_id,
                    }
                    break
            else:
                raise AssertionError("never saw the take_card ack")

        async with httpx.AsyncClient() as client:
            after_text = (await client.get(f"{http_base}/metrics")).text
        after = _metric_value(after_text, "gateway_command_ack_seconds_count", 'action="take_card"')
        assert after == before + 1
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_engine_metrics_move_across_a_real_round(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1, call_interval_ms=10
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)

    calls_before = metrics.engine_calls_total._value.get()
    claim_count_before = sum(
        s.value
        for s in metrics.engine_claim_validation_seconds.collect()[0].samples
        if s.name.endswith("_count")
    )
    rooms_active_before = metrics.engine_rooms_active._value.get()

    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        await wait_until(lambda: engine.status == "running", timeout=10)
        # A round in flight is exactly what "rooms active" (spec 10.4) means.
        assert metrics.engine_rooms_active._value.get() == rooms_active_before + 1

        await wait_until(lambda: engine.status in ("done", "idle"), timeout=30)
        await wait_until(lambda: engine.status == "idle", timeout=5)

        assert metrics.engine_calls_total._value.get() > calls_before
        claim_count_after = sum(
            s.value
            for s in metrics.engine_claim_validation_seconds.collect()[0].samples
            if s.name.endswith("_count")
        )
        assert claim_count_after > claim_count_before
        # Settlement returns the room to idle -- "rooms active" must drop
        # back down, not leak upward across rounds.
        assert metrics.engine_rooms_active._value.get() == rooms_active_before
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_ledger_transactions_counter_increments_on_a_real_post(pool, conn):
    before = metrics.ledger_transactions_total.labels(kind="deposit")._value.get()
    await create_funded_user(conn, Decimal("42.00"))  # fund_user posts kind="deposit"
    after = metrics.ledger_transactions_total.labels(kind="deposit")._value.get()
    assert after == before + 1


async def test_post_does_not_increment_the_metric_when_the_outer_transaction_rolls_back(pool, conn):
    # A code review pass caught that an earlier fix wasn't actually safe:
    # every real production caller already has its own transaction open
    # by the time it calls ledger.post(), which makes post()'s own
    # `async with conn.transaction()` a SAVEPOINT, not a real commit --
    # incrementing right after that savepoint releases (as the earlier
    # fix did) still overcounts if a LATER statement in the *caller's*
    # transaction then fails and rolls everything back. Simulates that
    # directly: post() succeeds inside an outer transaction this test
    # then deliberately rolls back, standing in for whatever real
    # statement (an UPDATE, an audit-log INSERT) a real caller runs after
    # post() but before its own commit.
    user_id = await create_funded_user(conn)
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    house = await ledger.get_or_create_account(conn, None, "house_float")

    before = metrics.ledger_transactions_total.labels(kind="adjustment")._value.get()

    outer = conn.transaction()
    await outer.start()
    try:
        await ledger.post(
            conn,
            "adjustment",
            [ledger.Entry(house.id, -Decimal("5.00")), ledger.Entry(cash.id, Decimal("5.00"))],
            idempotency_key=f"test-rollback-{user_id}",
        )
    finally:
        await outer.rollback()

    after = metrics.ledger_transactions_total.labels(kind="adjustment")._value.get()
    assert after == before, "metric incremented even though the outer transaction rolled back"


async def test_join_increments_ledger_transactions_metric_on_a_real_commit(pool, redis, card_pool, conn):
    # The other side of the fix above: a real call site (round_engine.py's
    # join(), which always calls post() with its own transaction already
    # open) must still increment the metric once ITS OWN transaction
    # genuinely commits -- confirming responsibility actually moved to the
    # caller, not just disappeared.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_id = await create_funded_user(conn)
        before = metrics.ledger_transactions_total.labels(kind="stake")._value.get()
        assert (await engine.join(user_id, 1)).ok
        after = metrics.ledger_transactions_total.labels(kind="stake")._value.get()
        assert after == before + 1
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_rounds_voided_counter_increments_on_a_real_refund(pool, redis, card_pool, conn):
    # A genuine round, gotten to "running" with real stakes through the
    # real engine (not hand-crafted rows), then killed mid-round the same
    # way test_recovery.py's crash test does (a graceful engine.stop()
    # only takes effect between rounds, so a hard cancel is what actually
    # leaves a round non-terminal) -- refund_round() is then called on it
    # directly, the same real, idempotent, ledger-backed void path crash
    # recovery and an underfilled lobby both use.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=50)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    p1 = await create_funded_user(conn)
    p2 = await create_funded_user(conn)
    assert (await engine.join(p1, 1)).ok
    assert (await engine.join(p2, 2)).ok
    await wait_until(lambda: engine.status == "running", timeout=10)
    round_id = engine.round_id
    assert round_id is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await redis.delete(f"room:lock:{room_id}")

    before = metrics.engine_rounds_voided_total._value.get()
    ledger_before = metrics.ledger_transactions_total.labels(kind="refund")._value.get()
    refunded = await refunds.refund_round(pool, round_id, reason="test-forced-void")
    assert refunded == 2  # p1 and p2, each their own ledger transaction
    after = metrics.engine_rounds_voided_total._value.get()
    assert after == before + 1
    # refund_round_in_transaction() itself can't safely record this (see
    # its own comment) -- confirms the caller-side fix actually counts
    # every entrant refunded, not just fires once regardless of how many.
    ledger_after = metrics.ledger_transactions_total.labels(kind="refund")._value.get()
    assert ledger_after == ledger_before + 2


@dataclass
class _FakeCheckoutProvider:
    name: str = "chapa"
    checkouts: dict[str, object] = field(default_factory=dict)

    async def create_checkout(self, *, amount, user_ref, our_ref, return_url, callback_url):
        return CheckoutResult(
            checkout_url=f"https://pay.test/{our_ref}", provider_ref=our_ref, raw_response={}
        )


async def test_deposit_outcomes_counter_increments_on_a_real_credit(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("0.00"))
    intent = await deposits.create_deposit_intent(
        pool,
        redis,
        _FakeCheckoutProvider(),
        user_id=user_id,
        amount=Decimal("25.00"),
        phone_e164="+251911000111",
        return_url="https://example.test/return",
        callback_url="https://payments.test/webhooks/chapa",
        min_deposit=Decimal("10.00"),
        daily_cap=Decimal("50000.00"),
    )

    before = metrics.deposit_outcomes_total.labels(outcome="credited")._value.get()
    outcome = await deposits._apply_confirmed_status(
        pool,
        redis,
        our_ref=intent.our_ref,
        event_id=f"evt-metrics-test-{uuid.uuid4()}",
        provider_name="chapa",
        status="succeeded",
        amount=Decimal("25.00"),
        provider_ref="prov-metrics-test",
        raw={},
    )
    assert outcome == "credited"
    after = metrics.deposit_outcomes_total.labels(outcome="credited")._value.get()
    assert after == before + 1


async def test_payments_metrics_endpoint_reports_live_queue_depth_and_house_revenue(
    payments_server, pool, redis, conn
):
    from services.payments.withdrawals import PAYOUT_STREAM

    await redis.xadd(PAYOUT_STREAM, {"our_ref": "metrics-test-ref", "payment_id": "0"})
    try:
        house_account = await ledger.get_or_create_account(conn, None, "house_revenue")
        provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
        before_revenue = await ledger.balance(conn, house_account.id)
        await ledger.post(
            conn,
            "adjustment",
            [
                ledger.Entry(provider_account.id, -Decimal("7.00")),
                ledger.Entry(house_account.id, Decimal("7.00")),
            ],
            idempotency_key=f"metrics-test-house-revenue-bump-{uuid.uuid4()}",
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{payments_server}/metrics")
        assert response.status_code == 200

        depth = _metric_value(response.text, "payout_queue_depth")
        assert depth >= 1

        reported_revenue = _metric_value(response.text, "house_revenue_total")
        assert reported_revenue == pytest.approx(float(before_revenue + Decimal("7.00")))
    finally:
        await redis.delete(PAYOUT_STREAM)
