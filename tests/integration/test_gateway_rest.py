"""Tests for the gateway's REST endpoints (/api/me, /api/history,
/api/deposit, /api/withdraw, /api/rounds/{id}/fairness) -- the Mini App
wallet screen's data source, the deposit/withdraw tabs, and the player-
facing provably-fair verification the results screen's "Verify draw"
button calls. Same auth boundary as the WebSocket handshake:
`Authorization: tma <initData>`, validated the same way.
"""

import asyncio
import hashlib
from decimal import Decimal

import httpx
import pytest

from packages.core.phone_crypto import encrypt_phone, phone_lookup_hash
from services.admin import queries as admin_queries
from services.engine.round_engine import RoundEngine, load_room_config
from services.gateway.app import app as gateway_app
from tests.integration.conftest import build_init_data, create_funded_user, create_room, fund_user, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_payments_deposits import FakePaymentProvider
from tests.integration.test_payout_worker import FakePayoutProvider
from tests.integration.test_round_engine import wait_until


def http_base(gateway_server: str) -> str:
    return gateway_server.replace("ws://", "http://").replace("/ws", "")


async def _set_phone(conn, user_id: int, phone: str, *, extra_sql: str = "") -> None:
    await conn.execute(
        f"UPDATE users SET phone_e164_encrypted = $2, phone_lookup_hash = $3{extra_sql} WHERE id = $1",
        user_id,
        encrypt_phone(phone),
        phone_lookup_hash(phone),
    )


async def test_api_me_requires_authorization_header(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/me")
    assert response.status_code == 401


async def test_api_me_rejects_tampered_init_data(gateway_server):
    telegram_id = next_telegram_id()
    raw = build_init_data(telegram_id)
    tampered = raw[:-4] + "0000"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {tampered}"}
        )
    assert response.status_code == 401


async def test_api_me_returns_real_balance(gateway_server, pool, conn):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    assert response.json() == {"cash": "0.00", "bonus": "0.00", "locked": "0.00"}

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("42.50"))

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.json()["cash"] == "42.50"


async def test_api_history_empty_for_new_user(gateway_server):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/history", headers={"Authorization": f"tma {init_data}"}
        )
    assert response.status_code == 200
    assert response.json() == []


@pytest.fixture
def fake_chapa():
    """/api/deposit and /api/withdraw read their provider from
    app.state.chapa (a real ChapaProvider by default, set once at gateway
    startup) -- swap in a fake for the duration of one test, same pattern
    test_admin_app.py uses for app.state.ip_allowlist.
    """
    original = gateway_app.state.chapa
    fake = FakePaymentProvider()
    gateway_app.state.chapa = fake
    try:
        yield fake
    finally:
        gateway_app.state.chapa = original


async def test_api_deposit_requires_auth(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit", json={"amount": "100"}
        )
    assert response.status_code == 401


async def test_api_deposit_rejects_invalid_amount(gateway_server):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "not-a-number"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_amount"


