"""HTTP-layer tests for services/admin/app.py: login end-to-end over real
requests, RBAC enforced through the actual dependency chain (not by calling
has_permission() directly), audit log immutability enforced by Postgres
itself, and IP allowlist enforcement.
"""

from decimal import Decimal

import asyncpg
import httpx
import pyotp
import pytest

from packages.core import ledger
from services.admin.app import app as admin_app
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin


async def _login(admin_server: str, username: str, password: str, totp_secret: str) -> str:
    code = pyotp.TOTP(totp_secret).now()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/auth/login",
            json={"username": username, "password": password, "totp_code": code},
        )
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


async def _auth_headers(admin_server: str, pool, role: str = "superadmin") -> dict[str, str]:
    admin_id, username, password, totp_secret = await create_test_admin(pool, role=role)
    token = await _login(admin_server, username, password, totp_secret)
    return {"Authorization": f"Bearer {token}"}


async def test_login_rejects_wrong_totp_over_http(admin_server, pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/auth/login",
            json={"username": username, "password": password, "totp_code": "000000"},
        )
    assert response.status_code == 401


async def test_protected_route_requires_bearer_token(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard")
    assert response.status_code == 401


async def test_protected_route_rejects_garbage_token(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{admin_server}/dashboard", headers={"Authorization": "Bearer not-a-real-token"}
        )
    assert response.status_code == 401


async def test_superadmin_can_reach_dashboard_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 200
    assert "active_rounds" in response.json()


async def test_rbac_support_cannot_adjust_balance_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "should be blocked by rbac", "request_id": "test-req-1"},
        )
    assert response.status_code == 403

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")


async def test_rbac_finance_can_adjust_balance_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "goodwill credit over http", "request_id": "test-req-2"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["ledger_transaction_id"]

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("15.00")


async def test_adjust_balance_over_http_is_idempotent_on_a_repeated_request_id(
    admin_server, pool, conn
):
    # The real double-click/retry path: two separate HTTP requests
    # carrying the same request_id (what a disabled-then-somehow-still-
    # fired duplicate click, or a browser/network retry, would send)
    # must credit exactly once.
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))
    body = {"amount": "5.00", "reason": "goodwill credit over http", "request_id": "test-req-repeat"}

    async with httpx.AsyncClient() as client:
        first = await client.post(f"{admin_server}/users/{user_id}/adjust", headers=headers, json=body)
        second = await client.post(f"{admin_server}/users/{user_id}/adjust", headers=headers, json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["ledger_transaction_id"] == second.json()["ledger_transaction_id"]

    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("15.00")  # credited once, not twice


async def test_adjust_balance_over_http_rejects_a_missing_request_id(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "missing request_id"},
        )
    assert response.status_code == 422


async def test_adjust_balance_over_http_rejects_done_as_a_reason(admin_server, pool, conn):
    """The exact real gap a 2026-09-23 house_float investigation found:
    a real 1,000 ETB balance adjustment was recorded with the reason
    "done" -- non-empty, so the pre-existing check let it through, but
    reconstructable by no one six months later. This is not a
    hypothetical placeholder; it's the actual value that was actually
    found on the actual platform."""
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={"amount": "5.00", "reason": "done", "request_id": "test-req-done"},
        )
    assert response.status_code == 422
    assert "reason must be a real, specific explanation" in response.json()["detail"]

    # Rejected, not silently truncated or accepted -- no money moved.
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("10.00")


async def test_adjust_balance_over_http_rejects_other_low_information_reasons(admin_server, pool, conn):
    """"done" is one real example, not the only one -- the same class of
    one-word non-answer must be rejected regardless of which specific
    word an admin happens to type."""
    headers = await _auth_headers(admin_server, pool, role="finance")
    for placeholder in ("test", "fix", "ok", "n/a", "temp", "short"):
        user_id = await create_funded_user(conn, Decimal("10.00"))
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{admin_server}/users/{user_id}/adjust",
                headers=headers,
                json={"amount": "5.00", "reason": placeholder, "request_id": f"test-req-{placeholder}"},
            )
        assert response.status_code == 422, f"{placeholder!r} was wrongly accepted as a reason"


async def test_adjust_balance_over_http_accepts_a_real_specific_reason(admin_server, pool, conn):
    """The fix must not become so strict it rejects genuine, accountable
    reasons -- only the low-information class above."""
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/adjust",
            headers=headers,
            json={
                "amount": "5.00",
                "reason": "Refunding player per support ticket #4821",
                "request_id": "test-req-real-reason",
            },
        )
    assert response.status_code == 200, response.text


