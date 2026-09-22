"""Enterprise SMS Control Plane API -- a genuinely separate product from
the Bingo admin console (its own subdomain, its own frontend, its own
route surface), sharing that console's existing admin authentication,
RBAC, and audit trail rather than growing a second one (DECISIONS.md,
2026-09-07). Node registration/lifecycle routes are admin-authenticated;
the small "/v1/nodes/*" surface below them is the generic delivery-node
protocol, authenticated by a completely separate per-node credential
(services/sms/node_auth.py) -- every node is untrusted external
infrastructure, never handed an admin bearer token.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncpg
import structlog
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.redis_conn import get_redis
from packages.core.sms import campaigns as campaigns_module
from packages.core.sms import messages as messages_module
from packages.core.sms import nodes as nodes_module
from packages.core.sms import csv_import as csv_import_module
from packages.core.sms.audience import InvalidAudienceFilter
from packages.core.sms.campaigns import CampaignNotFound, InvalidTransition
from packages.core.sms.csv_import import CsvImportError, ImportFormat, MAX_CSV_SIZE_BYTES
from packages.core.sms.messages import MessageNotFound, NotOwnedByNode
from packages.core.sms.nodes import NodeNotFound
from services.admin import auth
from services.admin.auth import AdminSession
from services.admin.rbac import has_permission
from services.sms import admin_queries
from services.sms.node_auth import CurrentNode

logger = structlog.get_logger()

SMS_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "sms"

# How often the in-process reconciliation sweep looks for silently
# in-flight messages -- this is a background asyncio task inside this
# same service process (like services/bot/app.py's own periodic tasks),
# not a separate deployable worker: at this pass's real traffic volume, a
# dedicated worker process is an extraction to make once volume justifies
# it, not before (DECISIONS.md).
RECONCILE_INTERVAL_SECONDS = 60


async def _reconcile_loop(pool: asyncpg.Pool, tenant_id: int) -> None:
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    reconciled = await messages_module.reconcile_stale_in_flight(conn, tenant_id=tenant_id)
            if reconciled:
                logger.info("sms_reconciliation_swept", reconciled=reconciled)
        except Exception:
            logger.exception("sms_reconciliation_sweep_failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
    app.state.redis = get_redis()
    tenant_id = await app.state.pool.fetchval("SELECT id FROM sms_tenants WHERE slug = 'default'")
    assert tenant_id is not None, "sms_tenants seed row missing -- run migrations"
    app.state.tenant_id = tenant_id
    sweep_task = asyncio.create_task(_reconcile_loop(app.state.pool, tenant_id))
    try:
        yield
    finally:
        sweep_task.cancel()
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Zemen Game SMS Control Plane API")


async def current_admin(request: Request, authorization: str = Header(default="")) -> AdminSession:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer session token")
    token = authorization[len("Bearer ") :]
    session = await auth.resolve_session(app.state.pool, app.state.redis, token)
    if session is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    return session


def require(permission: str) -> Any:
    async def _dependency(admin: Annotated[AdminSession, Depends(current_admin)]) -> AdminSession:
        if not has_permission(admin.role, permission):
            raise HTTPException(status_code=403, detail=f"role {admin.role!r} lacks {permission!r}")
        return admin

    return _dependency


def _client_ip(request: Request) -> str:
    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    if cf_connecting_ip:
        return cf_connecting_ip
    return request.client.host if request.client else "unknown"


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str


@app.post("/auth/login")
async def login(body: LoginRequest) -> dict[str, str]:
    """A thin passthrough to the exact same auth.login()/resolve_session()
    the Bingo admin console itself uses -- an admin who already has a
    working username/password/TOTP there can use the same credentials
    here; there is no second account system to provision or keep in sync.
    """
    try:
        token = await auth.login(
            app.state.pool, app.state.redis,
            username=body.username, password=body.password, totp_code=body.totp_code,
        )
    except auth.LoginRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth.LoginFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    session = await auth.resolve_session(app.state.pool, app.state.redis, token)
    assert session is not None
    return {"token": token, "role": session.role}


@app.post("/auth/logout")
async def logout(
    admin: Annotated[AdminSession, Depends(current_admin)], authorization: str = Header(default="")
) -> dict[str, str]:
    token = authorization[len("Bearer ") :]
    await auth.logout(app.state.redis, token)
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- overview --------------------------------------------------------------


@app.get("/overview")
async def overview(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> dict[str, Any]:
    tenant_id = app.state.tenant_id
    queue_rows = await app.state.pool.fetch(
        "SELECT status, count(*) AS n FROM sms_messages WHERE tenant_id = $1 GROUP BY status",
        tenant_id,
    )
    node_rows = await app.state.pool.fetch(
        "SELECT status, count(*) AS n FROM sms_delivery_nodes WHERE tenant_id = $1 GROUP BY status",
        tenant_id,
    )
    campaign_rows = await app.state.pool.fetch(
        "SELECT status, count(*) AS n FROM sms_campaigns WHERE tenant_id = $1 GROUP BY status",
        tenant_id,
    )
    return {
        "messages_by_status": {r["status"]: r["n"] for r in queue_rows},
        "nodes_by_status": {r["status"]: r["n"] for r in node_rows},
        "campaigns_by_status": {r["status"]: r["n"] for r in campaign_rows},
    }


# --- campaigns ---------------------------------------------------------


def _campaign_to_dict(campaign: campaigns_module.Campaign) -> dict[str, Any]:
    return {
        "id": campaign.id, "name": campaign.name, "template_id": campaign.template_id,
        "body_override": campaign.body_override, "status": campaign.status,
        "audience_filter": campaign.audience_filter, "recipient_count": campaign.recipient_count,
        "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
        "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
        "required_fleet_group": campaign.required_fleet_group,
        "import_job_id": campaign.import_job_id,
    }


@app.get("/campaigns")
async def list_campaigns(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> list[dict[str, Any]]:
    campaigns = await campaigns_module.list_campaigns(app.state.pool, tenant_id=app.state.tenant_id)
    return [_campaign_to_dict(c) for c in campaigns]


@app.get("/campaigns/{campaign_id}")
async def get_campaign(
    admin: Annotated[AdminSession, Depends(require("sms:view"))], campaign_id: int
) -> dict[str, Any]:
    try:
        campaign = await campaigns_module.get_campaign(app.state.pool, campaign_id=campaign_id)
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


class CreateCampaignRequest(BaseModel):
    name: str
    template_id: int | None = None
    body_override: str | None = None
    audience_filter: dict[str, Any] = {}
    # NULL/omitted -- today's only behavior -- means any active node may
    # deliver this campaign; set to restrict delivery to nodes whose own
    # fleet_group matches exactly (packages/core/sms/messages.py's claim
    # query enforces this).
    required_fleet_group: str | None = None
    # Set to create this campaign from a completed CSV import
    # (POST /import/csv) instead of the usual audience_filter -- see
    # packages/core/sms/campaigns.py's own create_campaign() for the
    # exact template_id/body_override requirement this relaxes.
    import_job_id: int | None = None


@app.post("/campaigns")
async def create_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))],
    body: CreateCampaignRequest,
) -> dict[str, Any]:
    try:
        campaign = await admin_queries.create_campaign_admin(
            app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id, name=body.name,
            template_id=body.template_id, body_override=body.body_override,
            audience_filter=body.audience_filter, required_fleet_group=body.required_fleet_group,
            import_job_id=body.import_job_id, ip_address=_client_ip(request),
        )
    except InvalidAudienceFilter as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


@app.post("/campaigns/{campaign_id}/validate")
async def validate_campaign(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))], campaign_id: int
) -> dict[str, Any]:
    try:
        campaign = await admin_queries.validate_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


@app.post("/campaigns/{campaign_id}/start")
async def start_campaign(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:campaigns:approve"))], campaign_id: int
) -> dict[str, Any]:
    """Gated by sms:campaigns:approve, not sms:campaigns:manage -- the one
    action in this whole product that actually sends real messages to real
    people (see services/admin/rbac.py's own comment on this permission).
    """
    try:
        campaign, created = await admin_queries.start_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**_campaign_to_dict(campaign), "messages_created": created}


@app.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))], campaign_id: int
) -> dict[str, Any]:
    try:
        campaign = await admin_queries.pause_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))], campaign_id: int
) -> dict[str, Any]:
    try:
        campaign = await admin_queries.resume_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


class ScheduleCampaignRequest(BaseModel):
    scheduled_at: datetime


@app.post("/campaigns/{campaign_id}/schedule")
async def schedule_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))],
    campaign_id: int,
    body: ScheduleCampaignRequest,
) -> dict[str, Any]:
    try:
        campaign = await admin_queries.schedule_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id,
            scheduled_at=body.scheduled_at, ip_address=_client_ip(request),
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


class CancelCampaignRequest(BaseModel):
    reason: str


@app.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))],
    campaign_id: int,
    body: CancelCampaignRequest,
) -> dict[str, Any]:
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    try:
        campaign = await admin_queries.cancel_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id,
            reason=body.reason, ip_address=_client_ip(request),
        )
    except CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_to_dict(campaign)


# --- CSV bulk import -------------------------------------------------------
#
# Gated by the exact same permissions campaign creation/sending already
# use: uploading and previewing a CSV is "drafting a campaign" (sms:
# campaigns:manage, the same permission POST /campaigns needs), and the
# one thing that actually sends real messages -- POST /campaigns/{id}/
# start -- is already gated by the narrower sms:campaigns:approve above.
# A CSV import is simply another way to create a campaign; it never
# needs its own permission namespace.


async def _read_upload_bounded(file: UploadFile, *, max_bytes: int) -> bytes:
    """Reads at most max_bytes + one chunk, aborting the instant that's
    exceeded -- never lets a maliciously (or accidentally) huge upload
    run to completion in memory before csv_import.py's own byte-count
    check would otherwise fire (Section 19: "oversized uploads",
    "memory exhaustion").
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"file exceeds the {max_bytes:,} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/import/csv")
async def upload_csv(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:campaigns:manage"))],
    file: UploadFile,
    format: ImportFormat = Form(...),
    # Required, not defaulted -- a missing/blank value would silently
    # reopen the double-submission gap this exists to close (mirrors
    # AdjustBalanceRequest.request_id's own comment in services/admin/
    # app.py). The frontend generates one fresh UUID per upload attempt
    # and resends the identical value on any retry of that same attempt.
    idempotency_key: str = Form(...),
) -> dict[str, Any]:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="idempotency_key is required")
    raw_bytes = await _read_upload_bounded(file, max_bytes=MAX_CSV_SIZE_BYTES)
    try:
        summary = await admin_queries.upload_csv_admin(
            app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
            raw_bytes=raw_bytes, original_filename=file.filename, format=format,
            idempotency_key=idempotency_key.strip(), ip_address=_client_ip(request),
        )
    except CsvImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "job_id": summary.job_id, "format": summary.format, "total_rows": summary.total_rows,
        "valid_rows": summary.valid_rows, "invalid_rows": summary.invalid_rows,
        "duplicate_rows": summary.duplicate_rows, "suppressed_rows": summary.suppressed_rows,
        "unsupported_columns": summary.unsupported_columns,
        "will_send": summary.valid_rows,
    }