async def test_api_deposit_rejects_below_minimum(gateway_server, pool, conn):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await _set_phone(conn, user_row["id"], f"+2519{telegram_id % 100_000_000:08d}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "1"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "below_minimum"


async def test_api_deposit_requires_phone_on_file(gateway_server, fake_chapa):
    # get_or_create_user_by_telegram_id() (the gateway's own auth path)
    # creates a user row with no phone_e164 -- this is exactly that user,
    # who has never gone through the bot's phone-share registration.
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "100"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "phone_required"


async def test_api_deposit_blocked_when_self_excluded(gateway_server, pool, conn, fake_chapa):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await _set_phone(
        conn, user_row["id"], f"+2519{telegram_id % 100_000_000:08d}", extra_sql=", status = 'self_excluded'"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "100"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "self_excluded"


async def test_api_deposit_succeeds_and_returns_a_checkout_url(gateway_server, pool, conn, fake_chapa):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await _set_phone(conn, user_row["id"], f"+2519{telegram_id % 100_000_000:08d}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "150"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checkout_url"] == f"https://pay.test/{body['our_ref']}"
    assert body["our_ref"].startswith("DEP-")

    row = await pool.fetchrow("SELECT status, amount FROM payments WHERE our_ref = $1", body["our_ref"])
    assert row["status"] == "processing"
    assert row["amount"] == Decimal("150.00")


async def test_api_deposit_rate_limited_after_five_in_a_row(gateway_server, pool, conn, fake_chapa):
    # Spec section 9.2: "deposit 5/hour" -- the same rate limit
    # test_bot_handlers.py proves through the bot's /deposit command,
    # proven here through the Mini App's REST path instead, since both
    # call the same create_deposit_intent() but are two independent
    # callers that could each forget to wire the check correctly.
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await _set_phone(conn, user_row["id"], f"+2519{telegram_id % 100_000_000:08d}")

    async with httpx.AsyncClient() as client:
        for i in range(5):
            response = await client.post(
                f"{http_base(gateway_server)}/api/deposit",
                headers={"Authorization": f"tma {init_data}"},
                json={"amount": "10"},
            )
            assert response.status_code == 200, f"attempt {i + 1}: {response.text}"

        response = await client.post(
            f"{http_base(gateway_server)}/api/deposit",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "10"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "rate_limited"


@pytest.fixture
def fake_chapa_payout():
    original = gateway_app.state.chapa
    fake = FakePayoutProvider()
    gateway_app.state.chapa = fake
    try:
        yield fake
    finally:
        gateway_app.state.chapa = original


async def test_api_withdraw_requires_auth(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            json={"amount": "100", "account_ref": "0911223344", "holder_name": "Test"},
        )
    assert response.status_code == 401


async def test_api_withdraw_rejects_insufficient_balance(gateway_server, fake_chapa_payout):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "1000", "account_ref": "0911223344", "holder_name": "Test Holder"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "insufficient_balance"


async def test_api_withdraw_succeeds_and_locks_funds(gateway_server, pool, conn, fake_chapa_payout):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("500.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{http_base(gateway_server)}/api/withdraw",
            headers={"Authorization": f"tma {init_data}"},
            json={"amount": "200", "account_ref": "0911223344", "holder_name": "Test Holder"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["our_ref"].startswith("WD-")
    assert body["status"] in ("approved", "review")


# --- admin availability toggle enforced server-side, not just client-side --
# A code-review pass caught that GET /api/payment-methods and the bot's own
# /deposit and /withdraw commands already honored an admin's live
# payment_provider_availability toggle, but the three endpoints that
# actually *move money* (/api/deposit, /api/deposit/manual, /api/withdraw)
# never checked it at all -- only the static app.state.chapa/None check.
# The Mini App JS hid the disabled option, but a client with the page
# already open (or any direct POST) could still complete a "disabled"
# deposit/withdrawal.


async def test_api_deposit_refuses_when_admin_disables_chapa(gateway_server, pool, conn, fake_chapa):
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="chapa", direction="in", enabled=False,
        reason="test: simulate an admin disabling chapa deposits", ip_address=None,
    )
    try:
        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        await _set_phone(conn, user_row["id"], f"+2519{telegram_id % 100_000_000:08d}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{http_base(gateway_server)}/api/deposit",
                headers={"Authorization": f"tma {init_data}"},
                json={"amount": "150"},
            )
        assert response.status_code == 503
    finally:
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="chapa", direction="in", enabled=True,
            reason="test cleanup", ip_address=None,
        )


async def test_api_deposit_manual_refuses_when_admin_disables_manual(gateway_server, pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    destination_id = await admin_queries.create_manual_payment_destination_admin(
        pool, admin_id=admin_id, method_kind="telebirr", account_ref="0911000000",
        account_name="Zemen Game PLC", instructions=None, ip_address="10.0.0.1",
    )
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="manual", direction="in", enabled=False,
        reason="test: simulate an admin disabling manual deposits", ip_address=None,
    )
    try:
        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{http_base(gateway_server)}/api/deposit/manual",
                headers={"Authorization": f"tma {init_data}"},
                json={"amount": "150", "manual_destination_id": destination_id, "external_reference": "FT26-x"},
            )
        assert response.status_code == 503

        # Never even reached manual.create_manual_deposit_request() --
        # confirms this is a real refusal, not a downstream rejection
        # that happens to also return a 4xx/5xx.
        row = await pool.fetchval(
            "SELECT id FROM payments WHERE user_id = (SELECT id FROM users WHERE telegram_id = $1) "
            "AND direction = 'in' AND provider = 'manual'",
            telegram_id,
        )
        assert row is None
    finally:
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="manual", direction="in", enabled=True,
            reason="test cleanup", ip_address=None,
        )


async def test_api_withdraw_refuses_when_admin_disables_the_requested_provider(gateway_server, pool, conn):
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="manual", direction="out", enabled=False,
        reason="test: simulate an admin disabling manual withdrawals", ip_address=None,
    )
    try:
        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            await client.get(f"{http_base(gateway_server)}/api/me", headers={"Authorization": f"tma {init_data}"})
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        await fund_user(conn, user_row["id"], Decimal("500.00"))

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{http_base(gateway_server)}/api/withdraw",
                headers={"Authorization": f"tma {init_data}"},
                json={
                    "amount": "200", "account_ref": "0911223344", "holder_name": "Test Holder",
                    "provider": "manual",
                },
            )
        assert response.status_code == 503
    finally:
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="manual", direction="out", enabled=True,
            reason="test cleanup", ip_address=None,
        )


async def test_api_round_fairness_requires_auth(gateway_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{http_base(gateway_server)}/api/rounds/1/fairness")
    assert response.status_code == 401


async def test_api_round_fairness_404_for_unknown_round(gateway_server):
    telegram_id = next_telegram_id()
    init_data = build_init_data(telegram_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{http_base(gateway_server)}/api/rounds/999999999/fairness",
            headers={"Authorization": f"tma {init_data}"},
        )
    assert response.status_code == 404


async def test_api_round_fairness_not_revealed_before_round_finishes(
    gateway_server, pool, redis, card_pool, conn
):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=5)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        round_id = engine.round_id

        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{http_base(gateway_server)}/api/rounds/{round_id}/fairness",
                headers={"Authorization": f"tma {init_data}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["revealed"] is False
        assert "server_seed_hash" in body
        assert "server_seed" not in body
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_api_round_fairness_revealed_and_independently_verifiable(
    gateway_server, pool, redis, card_pool, conn
):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=10)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p2, 2)).ok

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )

        telegram_id = next_telegram_id()
        init_data = build_init_data(telegram_id)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{http_base(gateway_server)}/api/rounds/{round_row['id']}/fairness",
                headers={"Authorization": f"tma {init_data}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["revealed"] is True
        assert body["verified"] is True
        assert len(body["draw_order"]) == 75

        # Independent re-check from the test's own side, not just trusting
        # the server's "verified" flag: hashing the revealed seed really
        # does reproduce the hash that was committed before the round ran.
        assert hashlib.sha256(bytes.fromhex(body["server_seed"])).hexdigest() == body["server_seed_hash"]
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)