async def test_create_room_rejects_a_malformed_stake_with_a_clean_error(admin_server, pool):
    # A real bug this reproduces: Decimal(str) raises decimal
    # .InvalidOperation, not ValueError -- create_room()'s own
    # `except ValueError` never caught it, so any malformed stake (an
    # empty string, a stray space, a comma instead of a decimal point --
    # all reachable through the console's own create-room form) fell
    # through as an unhandled exception and surfaced as an opaque 500,
    # not the clean 422 every other validation error on this route gives.
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/rooms",
            headers=headers,
            json={"code": f"bad-stake-{id(pool)}", "stake": "20,00"},
        )
    assert response.status_code == 422, response.text
    assert "decimal" in response.json()["detail"].lower()


async def test_update_room_rejects_a_malformed_stake_with_a_clean_error(admin_server, pool):
    # Same underlying gap as the create-room test above, on the edit
    # path: the console's edit form sends stake as a plain FormData
    # string, and update_room_admin() passed it straight through to the
    # UPDATE statement with no conversion at all -- asyncpg raises its
    # own DataError (also not a ValueError) for a non-numeric string
    # bound to a numeric column, equally uncaught before this fix.
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        create = await client.post(
            f"{admin_server}/rooms",
            headers=headers,
            json={"code": f"bad-stake-edit-{id(pool)}", "stake": "20.00"},
        )
        assert create.status_code == 200, create.text
        room_id = create.json()["room_id"]

        response = await client.patch(
            f"{admin_server}/rooms/{room_id}",
            headers=headers,
            json={"changes": {"stake": ""}, "reason": "testing malformed input"},
        )
    assert response.status_code == 422, response.text
    assert "decimal" in response.json()["detail"].lower()


async def test_rbac_support_cannot_set_kyc_level_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/kyc",
            headers=headers,
            json={"kyc_level": 2, "reason": "should be blocked by rbac"},
        )
    assert response.status_code == 403

    kyc_level = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert kyc_level == 0


async def test_rbac_finance_can_set_kyc_level_over_http(admin_server, pool, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/users/{user_id}/kyc",
            headers=headers,
            json={"kyc_level": 2, "reason": "ID documents reviewed and verified"},
        )
    assert response.status_code == 200, response.text

    kyc_level = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert kyc_level == 2


async def test_rbac_support_cannot_view_risk_screen_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="support")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/risk/shared-payout-accounts", headers=headers)
    assert response.status_code == 403


async def test_rbac_ops_can_view_risk_screen_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="ops")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/risk/shared-payout-accounts", headers=headers)
    assert response.status_code == 200, response.text

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/risk/repeat-pairings", headers=headers)
    assert response.status_code == 200, response.text


async def test_audit_log_route_requires_superadmin(admin_server, pool):
    for role in ("support", "finance", "ops"):
        headers = await _auth_headers(admin_server, pool, role=role)
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/audit-log", headers=headers)
        assert response.status_code == 403, role

    headers = await _auth_headers(admin_server, pool, role="superadmin")
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/audit-log", headers=headers)
    assert response.status_code == 200


async def test_logout_invalidates_token_over_http(admin_server, pool):
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    token = await _login(admin_server, username, password, totp_secret)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 200

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{admin_server}/auth/logout", headers=headers)
    assert response.status_code == 200

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/dashboard", headers=headers)
    assert response.status_code == 401