@app.get("/import/{job_id}")
async def get_import_job(
    admin: Annotated[AdminSession, Depends(require("sms:view"))], job_id: int
) -> dict[str, Any]:
    try:
        summary = await csv_import_module.get_import_summary(
            app.state.pool, job_id=job_id, tenant_id=app.state.tenant_id
        )
    except CsvImportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "job_id": summary.job_id, "format": summary.format, "total_rows": summary.total_rows,
        "valid_rows": summary.valid_rows, "invalid_rows": summary.invalid_rows,
        "duplicate_rows": summary.duplicate_rows, "suppressed_rows": summary.suppressed_rows,
    }


_IMPORT_ROW_STATUSES = {"valid", "invalid", "duplicate", "suppressed"}


@app.get("/import/{job_id}/rows")
async def list_import_rows(
    admin: Annotated[AdminSession, Depends(require("sms:view"))],
    job_id: int,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if status is not None and status not in _IMPORT_ROW_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_IMPORT_ROW_STATUSES)}")
    if limit > 1000:
        limit = 1000
    try:
        rows = await csv_import_module.list_import_rows(
            app.state.pool, job_id=job_id, tenant_id=app.state.tenant_id,
            status=status, limit=limit, offset=offset,  # type: ignore[arg-type]  # narrowed above
        )
    except CsvImportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return rows


