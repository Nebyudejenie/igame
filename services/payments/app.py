"""Payments API: the one inbound HTTP surface a payment provider's server
reaches over the public internet (spec section 8.1-8.2). Deposit *creation*
is a plain Python call from services/bot/handlers.py, the same way the bot
already reads/writes the ledger directly for /balance and /history -- only
the webhook, which genuinely originates outside this process, needs to be
a real route.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

AGENT_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "agent"

from packages.core import metrics
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.redis_conn import get_redis
from packages.core.tracing import configure_tracing
from services.payments import agent_auth, deposits, device_registry
from services.payments.chapa import ChapaProvider
from services.payments.device_registry import DeviceIdentity
from services.payments.provider import InvalidSignature
from services.payments.telebirr_ingest import SOURCE_MACRODROID, ingest_sms_evidence
from services.payments.withdrawals import PAYOUT_STREAM


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_tracing("payments", settings.otel_exporter_endpoint)
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
    app.state.redis = get_redis()
    app.state.chapa = ChapaProvider(settings.chapa_api_key)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Zemen Game Payments API")


@app.post("/webhooks/chapa")
async def chapa_webhook(request: Request) -> Response:
    raw_body = await request.body()
    headers = dict(request.headers)
    try:
        outcome = await deposits.handle_webhook(
            app.state.pool, app.state.redis, app.state.chapa, headers=headers, raw_body=raw_body
        )
    except InvalidSignature:
        # Chapa's own docs: discard and do not process further. No detail
        # in the response body -- an attacker probing signature checks
        # doesn't get to learn which part of their forgery was wrong.
        return Response(status_code=401)
    return Response(status_code=200, content=outcome)


class TelebirrIngestRequest(BaseModel):
    raw_sms: str
    device_id: str


def _extract_bearer_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[len("Bearer ") :]


async def _authenticate_ingest_request(authorization: str) -> DeviceIdentity | None:
    """Two credentials are accepted, checked strongest-first -- MacroDroid
    is a thin adapter (section 114) with no financial logic of its own, so
    its only job here is proving it's really a trusted device before the
    real pipeline (ingest_sms_evidence) ever sees the payload:

    1. A per-device token (services/admin/queries.py's
       create_ingestion_device_admin, migrations/versions/e3a7c9f01b2d) --
       the preferred credential going forward: identifies exactly which
       phone this is, is independently revocable, and gets its own health
       counters updated by the caller. Returns the resolved identity.
    2. The legacy single shared settings.macrodroid_ingest_token -- kept
       working exactly as before (same env var, same hmac.compare_digest
       check, same 401/503 semantics) so a phone configured before this
       device registry existed is never broken by its introduction.
       Returns None (no specific device to attribute health counters to).

    A token that matches a real but *revoked* device is never allowed to
    silently fall through to the legacy check -- a revoked credential
    must stay revoked even if a legacy shared token also happens to be
    configured.
    """
    token = _extract_bearer_token(authorization)

    auth_result = await device_registry.authenticate_device(app.state.pool, token)
    if auth_result.identity is not None:
        return auth_result.identity
    if auth_result.revoked_device_pk is not None:
        await device_registry.record_auth_failure(app.state.pool, device_pk=auth_result.revoked_device_pk)
        raise HTTPException(status_code=401, detail="device revoked")

    settings = get_settings()
    if settings.macrodroid_ingest_token and hmac.compare_digest(token, settings.macrodroid_ingest_token):
        return None

    if not settings.macrodroid_ingest_token:
        # Genuinely nothing configured (neither a legacy token nor any
        # registered device) -- a server-side setup gap, not a bad
        # credential; distinguished from the 401 below exactly the way
        # this route always has, so an operator knows whether to check
        # their own token or ask an admin to finish configuring the
        # feature at all.
        any_device_registered = await app.state.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM ingestion_devices)"
        )
        if not any_device_registered:
            raise HTTPException(status_code=503, detail="telebirr ingestion is not configured")
    metrics.ingestion_device_auth_failures_total.labels(reason="unknown_token").inc()
    raise HTTPException(status_code=401, detail="invalid bearer token")


@app.post("/internal/telebirr/ingest")
async def telebirr_ingest(
    request: Request, authorization: str = Header("")
) -> dict[str, str | int | None]:
    """Accept SMS payload from MacroDroid or any HTTP client.

    The body can be either:
    - JSON: {"raw_sms": "...", "device_id": "..."}
    - Plain text: the full SMS string
    """
    device = await _authenticate_ingest_request(authorization)
    raw_body = await request.body()
    body_device_id = "unknown-device"
    if raw_body:
        raw_sms = raw_body.decode("utf-8", errors="replace")
        try:
            import json
            data = json.loads(raw_sms)
            raw_sms = data.get("raw_sms", str(data))
            body_device_id = data.get("device_id", body_device_id)
        except (json.JSONDecodeError, ValueError):
            pass  # plain text body is accepted as-is
    else:
        raw_sms = ""
    if not raw_sms.strip():
        raise HTTPException(status_code=422, detail="raw_sms_required")

    # A device's own registered device_id is authoritative once a
    # per-device token authenticated the request -- the client-supplied
    # body.device_id is never trusted as identity on its own (it's a
    # label, not a secret; the bearer token is what was actually
    # verified), only used as-is on the legacy shared-token path, where
    # it always has been.
    source_ref = device.device_id if device is not None else body_device_id
    outcome = await ingest_sms_evidence(
        app.state.pool, raw_sms=raw_sms, source=SOURCE_MACRODROID, source_ref=source_ref
    )
    if device is not None:
        await device_registry.record_ingestion_outcome(
            app.state.pool,
            device_pk=device.id,
            device_id=device.device_id,
            outcome_status=outcome.status,
            reason=outcome.reason,
        )
    return {
        "status": outcome.status,
        "evidence_id": outcome.evidence_id,
        "external_reference": outcome.external_reference,
        "reason": outcome.reason,
        "device_name": device.device_name if device is not None else None,
    }

# --- Payment Agent Portal ---------------------------------------------
#
# An agent's only prior identity was a row in payment_agents plus
# whatever Telegram already authenticated them as (see services/bot/
# handlers.py's /portal command and agent_auth.py's own docstring for
# why this reuses that rather than adding a second login system). These
# three routes are the entire portal backend: exchange a one-time
# Telegram-delivered link for a session, read who that session belongs
# to, and read that agent's own submission history -- nothing else.
# Never their raw SMS, never another agent's or player's data.


class AgentLoginRequest(BaseModel):
    token: str


def _agent_bearer_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[len("Bearer ") :]


async def _current_agent(authorization: Annotated[str, Header()] = "") -> agent_auth.AgentSession:
    token = _agent_bearer_token(authorization)
    session = await agent_auth.resolve_session(app.state.pool, app.state.redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return session


@app.post("/agent-portal/login")
async def agent_portal_login(body: AgentLoginRequest) -> dict[str, str]:
    session_token = await agent_auth.consume_login_token(app.state.pool, app.state.redis, body.token)
    if session_token is None:
        raise HTTPException(status_code=401, detail="invalid, expired, or already-used login link")
    return {"session_token": session_token}


@app.post("/agent-portal/logout")
async def agent_portal_logout(authorization: Annotated[str, Header()] = "") -> dict[str, bool]:
    token = _agent_bearer_token(authorization)
    await agent_auth.logout(app.state.redis, token)
    return {"ok": True}


@app.get("/agent-portal/me")
async def agent_portal_me(
    session: Annotated[agent_auth.AgentSession, Depends(_current_agent)],
) -> dict[str, str | int | None]:
    return {"telegram_user_id": session.telegram_user_id, "display_name": session.display_name}


@app.get("/agent-portal/submissions")
async def agent_portal_submissions(
    session: Annotated[agent_auth.AgentSession, Depends(_current_agent)],
) -> list[dict[str, str | float | None]]:
    # Deliberately not raw_sms, payer_name, payer_phone, recipient_name,
    # or recipient_phone -- an agent sees enough to know their own
    # submission's fate, never another person's private information (the
    # exact same fields the admin console's own non-finance roles are
    # kept away from, see docs/TELEBIRR_ROLES_AND_ACCESS.md).
    rows = await app.state.pool.fetch(
        """
        SELECT external_reference, amount, status, reject_reason, received_at
        FROM payment_evidence
        WHERE source = 'telegram_agent' AND source_ref = $1
        ORDER BY received_at DESC
        LIMIT 50
        """,
        str(session.telegram_user_id),
    )
    return [
        {
            "reference": row["external_reference"],
            "amount": float(row["amount"]) if row["amount"] is not None else None,
            "status": row["status"],
            "reject_reason": row["reject_reason"],
            "received_at": row["received_at"].isoformat(),
        }
        for row in rows
    ]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    # payout_queue_depth and house_revenue_total are "live" gauges (spec
    # section 10.4) -- queried fresh on every scrape rather than maintained
    # incrementally, since a scrape is exactly the moment Prometheus wants
    # their current value and this avoids a background polling loop for
    # numbers nothing else in this process needs continuously updated.
    depth = await app.state.redis.xlen(PAYOUT_STREAM)
    metrics.payout_queue_depth.set(depth)

    revenue = await app.state.pool.fetchval(
        """
        SELECT COALESCE(SUM(b.balance), 0)
        FROM account_balances b JOIN accounts a ON a.id = b.account_id
        WHERE a.kind = 'house_revenue'
        """
    )
    metrics.house_revenue_total.set(float(revenue))

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Mounted last, same discipline gateway/admin's own static mounts already
# follow: every real API route above must be registered first, or this
# catch-all swallows it. agent.arada.fun and payments.arada.fun currently
# route to this exact same container (see docs/PRODUCTION_DOMAIN_AND_
# CLOUDFLARE.md) -- the Agent Portal is genuinely part of the payments
# service, not a second service, so this static bundle also happens to
# be reachable at payments.arada.fun/. That's harmless: it carries no
# secrets and every real capability still requires the bearer-token/
# session checks above regardless of which hostname reached it.
app.mount("/", StaticFiles(directory=AGENT_WEB_DIR, html=True), name="agent_portal")