async def test_ip_allowlist_blocks_disallowed_source(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/dashboard", headers=headers)
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_ip_allowlist_trusts_cf_connecting_ip_over_the_raw_connection_ip(admin_server, pool):
    """The actual regression this exists to prevent: once this service
    sits behind Cloudflare Tunnel + Traefik, request.client.host is
    always the proxy's own address, not the real visitor's -- an
    allowlist keyed on the raw connection IP would then either block
    every real visitor (Traefik's IP was never allowlisted) or, worse,
    silently allow everyone (if someone allowlisted Traefik's IP itself
    to work around that). CF-Connecting-IP is what Cloudflare's own edge
    sets to the true visitor IP, so the allowlist must honor it -- both
    directions checked here: an allowed CF-Connecting-IP must pass even
    though the raw test-client connection IP itself was never
    allowlisted, and a disallowed one must still be blocked.
    """
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    admin_app.state.ip_allowlist = ["203.0.113.7"]
    try:
        async with httpx.AsyncClient() as client:
            allowed = await client.get(
                f"{admin_server}/dashboard",
                headers={**headers, "CF-Connecting-IP": "203.0.113.7"},
            )
            blocked = await client.get(
                f"{admin_server}/dashboard",
                headers={**headers, "CF-Connecting-IP": "198.51.100.9"},
            )
        assert allowed.status_code == 200
        assert blocked.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_login_endpoint_is_blocked_by_the_ip_allowlist(admin_server, pool):
    # Regression: a real code review pass caught this endpoint bypassing
    # the IP allowlist entirely. It's the one route that can never go
    # through current_admin() -- there's no bearer token yet, that's the
    # whole point of logging in -- so, unlike every other route, it needs
    # its own direct _check_ip_allowlist() call the same way /metrics
    # does. It's also the single most exposed route to check it on: an
    # attacker outside the allowlist could otherwise still throw
    # password/TOTP guesses at it even with every other admin route
    # already unreachable to them.
    admin_id, username, password, totp_secret = await create_test_admin(pool)
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        code = pyotp.TOTP(totp_secret).now()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{admin_server}/auth/login",
                json={"username": username, "password": password, "totp_code": code},
            )
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_metrics_endpoint_is_reachable_with_no_session_token(admin_server):
    # Unlike every other route, /metrics doesn't require a bearer session
    # (a Prometheus scraper can't practically present one) -- but it must
    # still go through the IP allowlist, confirmed by the next test.
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/metrics")
    assert response.status_code == 200
    assert "house_revenue_total" in response.text


async def test_metrics_endpoint_is_blocked_by_the_ip_allowlist(admin_server):
    # Regression: a real code review pass caught this endpoint bypassing
    # the IP allowlist entirely -- house_revenue_total (live revenue in
    # ETB), deposit_outcomes_total, and payout_queue_depth were reachable
    # by anyone on the network with no protection at all, unlike every
    # other route in this file.
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/metrics")
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_console_frontend_is_reachable_with_no_allowlist(admin_server):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/console/")
    assert response.status_code == 200
    assert "Zemen Game Admin" in response.text


async def test_console_frontend_is_blocked_by_the_ip_allowlist(admin_server):
    # The frontend mounted at /console is plain StaticFiles -- unlike
    # every API route, it has no Depends(current_admin) of its own to run
    # the allowlist check through, which is exactly the gap /metrics and
    # /auth/login were each separately caught with before (see their own
    # tests above). This is the same regression guard for the dedicated
    # _console_frontend_ip_allowlist middleware that closes it here.
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{admin_server}/console/")
        assert response.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_fastapis_own_docs_routes_are_reachable_with_no_allowlist(admin_server):
    async with httpx.AsyncClient() as client:
        docs = await client.get(f"{admin_server}/docs")
        openapi = await client.get(f"{admin_server}/openapi.json")
        redoc = await client.get(f"{admin_server}/redoc")
    assert docs.status_code == 200
    assert openapi.status_code == 200
    assert redoc.status_code == 200


async def test_fastapis_own_docs_routes_are_blocked_by_the_ip_allowlist(admin_server):
    # Regression: a code-review pass that actually enumerated app.routes
    # (not just routes anyone had written by hand) found /docs,
    # /openapi.json, and /redoc bypassing the allowlist entirely --
    # FastAPI adds these automatically, so a search for a hand-written
    # route missing the check (the way /metrics and /auth/login were
    # each separately caught before) would never find them. They expose
    # this real-money admin panel's entire API surface -- every route,
    # every request/response field -- to anyone on the network. Same
    # regression guard as the /metrics and /console tests above, for the
    # same underlying middleware, now covering these too.
    admin_app.state.ip_allowlist = ["10.0.0.1"]
    try:
        async with httpx.AsyncClient() as client:
            docs = await client.get(f"{admin_server}/docs")
            openapi = await client.get(f"{admin_server}/openapi.json")
            redoc = await client.get(f"{admin_server}/redoc")
        assert docs.status_code == 403
        assert openapi.status_code == 403
        assert redoc.status_code == 403
    finally:
        admin_app.state.ip_allowlist = []


async def test_audit_log_is_immutable_at_the_database_level(pool):
    admin_id, *_ = await create_test_admin(pool)
    row = await pool.fetchrow(
        "INSERT INTO admin_audit_log (admin_id, action, target_type, target_id) "
        "VALUES ($1, 'test.action', 'user', '1') RETURNING id",
        admin_id,
    )

    with pytest.raises(asyncpg.PostgresError):
        await pool.execute(
            "UPDATE admin_audit_log SET reason = 'tampered' WHERE id = $1", row["id"]
        )

    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("DELETE FROM admin_audit_log WHERE id = $1", row["id"])