# --- messages ------------------------------------------------------------


def _message_to_dict(message: messages_module.Message) -> dict[str, Any]:
    return {
        "id": message.id, "campaign_id": message.campaign_id, "contact_id": message.contact_id,
        "phone_e164": message.phone_e164, "body": message.body, "segment_count": message.segment_count,
        "priority": message.priority, "status": message.status, "attempt_count": message.attempt_count,
    }


@app.get("/messages")
async def list_messages(
    admin: Annotated[AdminSession, Depends(require("sms:view"))],
    campaign_id: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    result = await messages_module.list_messages(
        app.state.pool, tenant_id=app.state.tenant_id, campaign_id=campaign_id, status=status
    )
    return [_message_to_dict(m) for m in result]


@app.get("/campaigns/{campaign_id}/messages")
async def list_campaign_messages(
    admin: Annotated[AdminSession, Depends(require("sms:view"))], campaign_id: int
) -> list[dict[str, Any]]:
    result = await messages_module.list_messages(app.state.pool, tenant_id=app.state.tenant_id, campaign_id=campaign_id)
    return [_message_to_dict(m) for m in result]


# --- templates -------------------------------------------------------------


@app.get("/templates")
async def list_templates(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> list[dict[str, Any]]:
    rows = await app.state.pool.fetch(
        "SELECT id, name, body, variables, is_active FROM sms_templates WHERE tenant_id = $1 ORDER BY id DESC",
        app.state.tenant_id,
    )
    return [dict(row) for row in rows]


class CreateTemplateRequest(BaseModel):
    name: str
    body: str


@app.post("/templates")
async def create_template(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:templates:manage"))],
    body: CreateTemplateRequest,
) -> dict[str, Any]:
    try:
        return await admin_queries.create_template_admin(
            app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
            name=body.name, body=body.body, ip_address=_client_ip(request),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="a template with this name already exists") from exc


# --- contacts ----------------------------------------------------------


@app.get("/contacts")
async def list_contacts(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> list[dict[str, Any]]:
    rows = await app.state.pool.fetch(
        """
        SELECT id, phone_e164, display_name, attributes, opted_out
        FROM sms_contacts WHERE tenant_id = $1 ORDER BY id DESC LIMIT 500
        """,
        app.state.tenant_id,
    )
    return [dict(row) for row in rows]


class CreateContactRequest(BaseModel):
    phone_e164: str
    display_name: str | None = None
    attributes: dict[str, Any] = {}


@app.post("/contacts")
async def create_contact(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:contacts:manage"))],
    body: CreateContactRequest,
) -> dict[str, Any]:
    return await admin_queries.create_contact_admin(
        app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
        phone_e164=body.phone_e164, display_name=body.display_name, attributes=body.attributes,
        ip_address=_client_ip(request),
    )


# --- suppressions --------------------------------------------------------


@app.get("/suppressions")
async def list_suppressions(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> list[dict[str, Any]]:
    from packages.core.sms import compliance

    rows = await compliance.list_suppressions(app.state.pool, tenant_id=app.state.tenant_id)
    return [
        {"id": s.id, "phone_e164": s.phone_e164, "reason": s.reason, "note": s.note, "created_at": s.created_at.isoformat()}
        for s in rows
    ]


class AddSuppressionRequest(BaseModel):
    phone_e164: str
    reason: str = "manual"
    note: str | None = None


@app.post("/suppressions")
async def add_suppression(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:compliance:manage"))],
    body: AddSuppressionRequest,
) -> dict[str, Any]:
    suppression = await admin_queries.add_suppression_admin(
        app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
        phone_e164=body.phone_e164, reason=body.reason, note=body.note, ip_address=_client_ip(request),
    )
    return {"id": suppression.id, "phone_e164": suppression.phone_e164, "reason": suppression.reason}


@app.delete("/suppressions/{phone_e164}")
async def remove_suppression(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:compliance:manage"))],
    phone_e164: str,
) -> dict[str, bool]:
    removed = await admin_queries.remove_suppression_admin(
        app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
        phone_e164=phone_e164, ip_address=_client_ip(request),
    )
    return {"removed": removed}


# --- delivery nodes (admin-managed lifecycle) -----------------------------


def _node_to_dict(node: nodes_module.DeliveryNode) -> dict[str, Any]:
    return {
        "id": node.id, "name": node.name, "fleet_group": node.fleet_group, "status": node.status,
        # A computed overlay (degraded/offline), never a second stored
        # status -- see packages/core/sms/nodes.py::display_status().
        "display_status": nodes_module.display_status(node),
        "health_score": node.health_score,
        "last_heartbeat_at": node.last_heartbeat_at.isoformat() if node.last_heartbeat_at else None,
        "app_version": node.app_version,
        "max_concurrent_jobs": node.max_concurrent_jobs,
        "protocol_version": node.protocol_version,
    }


@app.get("/nodes")
async def list_nodes(admin: Annotated[AdminSession, Depends(require("sms:view"))]) -> list[dict[str, Any]]:
    result = await nodes_module.list_nodes(app.state.pool, tenant_id=app.state.tenant_id)
    return [_node_to_dict(n) for n in result]


class CreateNodeRequest(BaseModel):
    name: str
    fleet_group: str = "default"


@app.post("/nodes")
async def create_node(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))],
    body: CreateNodeRequest,
) -> dict[str, Any]:
    try:
        node, raw_token = await admin_queries.create_node_admin(
            app.state.pool, tenant_id=app.state.tenant_id, admin_id=admin.admin_id,
            name=body.name, fleet_group=body.fleet_group, ip_address=_client_ip(request),
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="a node with this name already exists") from exc
    # The only moment this raw credential is ever visible -- shown once,
    # exactly like a new admin's TOTP secret (services/admin/auth.py).
    return {**_node_to_dict(node), "token": raw_token}


async def _node_lifecycle_action(admin: AdminSession, node_id: int, ip_address: str | None, fn: Any) -> dict[str, str]:
    try:
        await fn(app.state.pool, admin_id=admin.admin_id, node_id=node_id, ip_address=ip_address)
    except NodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/nodes/{node_id}/approve")
async def approve_node(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.approve_node_admin)


@app.post("/nodes/{node_id}/disable")
async def disable_node(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.disable_node_admin)


@app.post("/nodes/{node_id}/resume")
async def resume_node(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.resume_node_admin)


@app.post("/nodes/{node_id}/drain")
async def drain_node(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.drain_node_admin)


@app.post("/nodes/{node_id}/maintenance")
async def set_node_maintenance(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.set_maintenance_admin)


@app.post("/nodes/{node_id}/revoke")
async def revoke_node(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    return await _node_lifecycle_action(admin, node_id, _client_ip(request), admin_queries.revoke_node_admin)


@app.post("/nodes/{node_id}/rotate-token")
async def rotate_node_token(
    request: Request, admin: Annotated[AdminSession, Depends(require("sms:nodes:manage"))], node_id: int
) -> dict[str, str]:
    try:
        raw_token = await admin_queries.rotate_node_token_admin(
            app.state.pool, admin_id=admin.admin_id, node_id=node_id, ip_address=_client_ip(request)
        )
    except NodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"token": raw_token}


# --- generic delivery-node protocol (node-authenticated, not admin) -------


class HeartbeatRequest(BaseModel):
    app_version: str | None = None
    capabilities: dict[str, Any] = {}
    max_concurrent_jobs: int | None = None
    protocol_version: int | None = None


@app.post("/v1/nodes/heartbeat")
async def node_heartbeat(node: CurrentNode, body: HeartbeatRequest) -> dict[str, Any]:
    async with app.state.pool.acquire() as conn:
        await nodes_module.record_heartbeat(
            conn, node_id=node.id, app_version=body.app_version, capabilities=body.capabilities,
            max_concurrent_jobs=body.max_concurrent_jobs, protocol_version=body.protocol_version,
        )
        score = await nodes_module.compute_and_store_health_score(conn, node_id=node.id)
    return {"status": node.status, "health_score": score}


@app.post("/v1/nodes/fetch-job")
async def node_fetch_job(node: CurrentNode) -> dict[str, Any] | None:
    if node.status != "active":
        # Honest, not a bare 401/403 -- a pending node is waiting on
        # admin approval, a disabled/draining/maintenance one is
        # intentionally not being given new work, distinct real reasons a
        # device operator should be able to tell apart.
        return {"job": None, "reason": f"node status is {node.status!r}, not accepting work"}
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            message = await messages_module.claim_next_message(conn, tenant_id=node.tenant_id, node=node)
    if message is None:
        return {"job": None, "reason": "no eligible queued messages, or already at max_concurrent_jobs"}
    return {
        "job": {
            "message_id": message.id, "phone_e164": message.phone_e164, "body": message.body,
            "priority": message.priority, "attempt_number": message.attempt_count,
        },
        "reason": None,
    }


@app.post("/v1/nodes/jobs/{message_id}/start")
async def node_start_job(node: CurrentNode, message_id: int) -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        try:
            message = await messages_module.start_message(conn, message_id=message_id, node_id=node.id)
        except MessageNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotOwnedByNode as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": message.status}


class ReportResultRequest(BaseModel):
    outcome: Literal["delivered", "failed", "unknown"]
    error_class: Literal["temporary", "permanent", "network", "timeout", "node_failure", "unknown"] | None = None
    raw_provider_response: str | None = None


@app.post("/v1/nodes/jobs/{message_id}/result")
async def node_report_result(node: CurrentNode, message_id: int, body: ReportResultRequest) -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            try:
                message = await messages_module.report_result(
                    conn, message_id=message_id, node_id=node.id, outcome=body.outcome,
                    error_class=body.error_class, raw_provider_response=body.raw_provider_response,
                )
            except MessageNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except NotOwnedByNode as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": message.status}


if SMS_WEB_DIR.exists():
    app.mount("/console", StaticFiles(directory=str(SMS_WEB_DIR), html=True), name="console")
