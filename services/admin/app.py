"""Admin console API. Separate authentication from players entirely (spec
section 33): username + password + TOTP, session tokens in Redis, RBAC
enforced on every mutating route, every mutation audit-logged. No route in
this file ever writes a balance directly -- adjust_balance goes through
the ledger like any other money movement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

ADMIN_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "admin"

from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.redis_conn import get_redis
from services.admin import (
    announcement_queries,
    auth,
    bonus_queries,
    bot_content_queries,
    command_registry_queries,
    keno_queries,
    notification_queries,
    queries,
    search_queries,
    simulated_players_queries,
    system_health,
    telegram_diagnostics,
)
from services.admin.auth import AdminSession
from services.admin.rbac import has_permission


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=2, max_size=20)
    app.state.redis = get_redis()
    app.state.ip_allowlist = [
        ip.strip() for ip in settings.admin_ip_allowlist.split(",") if ip.strip()
    ]
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Zemen Game Admin API")


def _client_ip(request: Request) -> str:
    # Once this service sits behind Cloudflare Tunnel + Traefik,
    # request.client.host only ever sees Traefik's own container IP --
    # ADMIN_IP_ALLOWLIST would silently stop meaning anything. CF-
    # Connecting-IP is the trustworthy signal instead: Cloudflare's edge
    # sets it to the real visitor IP and overwrites any value a client
    # tries to send itself (unlike X-Forwarded-For, which a client could
    # forge freely) -- this container is never reachable except through
    # Cloudflare once actually exposed publicly, so there is no other
    # path an attacker could use to inject a fake header directly. Falls
    # back to the raw connection IP so today's access pattern (an SSH
    # tunnel straight to this container's own port, no proxy in front at
    # all) is completely unaffected.
    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    if cf_connecting_ip:
        return cf_connecting_ip
    return request.client.host if request.client else "unknown"


def _check_ip_allowlist(request: Request) -> None:
    allowlist = app.state.ip_allowlist
    if not allowlist:
        return
    if _client_ip(request) not in allowlist:
        raise HTTPException(status_code=403, detail="source IP not permitted")


# Routes with no Depends() of their own to run the allowlist check
# through, so it's enforced here as middleware instead. A code-review
# pass that actually enumerated app.routes (not just the routes anyone
# had written by hand) found /docs, /redoc, and /openapi.json here too --
# FastAPI adds these automatically, so they'd never show up in a search
# for a hand-written route missing the check the way /metrics and
# /auth/login did in earlier passes, but they leak this real-money
# panel's entire API surface (every route, every request/response
# field) to anyone on the network regardless of the allowlist,
# confirmed live: with a real allowlist configured, /dashboard and
# /metrics correctly 403 an excluded IP while /docs/openapi.json/redoc
# still returned 200.
_UNAUTHENTICATED_DOC_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


@app.middleware("http")
async def _unauthenticated_route_ip_allowlist(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # The frontend mounted below at /console is plain StaticFiles, which
    # (unlike every API route) can't run a Depends(current_admin) IP
    # check either -- and every other unauthenticated route in this file
    # (/auth/login, /metrics) already learned the hard way, via a real
    # code review finding, that "no bearer token yet" is not license to
    # skip the allowlist spec section 9.2 asks the whole admin panel to
    # have.
    path = request.url.path
    if (path.startswith("/console") or path in _UNAUTHENTICATED_DOC_PATHS) and app.state.ip_allowlist:
        if _client_ip(request) not in app.state.ip_allowlist:
            return Response(status_code=403, content="source IP not permitted")
    return await call_next(request)


def _require_reason(reason: str) -> None:
    """Every financially-consequential admin action needs an accountable
    reason on the record (spec: "no hidden god mode") -- a code review
    pass caught this exact check copy-pasted across four routes, and,
    more importantly, silently *missing* from a fifth (approve_
    withdrawal): the one route that had a required `reason: str` field
    on its own request model but never actually enforced it, letting an
    empty string through to become a `None` reason on real-money-release
    audit log entry. Every sibling route (reject, void, adjust, set
    -status) already required one; nothing about approving a withdrawal
    is less consequential than rejecting one.
    """
    if not reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")


async def current_admin(
    request: Request, authorization: str = Header(default="")
) -> AdminSession:
    _check_ip_allowlist(request)
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


# --- auth ------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str


@app.post("/auth/login")
async def login(request: Request, body: LoginRequest) -> dict[str, str]:
    # A code review pass caught that every other route enforces the IP
    # allowlist either via current_admin() (the session dependency almost
    # every route uses) or, for the one unauthenticated exception besides
    # this route (/metrics), by calling this directly -- but login() takes
    # no bearer token yet (that's the whole point: it's how one is
    # obtained), so it never went through either path. This is actually
    # the single most exposed route to check it on: an attacker outside
    # the allowlist could otherwise still throw password/TOTP guesses at
    # it even though every other admin route was already unreachable to
    # them.
    _check_ip_allowlist(request)
    try:
        token = await auth.login(
            app.state.pool,
            app.state.redis,
            username=body.username,
            password=body.password,
            totp_code=body.totp_code,
        )
    except auth.LoginRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth.LoginFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # The frontend needs the role to filter its own nav (an architecture
    # audit caught that every screen was shown regardless of role, with
    # a 403 only ever discovered after the click) -- reusing
    # resolve_session() rather than widening auth.login()'s own, widely
    # relied-on `-> str` return type across every test that calls it.
    session = await auth.resolve_session(app.state.pool, app.state.redis, token)
    assert session is not None  # was just created above; can't be missing or inactive
    return {"token": token, "role": session.role}


@app.post("/auth/logout")
async def logout(
    admin: Annotated[AdminSession, Depends(current_admin)],
    authorization: str = Header(default=""),
) -> dict[str, str]:
    # current_admin already validated this is a real "Bearer <token>"
    # header; re-slice it here to get the actual token to delete.
    token = authorization[len("Bearer ") :]
    await auth.logout(app.state.redis, token)
    return {"status": "ok"}


# --- dashboard ---------------------------------------------------------


@app.get("/dashboard")
async def dashboard(admin: Annotated[AdminSession, Depends(require("dashboard:view"))]) -> dict[str, Any]:
    return await queries.dashboard_summary(app.state.pool)


@app.get("/system-health")
async def system_health_check(
    admin: Annotated[AdminSession, Depends(require("dashboard:view"))],
) -> list[dict[str, str]]:
    """Same permission as the dashboard itself -- this is the top status
    bar the dashboard renders, not a separate, more sensitive capability.
    Every check here is a real, live probe (services/admin/system_health
    .py's own docstring lists exactly what is and isn't covered) -- never
    a cached or decorative value.
    """
    settings = get_settings()
    checks = await system_health.run_all_checks(
        app.state.pool, app.state.redis, bot_token=settings.telegram_bot_token
    )
    return [{"name": c.name, "status": c.status, "why": c.why} for c in checks]


# --- global search -------------------------------------------------------


@app.get("/search")
async def global_search(
    admin: Annotated[AdminSession, Depends(current_admin)], q: str
) -> dict[str, list[dict[str, Any]]]:
    """No specific permission dependency -- every authenticated admin can
    call this endpoint, but search_queries.global_search() only includes
    a category (users, payments, audit, ...) if admin.role already holds
    that category's own existing view permission, so what's returned is
    already exactly what that role could see by visiting each underlying
    screen directly. Never a broader result set than the role's real
    access.
    """
    return await search_queries.global_search(app.state.pool, query=q, role=admin.role)


# --- users -------------------------------------------------------------


@app.get("/users")
async def search_users(
    admin: Annotated[AdminSession, Depends(require("users:view"))], q: str
) -> list[dict[str, Any]]:
    return await queries.search_users(app.state.pool, q)


@app.get("/users/{user_id}")
async def get_user(
    admin: Annotated[AdminSession, Depends(require("users:view"))], user_id: int
) -> dict[str, Any]:
    detail = await queries.get_user_detail(app.state.pool, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="user not found")
    return detail


@app.get("/users/{user_id}/ledger")
async def get_user_ledger(
    admin: Annotated[AdminSession, Depends(require("users:view"))], user_id: int
) -> list[dict[str, Any]]:
    return await queries.get_user_ledger_history(app.state.pool, user_id)


class AdjustBalanceRequest(BaseModel):
    amount: str
    reason: str
    # One per "Apply" click, generated client-side (crypto.randomUUID() in
    # web/admin/js/screens/users.js) -- becomes the ledger idempotency key.
    # Required, not defaulted: a missing/blank value would silently reopen
    # the double-submission gap this field exists to close.
    request_id: str


@app.post("/users/{user_id}/adjust")
async def adjust_balance(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:adjust_balance"))],
    user_id: int,
    body: AdjustBalanceRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    if not body.request_id.strip():
        raise HTTPException(status_code=422, detail="request_id is required")
    try:
        amount = Decimal(body.amount)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="amount must be a decimal number") from exc

    try:
        txn_id = await queries.adjust_balance(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            amount=amount,
            reason=body.reason,
            ip_address=_client_ip(request),
            request_id=body.request_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ledger_transaction_id": txn_id}


class SetStatusRequest(BaseModel):
    status: str
    reason: str


@app.post("/users/{user_id}/status")
async def set_user_status(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:suspend"))],
    user_id: int,
    body: SetStatusRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        await queries.set_user_status(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            status=body.status,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except queries.InvalidStatusTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok"}


class SetKycLevelRequest(BaseModel):
    kyc_level: int
    reason: str


@app.post("/users/{user_id}/kyc")
async def set_kyc_level(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("users:verify_kyc"))],
    user_id: int,
    body: SetKycLevelRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        await queries.set_kyc_level(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            kyc_level=body.kyc_level,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except queries.InvalidKycLevel as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok"}


# --- rounds --------------------------------------------------------------


@app.get("/rounds")
async def list_rounds(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], room_id: int | None = None
) -> list[dict[str, Any]]:
    return await queries.list_rounds(app.state.pool, room_id)


@app.get("/rounds/{round_id}")
async def get_round(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], round_id: int
) -> dict[str, Any]:
    detail = await queries.get_round_detail(app.state.pool, round_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="round not found")
    return detail


@app.get("/rounds/{round_id}/fairness")
async def get_round_fairness(
    admin: Annotated[AdminSession, Depends(require("rounds:view"))], round_id: int
) -> dict[str, Any]:
    fairness = await queries.get_round_fairness(app.state.pool, round_id)
    if fairness is None:
        raise HTTPException(status_code=404, detail="round not found")
    return fairness


class VoidRoundRequest(BaseModel):
    reason: str


@app.post("/rounds/{round_id}/void")
async def void_round(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rounds:void"))],
    round_id: int,
    body: VoidRoundRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    refunded = await queries.void_round_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        round_id=round_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"refunded": refunded}


# --- withdrawals -----------------------------------------------------------


@app.get("/withdrawals")
async def list_pending_withdrawals(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_pending_withdrawals(app.state.pool)


class WithdrawalDecisionRequest(BaseModel):
    reason: str


@app.post("/withdrawals/{payment_id}/approve")
async def approve_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: WithdrawalDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    approved = await queries.approve_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"approved": approved}


@app.post("/withdrawals/{payment_id}/reject")
async def reject_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: WithdrawalDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    rejected = await queries.reject_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"rejected": rejected}


# --- manual deposits (P1: keep taking deposits when Chapa is down) -------


@app.get("/manual-deposits")
async def list_pending_manual_deposits(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_pending_manual_deposits(app.state.pool)


class ManualPaymentDecisionRequest(BaseModel):
    reason: str


@app.post("/manual-deposits/{payment_id}/approve")
async def approve_manual_deposit(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: ManualPaymentDecisionRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        outcome = await queries.approve_manual_deposit_admin(
            app.state.pool,
            app.state.redis,
            admin_id=admin.admin_id,
            payment_id=payment_id,
            reason=body.reason,
            ip_address=_client_ip(request),
            two_person_threshold=get_settings().auto_approve_withdraw_etb,
        )
    except queries.SameAdminCannotProvideSecondApproval as exc:
        raise HTTPException(status_code=409, detail="same_admin_cannot_double_approve") from exc
    return {"outcome": outcome}


@app.post("/manual-deposits/{payment_id}/reject")
async def reject_manual_deposit(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: ManualPaymentDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    rejected = await queries.reject_manual_deposit_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"rejected": rejected}


@app.get("/manual-deposits/{payment_id}/receipt")
async def get_manual_deposit_receipt(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
    payment_id: int,
) -> Response:
    # A thin proxy through the Bot API rather than any new object storage
    # -- this is a Telegram-native product, so the receipt photo already
    # lives on Telegram's own servers the moment a player sends it to the
    # bot; we only ever store its file_id (see services/payments/manual.py
    # 's attach_receipt_to_latest_pending_deposit).
    file_id = await queries.get_manual_deposit_receipt_file_id(app.state.pool, payment_id)
    if file_id is None:
        raise HTTPException(status_code=404, detail="no receipt attached to this request")
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="bot is not configured")
    async with httpx.AsyncClient() as client:
        file_resp = await client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
            params={"file_id": file_id},
        )
        if file_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="could not resolve receipt from Telegram")
        file_path = file_resp.json()["result"]["file_path"]
        photo_resp = await client.get(
            f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
        )
        if photo_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="could not download receipt from Telegram")
    return Response(content=photo_resp.content, media_type="image/jpeg")


# --- manual withdrawals ----------------------------------------------------


@app.get("/manual-withdrawals")
async def list_pending_manual_withdrawals(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_pending_manual_withdrawals(app.state.pool)


@app.get("/manual-withdrawals/awaiting-settlement")
async def list_manual_withdrawals_awaiting_settlement(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_manual_withdrawals_awaiting_settlement(app.state.pool)


@app.post("/manual-withdrawals/{payment_id}/approve")
async def approve_manual_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: ManualPaymentDecisionRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        outcome = await queries.approve_manual_withdrawal_admin(
            app.state.pool,
            app.state.redis,
            admin_id=admin.admin_id,
            payment_id=payment_id,
            reason=body.reason,
            ip_address=_client_ip(request),
            two_person_threshold=get_settings().auto_approve_withdraw_etb,
        )
    except queries.SameAdminCannotProvideSecondApproval as exc:
        raise HTTPException(status_code=409, detail="same_admin_cannot_double_approve") from exc
    return {"outcome": outcome}


class SettleManualWithdrawalRequest(BaseModel):
    external_reference: str
    reason: str


@app.post("/manual-withdrawals/{payment_id}/settle")
async def settle_manual_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: SettleManualWithdrawalRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    if not body.external_reference.strip():
        raise HTTPException(status_code=422, detail="external_reference is required")
    settled = await queries.settle_manual_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        external_reference=body.external_reference,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"settled": settled}


@app.post("/manual-withdrawals/{payment_id}/fail")
async def fail_manual_withdrawal(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    payment_id: int,
    body: ManualPaymentDecisionRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    failed = await queries.fail_manual_withdrawal_admin(
        app.state.pool,
        app.state.redis,
        admin_id=admin.admin_id,
        payment_id=payment_id,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"failed": failed}


# --- rooms ---------------------------------------------------------------


@app.get("/rooms")
async def list_rooms(admin: Annotated[AdminSession, Depends(require("rooms:view"))]) -> list[dict[str, Any]]:
    return await queries.list_rooms(app.state.pool)


class CreateRoomRequest(BaseModel):
    code: str
    stake: str
    house_cut_bps: int = 2000
    min_players: int = 1
    max_players: int = 100
    max_cards_per_player: int = 1
    lobby_seconds: int = 30
    call_interval_ms: int = 4000
    result_seconds: int = 10
    win_patterns: list[str] = ["row", "col", "diag"]
    min_winning_lines: int = 2


@app.post("/rooms")
async def create_room(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rooms:manage"))],
    body: CreateRoomRequest,
) -> dict[str, int]:
    try:
        stake = Decimal(body.stake)
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="stake must be a decimal number") from exc
    try:
        room_id = await queries.create_room_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            code=body.code,
            stake=stake,
            house_cut_bps=body.house_cut_bps,
            min_players=body.min_players,
            max_players=body.max_players,
            max_cards_per_player=body.max_cards_per_player,
            lobby_seconds=body.lobby_seconds,
            call_interval_ms=body.call_interval_ms,
            result_seconds=body.result_seconds,
            win_patterns=body.win_patterns,
            min_winning_lines=body.min_winning_lines,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"room_id": room_id}


class UpdateRoomRequest(BaseModel):
    changes: dict[str, Any]
    reason: str | None = None


@app.patch("/rooms/{room_id}")
async def update_room(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rooms:manage"))],
    room_id: int,
    body: UpdateRoomRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.update_room_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            room_id=room_id,
            changes=body.changes,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="room not found")
    return {"updated": updated}


@app.get("/rooms/{room_id}/stop-preview")
async def room_stop_preview(
    admin: Annotated[AdminSession, Depends(require("rooms:emergency_stop"))],
    room_id: int,
) -> dict[str, Any]:
    """What the admin console's confirmation dialog shows before an
    operator commits to POST /rooms/{room_id}/stop -- real current round/
    player/staked-amount data, not a guess (Section 6's own "must display
    real financial consequence" requirement).
    """
    preview = await queries.get_room_stop_preview_admin(app.state.pool, room_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="room not found")
    return preview


class StopRoomRequest(BaseModel):
    reason: str
    confirmation: str


@app.post("/rooms/{room_id}/stop")
async def stop_room(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("rooms:emergency_stop"))],
    room_id: int,
    body: StopRoomRequest,
) -> dict[str, Any]:
    """Emergency single-room stop: halts an active round (real refund via
    the existing ledger-backed primitive, never a raw balance edit),
    deactivates the room so it can't be immediately re-claimed, and
    audits the whole action atomically. See services/admin/queries.py::
    stop_room_admin() for the full safety reasoning and
    docs/EMERGENCY_ROOM_STOP.md for the complete design/test record.
    """
    _require_reason(body.reason)
    try:
        result = await queries.stop_room_admin(
            app.state.pool,
            app.state.redis,
            admin_id=admin.admin_id,
            room_id=room_id,
            reason=body.reason,
            confirmation=body.confirmation,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


# --- manual payment configuration (payments:configure -- superadmin only,
# see rbac.py's own comment on why this is narrower than payments:approve) --


@app.get("/manual-payment-destinations")
async def list_manual_payment_destinations(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    # view-only listing stays at the normal payments:view level (an
    # ops/support admin looking at a manual deposit in review needs to
    # see which destination it was paid into); only creating/editing a
    # destination needs payments:configure.
    return await queries.list_manual_payment_destinations(app.state.pool)


class CreateManualPaymentDestinationRequest(BaseModel):
    method_kind: str
    account_ref: str
    account_name: str
    instructions: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None


@app.post("/manual-payment-destinations")
async def create_manual_payment_destination(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    body: CreateManualPaymentDestinationRequest,
) -> dict[str, int]:
    destination_id = await queries.create_manual_payment_destination_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        method_kind=body.method_kind,
        account_ref=body.account_ref,
        account_name=body.account_name,
        instructions=body.instructions,
        ip_address=_client_ip(request),
        effective_from=body.effective_from,
        effective_until=body.effective_until,
    )
    return {"id": destination_id}


class UpdateManualPaymentDestinationRequest(BaseModel):
    changes: dict[str, Any]
    reason: str | None = None


@app.patch("/manual-payment-destinations/{destination_id}")
async def update_manual_payment_destination(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    destination_id: int,
    body: UpdateManualPaymentDestinationRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.update_manual_payment_destination_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            destination_id=destination_id,
            changes=body.changes,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="destination not found")
    return {"updated": updated}


@app.get("/payment-provider-availability")
async def get_payment_provider_availability(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.get_payment_provider_availability(app.state.pool)


class SetPaymentProviderAvailabilityRequest(BaseModel):
    enabled: bool
    reason: str


@app.patch("/payment-provider-availability/{provider}/{direction}")
async def set_payment_provider_availability(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    provider: str,
    direction: str,
    body: SetPaymentProviderAvailabilityRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    updated = await queries.set_payment_provider_availability_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        provider=provider,
        direction=direction,
        enabled=body.enabled,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="unknown provider/direction")
    return {"updated": updated}


# --- Telebirr SMS-evidence review -----------------------------------------


@app.get("/telebirr-evidence")
async def list_telebirr_evidence(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
    status: str | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    rows, next_cursor = await queries.list_payment_evidence(
        app.state.pool, status=status, limit=limit, cursor=cursor
    )
    return {"items": rows, "next_cursor": next_cursor}


@app.get("/telebirr-evidence/{evidence_id}/raw-sms")
async def get_telebirr_evidence_raw_sms(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:view_raw_evidence"))],
    evidence_id: int,
) -> dict[str, str]:
    raw_sms = await queries.get_payment_evidence_raw_sms(
        app.state.pool, admin_id=admin.admin_id, evidence_id=evidence_id, ip_address=_client_ip(request)
    )
    if raw_sms is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return {"raw_sms": raw_sms}


class ResolveTelebirrEvidenceRequest(BaseModel):
    to_status: str
    reason: str


@app.post("/telebirr-evidence/{evidence_id}/resolve")
async def resolve_telebirr_evidence(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:approve"))],
    evidence_id: int,
    body: ResolveTelebirrEvidenceRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    try:
        resolved = await queries.resolve_payment_evidence_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            evidence_id=evidence_id,
            to_status=body.to_status,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except queries.InvalidEvidenceTransition as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not resolved:
        raise HTTPException(status_code=404, detail="evidence not found")
    return {"resolved": resolved}


# --- Telegram payment-agent allowlist --------------------------------------


@app.get("/payment-agents")
async def list_payment_agents(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_payment_agents(app.state.pool)


class CreatePaymentAgentRequest(BaseModel):
    telegram_user_id: int
    display_name: str | None = None


@app.post("/payment-agents")
async def create_payment_agent(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    body: CreatePaymentAgentRequest,
) -> dict[str, int]:
    agent_id = await queries.create_payment_agent_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        telegram_user_id=body.telegram_user_id,
        display_name=body.display_name,
        ip_address=_client_ip(request),
    )
    return {"id": agent_id}


class SetPaymentAgentActiveRequest(BaseModel):
    is_active: bool


@app.patch("/payment-agents/{agent_id}")
async def set_payment_agent_active(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    agent_id: int,
    body: SetPaymentAgentActiveRequest,
) -> dict[str, bool]:
    updated = await queries.set_payment_agent_active_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        agent_id=agent_id,
        is_active=body.is_active,
        ip_address=_client_ip(request),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"updated": updated}


# --- Telebirr ingestion devices (automated Android/MacroDroid path) -------


@app.get("/ingestion-devices")
async def list_ingestion_devices(
    admin: Annotated[AdminSession, Depends(require("payments:view"))],
) -> list[dict[str, Any]]:
    return await queries.list_ingestion_devices(app.state.pool)


class CreateIngestionDeviceRequest(BaseModel):
    device_id: str
    device_name: str


@app.post("/ingestion-devices")
async def create_ingestion_device(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    body: CreateIngestionDeviceRequest,
) -> dict[str, Any]:
    if not body.device_id.strip() or not body.device_name.strip():
        raise HTTPException(status_code=422, detail="device_id_and_device_name_required")
    try:
        result = await queries.create_ingestion_device_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            device_id=body.device_id.strip(),
            device_name=body.device_name.strip(),
            ip_address=_client_ip(request),
        )
    except queries.DeviceIdAlreadyRegistered as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The token is shown exactly once, in this one response body -- this
    # route never logs it (queries.create_ingestion_device_admin's own
    # audit.record() call only ever writes device_id/device_name, never
    # the token or its hash), and it is never again retrievable afterward,
    # only rotated.
    return result


class SetIngestionDeviceStatusRequest(BaseModel):
    status: str


@app.patch("/ingestion-devices/{device_pk}")
async def set_ingestion_device_status(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    device_pk: int,
    body: SetIngestionDeviceStatusRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.set_ingestion_device_status_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            device_pk=device_pk,
            status=body.status,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="device not found")
    return {"updated": updated}


@app.post("/ingestion-devices/{device_pk}/rotate-token")
async def rotate_ingestion_device_token(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("payments:configure"))],
    device_pk: int,
) -> dict[str, Any]:
    result = await queries.rotate_ingestion_device_token_admin(
        app.state.pool, admin_id=admin.admin_id, device_pk=device_pk, ip_address=_client_ip(request)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="device not found")
    return result


# --- admin account management (superadmin-only -- see rbac.py's own
# comment on admin_users:manage) -----------------------------------------


@app.get("/admin-users")
async def list_admin_users(
    admin: Annotated[AdminSession, Depends(require("admin_users:manage"))],
) -> list[dict[str, Any]]:
    return await queries.list_admin_users(app.state.pool)


class CreateAdminUserRequest(BaseModel):
    username: str
    password: str
    role: str


@app.post("/admin-users")
async def create_admin_user_route(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("admin_users:manage"))],
    body: CreateAdminUserRequest,
) -> dict[str, Any]:
    try:
        return await queries.create_admin_user_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            username=body.username,
            password=body.password,
            role=body.role,
            ip_address=_client_ip(request),
        )
    except queries.AdminUsernameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SetAdminUserActiveRequest(BaseModel):
    is_active: bool


@app.patch("/admin-users/{target_admin_id}/active")
async def set_admin_user_active(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("admin_users:manage"))],
    target_admin_id: int,
    body: SetAdminUserActiveRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.set_admin_user_active_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            target_admin_id=target_admin_id,
            is_active=body.is_active,
            ip_address=_client_ip(request),
        )
    except queries.CannotModifyOwnAccount as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="admin user not found")
    return {"updated": updated}


class SetAdminUserRoleRequest(BaseModel):
    role: str


@app.patch("/admin-users/{target_admin_id}/role")
async def set_admin_user_role(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("admin_users:manage"))],
    target_admin_id: int,
    body: SetAdminUserRoleRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.set_admin_user_role_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            target_admin_id=target_admin_id,
            role=body.role,
            ip_address=_client_ip(request),
        )
    except queries.CannotModifyOwnAccount as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="admin user not found")
    return {"updated": updated}


class ResetAdminUserPasswordRequest(BaseModel):
    new_password: str


@app.post("/admin-users/{target_admin_id}/reset-password")
async def reset_admin_user_password(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("admin_users:manage"))],
    target_admin_id: int,
    body: ResetAdminUserPasswordRequest,
) -> dict[str, bool]:
    try:
        updated = await queries.reset_admin_user_password_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            target_admin_id=target_admin_id,
            new_password=body.new_password,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="admin user not found")
    return {"updated": updated}


# --- bot content (player-facing bot text overrides, no deploy needed) ----


@app.get("/bot-content")
async def list_bot_content(
    admin: Annotated[AdminSession, Depends(require("bot_content:manage"))],
) -> list[dict[str, Any]]:
    return await bot_content_queries.list_bot_content_admin(app.state.pool)


class SetBotContentOverrideRequest(BaseModel):
    value: str


@app.put("/bot-content/{key}/{language}")
async def set_bot_content_override(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bot_content:manage"))],
    key: str,
    language: str,
    body: SetBotContentOverrideRequest,
) -> dict[str, bool]:
    try:
        await bot_content_queries.set_bot_content_override_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            key=key,
            language=language,
            value=body.value,
            ip_address=_client_ip(request),
        )
    except (bot_content_queries.UnknownBotContentKey, bot_content_queries.InvalidBotContentPlaceholders) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"updated": True}


@app.delete("/bot-content/{key}/{language}")
async def clear_bot_content_override(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bot_content:manage"))],
    key: str,
    language: str,
) -> dict[str, bool]:
    cleared = await bot_content_queries.clear_bot_content_override_admin(
        app.state.pool, admin_id=admin.admin_id, key=key, language=language, ip_address=_client_ip(request)
    )
    if not cleared:
        raise HTTPException(status_code=404, detail="no override set for this key/language")
    return {"cleared": cleared}


# --- bonuses & referrals -----------------------------------------------


class CreateBonusRuleRequest(BaseModel):
    name: str
    trigger_type: str
    reward_type: str
    reward_amount: Decimal | None = None
    reward_percentage: Decimal | None = None
    reward_cap: Decimal | None = None
    min_qualifying_deposit: Decimal = Decimal("0")
    wagering_multiplier: Decimal = Decimal("3")
    expiry_days: int | None = None
    max_grants_per_user: int = 1


@app.post("/bonus-rules")
async def create_bonus_rule(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bonuses:manage_rules"))],
    body: CreateBonusRuleRequest,
) -> dict[str, int]:
    try:
        rule_id = await bonus_queries.create_bonus_rule_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            name=body.name,
            trigger_type=body.trigger_type,
            reward_type=body.reward_type,
            reward_amount=body.reward_amount,
            reward_percentage=body.reward_percentage,
            reward_cap=body.reward_cap,
            min_qualifying_deposit=body.min_qualifying_deposit,
            wagering_multiplier=body.wagering_multiplier,
            expiry_days=body.expiry_days,
            max_grants_per_user=body.max_grants_per_user,
            ip_address=_client_ip(request),
        )
    except bonus_queries.InvalidBonusRule as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": rule_id}


@app.get("/bonus-rules")
async def list_bonus_rules(
    admin: Annotated[AdminSession, Depends(require("bonuses:view"))],
) -> list[dict[str, Any]]:
    return await bonus_queries.list_bonus_rules_admin(app.state.pool)


class UpdateBonusRuleRequest(BaseModel):
    changes: dict[str, Any]


@app.patch("/bonus-rules/{rule_id}")
async def update_bonus_rule(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bonuses:manage_rules"))],
    rule_id: int,
    body: UpdateBonusRuleRequest,
) -> dict[str, bool]:
    try:
        updated = await bonus_queries.update_bonus_rule_admin(
            app.state.pool, admin_id=admin.admin_id, rule_id=rule_id, changes=body.changes,
            ip_address=_client_ip(request),
        )
    except bonus_queries.InvalidBonusRule as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="bonus rule not found")
    return {"updated": updated}


class GrantManualBonusRequest(BaseModel):
    user_id: int
    amount: Decimal
    wagering_multiplier: Decimal = Decimal("3")
    expiry_days: int | None = None
    reason: str


@app.post("/bonuses/grant")
async def grant_manual_bonus(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bonuses:grant"))],
    body: GrantManualBonusRequest,
) -> dict[str, int]:
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if body.amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be positive")
    bonus_id = await bonus_queries.grant_manual_bonus_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        user_id=body.user_id,
        amount=body.amount,
        wagering_multiplier=body.wagering_multiplier,
        expiry_days=body.expiry_days,
        reason=body.reason,
        ip_address=_client_ip(request),
    )
    return {"id": bonus_id}


@app.get("/bonuses")
async def list_bonuses(
    admin: Annotated[AdminSession, Depends(require("bonuses:view"))],
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return await bonus_queries.list_bonuses_admin(
        app.state.pool, user_id=user_id, status=status, limit=limit, offset=offset
    )


class RevokeBonusRequest(BaseModel):
    reason: str


@app.post("/bonuses/{bonus_id}/revoke")
async def revoke_bonus_route(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("bonuses:grant"))],
    bonus_id: int,
    body: RevokeBonusRequest,
) -> dict[str, bool]:
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    revoked = await bonus_queries.revoke_bonus_admin(
        app.state.pool, admin_id=admin.admin_id, bonus_id=bonus_id, reason=body.reason,
        ip_address=_client_ip(request),
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="active bonus not found")
    return {"revoked": revoked}


@app.get("/bonuses/referral-funnel")
async def referral_funnel(
    admin: Annotated[AdminSession, Depends(require("bonuses:view"))],
) -> dict[str, Any]:
    return await bonus_queries.referral_funnel_admin(app.state.pool)


# --- Keno (build spec Part 15) ---------------------------------------------


@app.get("/keno/dashboard")
async def keno_dashboard(
    admin: Annotated[AdminSession, Depends(require("keno:view"))],
) -> dict[str, Any]:
    return await keno_queries.dashboard_summary_admin(app.state.pool)


@app.get("/keno/configs")
async def list_keno_configs(
    admin: Annotated[AdminSession, Depends(require("keno:view"))],
) -> list[dict[str, Any]]:
    return await keno_queries.list_configs_admin(app.state.pool)


class CreateKenoConfigRequest(BaseModel):
    round_cycle_seconds: int
    betting_seconds: int
    draw_seconds: int
    result_seconds: int
    min_picks: int
    max_picks: int
    max_tickets_per_user_per_round: int
    per_user_round_capacity_share_bps: int
    jackpot_diversion_bps: int
    rtp_floor_bps: int = 7500
    rtp_ceiling_bps: int = 9700
    keno_enabled: bool
    reason: str


@app.post("/keno/configs")
async def create_keno_config(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("keno:configure"))],
    body: CreateKenoConfigRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await keno_queries.create_config_admin(
            app.state.pool, admin_id=admin.admin_id, ip_address=_client_ip(request), **body.model_dump()
        )
    except keno_queries.InvalidKenoConfig as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class KenoKillSwitchRequest(BaseModel):
    enabled: bool
    reason: str


@app.post("/keno/kill-switch")
async def keno_kill_switch(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("keno:configure"))],
    body: KenoKillSwitchRequest,
) -> dict[str, Any]:
    """Part 0's own required "one admin toggle" that instantly stops new
    rounds and blocks new tickets -- superadmin-only (keno:configure),
    the same highest-leverage-lever tier as rooms:emergency_stop."""
    _require_reason(body.reason)
    try:
        return await keno_queries.set_keno_enabled_admin(
            app.state.pool, admin_id=admin.admin_id, enabled=body.enabled, reason=body.reason,
            ip_address=_client_ip(request),
        )
    except keno_queries.InvalidKenoConfig as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/keno/paytables")
async def list_keno_paytables(
    admin: Annotated[AdminSession, Depends(require("keno:view"))],
    pick_count: int | None = None,
) -> list[dict[str, Any]]:
    return await keno_queries.list_paytables_admin(app.state.pool, pick_count=pick_count)


class PreviewKenoPaytableRequest(BaseModel):
    pick_count: int
    multipliers: dict[str, str]


@app.post("/keno/paytables/preview")
async def preview_keno_paytable(
    admin: Annotated[AdminSession, Depends(require("keno:manage"))],
    body: PreviewKenoPaytableRequest,
) -> dict[str, Any]:
    """Part 3.2's "the admin paytable editor must compute and display
    exact RTP ... live" -- pure computation, nothing persisted, safe for
    ops/superadmin (keno:manage) to call on every keystroke while
    designing a table before a superadmin actually activates it."""
    try:
        return keno_queries.preview_paytable_stats(body.pick_count, body.multipliers)
    except (keno_queries.InvalidKenoConfig, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class CreateKenoPaytableRequest(BaseModel):
    pick_count: int
    profile: str
    multipliers: dict[str, str]
    reason: str


@app.post("/keno/paytables")
async def create_keno_paytable(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("keno:configure"))],
    body: CreateKenoPaytableRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await keno_queries.create_paytable_admin(
            app.state.pool, admin_id=admin.admin_id, pick_count=body.pick_count, profile=body.profile,
            multipliers=body.multipliers, reason=body.reason, ip_address=_client_ip(request),
        )
    except keno_queries.InvalidKenoConfig as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/keno/tiers")
async def list_keno_tiers(
    admin: Annotated[AdminSession, Depends(require("keno:view"))],
) -> list[dict[str, Any]]:
    return await keno_queries.list_tiers_admin(app.state.pool)


class CreateKenoTierRequest(BaseModel):
    tier_number: int
    min_reserve: Decimal
    max_pick_count: int
    max_top_multiplier: Decimal
    stake_options: list[Decimal]
    max_win_per_ticket: Decimal
    max_round_exposure_pct: Decimal
    paytable_profile: str
    reason: str


@app.post("/keno/tiers")
async def create_keno_tier(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("keno:configure"))],
    body: CreateKenoTierRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await keno_queries.create_tier_admin(
            app.state.pool, admin_id=admin.admin_id, ip_address=_client_ip(request), **body.model_dump()
        )
    except keno_queries.InvalidKenoConfig as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SetKenoCurrentTierRequest(BaseModel):
    tier_id: int
    reason: str


@app.post("/keno/tiers/set-current")
async def set_keno_current_tier(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("keno:configure"))],
    body: SetKenoCurrentTierRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await keno_queries.set_current_tier_admin(
            app.state.pool, admin_id=admin.admin_id, tier_id=body.tier_id, reason=body.reason,
            ip_address=_client_ip(request),
        )
    except keno_queries.InvalidKenoConfig as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/bonuses/fraud-candidates")
async def referral_fraud_candidates(
    admin: Annotated[AdminSession, Depends(require("bonuses:view_fraud_signals"))],
) -> dict[str, Any]:
    return await bonus_queries.referral_fraud_candidates_admin(app.state.pool)


# --- reports ---------------------------------------------------------------


@app.get("/reports/ggr")
async def report_ggr(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], on_date: date
) -> dict[str, Any]:
    return await queries.daily_ggr(app.state.pool, on_date)


@app.get("/reports/ltv")
async def report_ltv(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], limit: int = 20
) -> list[dict[str, Any]]:
    return await queries.top_players_by_ltv(app.state.pool, limit)


@app.get("/reports/retention")
async def report_retention(
    admin: Annotated[AdminSession, Depends(require("reports:view"))], weeks: int = 8
) -> list[dict[str, Any]]:
    return await queries.retention_cohorts(app.state.pool, weeks)


# --- risk --------------------------------------------------------------


@app.get("/risk/shared-payout-accounts")
async def risk_shared_payout_accounts(
    admin: Annotated[AdminSession, Depends(require("risk:view"))],
) -> list[dict[str, Any]]:
    return await queries.shared_payout_account_clusters(app.state.pool)


@app.get("/risk/repeat-pairings")
async def risk_repeat_pairings(
    admin: Annotated[AdminSession, Depends(require("risk:view"))],
    min_shared_rounds: int = 3,
    since_days: int = 30,
) -> list[dict[str, Any]]:
    return await queries.repeat_room_pairings(
        app.state.pool, min_shared_rounds=min_shared_rounds, since_days=since_days
    )


# --- telegram ------------------------------------------------------------


@app.get("/telegram/webhook-health")
async def telegram_webhook_health(
    admin: Annotated[AdminSession, Depends(require("telegram:view_health"))],
) -> dict[str, Any]:
    """A real, live getWebhookInfo() call (services/admin/telegram_
    diagnostics.py), not a cached value -- Section 41/42's "TELEGRAM
    HEALTH" / one-click diagnostic ask. 503s with a clear reason rather
    than a raw exception when no bot token is configured at all (a
    perfectly valid state in a dev/staging environment), the same
    "explain what's actually true" discipline every other empty-config
    gate in this codebase already follows (e.g. payments/availability.py).
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="telegram_bot_token is not configured")
    try:
        health = await telegram_diagnostics.get_webhook_health(settings.telegram_bot_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"getWebhookInfo failed: {exc}") from None
    return {
        "url": health.url,
        "pending_update_count": health.pending_update_count,
        "last_error_date": health.last_error_date.isoformat() if health.last_error_date else None,
        "last_error_message": health.last_error_message,
        "last_synchronization_error_date": (
            health.last_synchronization_error_date.isoformat()
            if health.last_synchronization_error_date
            else None
        ),
        "ip_address": health.ip_address,
        "max_connections": health.max_connections,
        "status": health.status,
        "warning_threshold": telegram_diagnostics.PENDING_UPDATES_WARNING_THRESHOLD,
        "critical_threshold": telegram_diagnostics.PENDING_UPDATES_CRITICAL_THRESHOLD,
    }


@app.get("/telegram/commands")
async def list_telegram_commands(
    admin: Annotated[AdminSession, Depends(require("telegram:commands_view"))],
) -> list[dict[str, Any]]:
    settings = get_settings()
    return await command_registry_queries.list_commands_admin(
        app.state.pool, bot_metrics_url=settings.bot_metrics_url
    )


class UpdateCommandRequest(BaseModel):
    changes: dict[str, Any]
    reason: str | None = None


@app.patch("/telegram/commands/{handler_name}")
async def update_telegram_command(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("telegram:commands_manage"))],
    handler_name: str,
    body: UpdateCommandRequest,
) -> dict[str, Any]:
    try:
        return await command_registry_queries.update_command_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            handler_name=handler_name,
            changes=body.changes,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except command_registry_queries.UnknownBotCommand as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        command_registry_queries.CommandNotAdminManaged,
        command_registry_queries.InvalidCommandField,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/telegram/commands/{handler_name}/preview")
async def preview_telegram_command(
    admin: Annotated[AdminSession, Depends(require("telegram:commands_view"))],
    handler_name: str,
    language: str = "am",
) -> dict[str, Any]:
    try:
        return await command_registry_queries.preview_command_content_admin(
            app.state.pool, handler_name=handler_name, language=language
        )
    except (command_registry_queries.UnknownBotCommand, command_registry_queries.MissingContentKey) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class SendTestCommandRequest(BaseModel):
    target_telegram_id: int
    language: str = "am"


@app.post("/telegram/commands/{handler_name}/send-test")
async def send_test_telegram_command(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("telegram:commands_manage"))],
    handler_name: str,
    body: SendTestCommandRequest,
) -> dict[str, str]:
    try:
        sent_text = await command_registry_queries.send_test_command_admin(
            app.state.pool,
            app.state.redis,
            admin_id=admin.admin_id,
            handler_name=handler_name,
            target_telegram_id=body.target_telegram_id,
            language=body.language,
            ip_address=_client_ip(request),
        )
    except (command_registry_queries.UnknownBotCommand, command_registry_queries.MissingContentKey) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"sent_text": sent_text}


# --- audit log ---------------------------------------------------------


@app.get("/audit-log")
async def audit_log(
    admin: Annotated[AdminSession, Depends(require("audit:view"))],
    limit: int = 100,
    admin_id: int | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    # admin_id/action let a superadmin pull one specific admin's (or one
    # specific action type's) history without scrolling the combined feed
    # of every admin's actions -- exact-match only, both parameterized,
    # same as every other filtered list in this console.
    clauses = []
    params: list[Any] = []

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if admin_id is not None:
        clauses.append(f"l.admin_id = {_p(admin_id)}")
    if action is not None:
        clauses.append(f"l.action = {_p(action)}")
    where = " AND ".join(clauses) if clauses else "true"
    params.append(limit)
    rows = await app.state.pool.fetch(
        f"""
        SELECT l.id, l.admin_id, a.username AS admin_username, l.action, l.target_type, l.target_id,
               l.before, l.after, l.reason, l.ip_address, l.created_at
        FROM admin_audit_log l
        JOIN admin_users a ON a.id = l.admin_id
        WHERE {where}
        ORDER BY l.id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    # Every other route in this file goes through current_admin (session
    # token + this same IP check) or, at minimum, this IP check alone --
    # a real code review pass caught this endpoint bypassing both,
    # exposing house_revenue_total (live revenue in ETB), deposit_outcomes
    # _total, and payout_queue_depth to anyone on the network with no
    # session token and no allowlist check. A full session isn't required
    # here (a Prometheus scraper can't practically present one), but the
    # IP allowlist -- the one baseline control spec section 9.2 asks the
    # whole admin panel to have -- now applies here too.
    _check_ip_allowlist(request)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Notification Center -----------------------------------------------
#
# Templates/campaigns/audience/queue/history/analytics. Sending itself
# never happens synchronously in a request handler -- every route here
# only ever reads state or flips a campaign's own status; the real work
# (audience resolution, delivery, retries) is services/bot/
# campaign_worker.py, running in the bot process against the exact same
# rows these routes read and write.


class CreateTemplateRequest(BaseModel):
    name: str
    category: str
    title: str
    body: str


@app.post("/notifications/templates")
async def create_notification_template(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:templates_manage"))],
    body: CreateTemplateRequest,
) -> dict[str, int]:
    template_id = await notification_queries.create_template_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        name=body.name,
        category=body.category,
        title=body.title,
        body=body.body,
        ip_address=_client_ip(request),
    )
    return {"id": template_id}


@app.get("/notifications/templates")
async def list_notification_templates(
    admin: Annotated[AdminSession, Depends(require("notifications:view"))],
) -> list[dict[str, Any]]:
    return await notification_queries.list_templates_admin(app.state.pool)


class UpdateTemplateRequest(BaseModel):
    changes: dict[str, Any]


@app.patch("/notifications/templates/{template_id}")
async def update_notification_template(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:templates_manage"))],
    template_id: int,
    body: UpdateTemplateRequest,
) -> dict[str, bool]:
    try:
        updated = await notification_queries.update_template_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            template_id=template_id,
            changes=body.changes,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="template not found")
    return {"updated": updated}


class AudienceCountRequest(BaseModel):
    audience_filter: dict[str, Any] = {}
    exclude_user_ids: list[int] = []


@app.post("/notifications/audience/count")
async def count_notification_audience(
    admin: Annotated[AdminSession, Depends(require("notifications:view"))],
    body: AudienceCountRequest,
) -> dict[str, int]:
    try:
        count = await notification_queries.resolve_audience_count(
            app.state.pool, audience_filter=body.audience_filter, exclude_user_ids=body.exclude_user_ids
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"count": count}


class CreateCampaignRequest(BaseModel):
    internal_name: str
    title: str
    body: str
    audience_filter: dict[str, Any] = {}
    exclude_user_ids: list[int] = []
    template_id: int | None = None


@app.post("/notifications/campaigns")
async def create_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:create"))],
    body: CreateCampaignRequest,
) -> dict[str, int]:
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(status_code=422, detail="title and body are required")
    campaign_id = await notification_queries.create_campaign_admin(
        app.state.pool,
        admin_id=admin.admin_id,
        internal_name=body.internal_name,
        title=body.title,
        body=body.body,
        audience_filter=body.audience_filter,
        exclude_user_ids=body.exclude_user_ids,
        template_id=body.template_id,
        ip_address=_client_ip(request),
    )
    return {"id": campaign_id}


@app.get("/notifications/campaigns")
async def list_notification_campaigns(
    admin: Annotated[AdminSession, Depends(require("notifications:view"))],
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return await notification_queries.list_campaigns_admin(
        app.state.pool, status=status, search=search, limit=limit, offset=offset
    )


@app.get("/notifications/campaigns/{campaign_id}")
async def get_notification_campaign(
    admin: Annotated[AdminSession, Depends(require("notifications:view"))],
    campaign_id: int,
) -> dict[str, Any]:
    detail = await notification_queries.get_campaign_detail_admin(app.state.pool, campaign_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return detail


class UpdateCampaignRequest(BaseModel):
    changes: dict[str, Any]


@app.patch("/notifications/campaigns/{campaign_id}")
async def update_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:create"))],
    campaign_id: int,
    body: UpdateCampaignRequest,
) -> dict[str, bool]:
    try:
        updated = await notification_queries.update_campaign_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            campaign_id=campaign_id,
            changes=body.changes,
            ip_address=_client_ip(request),
        )
    except notification_queries.CampaignNotEditable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"updated": updated}


@app.delete("/notifications/campaigns/{campaign_id}")
async def delete_draft_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:create"))],
    campaign_id: int,
) -> dict[str, bool]:
    deleted = await notification_queries.delete_draft_campaign_admin(
        app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="draft campaign not found")
    return {"deleted": deleted}


@app.post("/notifications/campaigns/{campaign_id}/duplicate")
async def duplicate_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:create"))],
    campaign_id: int,
) -> dict[str, int]:
    new_id = await notification_queries.duplicate_campaign_admin(
        app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
    )
    if new_id is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"id": new_id}


@app.post("/notifications/campaigns/{campaign_id}/send")
async def send_notification_campaign_now(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:send"))],
    campaign_id: int,
) -> dict[str, bool]:
    try:
        sent = await notification_queries.send_campaign_now_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except notification_queries.InvalidCampaignTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not sent:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"queued": sent}


class ScheduleCampaignRequest(BaseModel):
    scheduled_at: datetime


@app.post("/notifications/campaigns/{campaign_id}/schedule")
async def schedule_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:schedule"))],
    campaign_id: int,
    body: ScheduleCampaignRequest,
) -> dict[str, bool]:
    try:
        scheduled = await notification_queries.schedule_campaign_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            campaign_id=campaign_id,
            scheduled_at=body.scheduled_at,
            ip_address=_client_ip(request),
        )
    except notification_queries.InvalidCampaignTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not scheduled:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"scheduled": scheduled}


@app.post("/notifications/campaigns/{campaign_id}/cancel")
async def cancel_notification_campaign(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("notifications:cancel"))],
    campaign_id: int,
) -> dict[str, bool]:
    try:
        cancelled = await notification_queries.cancel_campaign_admin(
            app.state.pool, admin_id=admin.admin_id, campaign_id=campaign_id, ip_address=_client_ip(request)
        )
    except notification_queries.InvalidCampaignTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not cancelled:
        raise HTTPException(status_code=404, detail="campaign not found")
    return {"cancelled": cancelled}


@app.get("/notifications/campaigns/{campaign_id}/deliveries")
async def list_notification_campaign_deliveries(
    admin: Annotated[AdminSession, Depends(require("notifications:view_delivery_details"))],
    campaign_id: int,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    return await notification_queries.list_deliveries_admin(
        app.state.pool, campaign_id=campaign_id, status=status, limit=limit, offset=offset
    )


@app.get("/notifications/overview")
async def notification_center_overview(
    admin: Annotated[AdminSession, Depends(require("notifications:view_analytics"))],
) -> dict[str, Any]:
    return await notification_queries.notification_overview_admin(app.state.pool)


# --- Simulated Players -------------------------------------------------


@app.get("/simulated-players")
async def list_simulated_players(
    admin: Annotated[AdminSession, Depends(require("simulated_players:view"))],
) -> list[dict[str, Any]]:
    return await simulated_players_queries.list_simulated_players(app.state.pool)


@app.get("/simulated-players/settings")
async def get_simulated_players_settings(
    admin: Annotated[AdminSession, Depends(require("simulated_players:view"))],
) -> dict[str, Any]:
    return await simulated_players_queries.get_settings_admin(app.state.pool)


@app.get("/simulated-players/daily-activity")
async def get_simulated_players_daily_activity(
    admin: Annotated[AdminSession, Depends(require("simulated_players:view"))], on_date: date
) -> dict[str, Any]:
    return await simulated_players_queries.simulated_players_daily_activity(app.state.pool, on_date)


class UpdateSimulatedPlayersSettingsRequest(BaseModel):
    enabled: bool
    max_concurrent_bots: int
    reason: str


@app.patch("/simulated-players/settings")
async def update_simulated_players_settings(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:stop_all"))],
    body: UpdateSimulatedPlayersSettingsRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await simulated_players_queries.update_settings_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            enabled=body.enabled,
            max_concurrent_bots=body.max_concurrent_bots,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class CreateSimulatedPlayerRequest(BaseModel):
    display_name: str
    strategy: str = "normal"


@app.post("/simulated-players")
async def create_simulated_player(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    body: CreateSimulatedPlayerRequest,
) -> dict[str, int]:
    try:
        user_id = await simulated_players_queries.create_simulated_player(
            app.state.pool,
            admin_id=admin.admin_id,
            display_name=body.display_name,
            strategy=body.strategy,
            ip_address=_client_ip(request),
        )
    except (ValueError, simulated_players_queries.SimulatedPlayerRosterFull) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user_id": user_id}


class SetSimulatedPlayerStrategyRequest(BaseModel):
    strategy: str
    join_probability_pct: int = 70
    max_cards_per_join: int = 1
    schedule_mode: str = "always_on"
    schedule_window_start_minute: int | None = None
    schedule_window_end_minute: int | None = None
    pinned_room_id: int | None = None
    reason: str


@app.patch("/simulated-players/{user_id}/strategy")
async def set_simulated_player_strategy(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    user_id: int,
    body: SetSimulatedPlayerStrategyRequest,
) -> dict[str, bool]:
    _require_reason(body.reason)
    try:
        await simulated_players_queries.set_strategy_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            strategy=body.strategy,
            join_probability_pct=body.join_probability_pct,
            max_cards_per_join=body.max_cards_per_join,
            schedule_mode=body.schedule_mode,
            schedule_window_start_minute=body.schedule_window_start_minute,
            schedule_window_end_minute=body.schedule_window_end_minute,
            pinned_room_id=body.pinned_room_id,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except simulated_players_queries.SimulatedPlayerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"updated": True}


class SimulatedPlayerActionRequest(BaseModel):
    reason: str | None = None


async def _simulated_player_action(
    request: Request,
    admin: AdminSession,
    user_id: int,
    body: SimulatedPlayerActionRequest,
    fn: Any,
) -> dict[str, bool]:
    try:
        await fn(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except simulated_players_queries.SimulatedPlayerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except simulated_players_queries.InvalidSimulatedPlayerTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"updated": True}


@app.post("/simulated-players/{user_id}/start")
async def start_simulated_player(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    user_id: int,
    body: SimulatedPlayerActionRequest,
) -> dict[str, bool]:
    return await _simulated_player_action(
        request, admin, user_id, body, simulated_players_queries.start_simulated_player_admin
    )


@app.post("/simulated-players/{user_id}/pause")
async def pause_simulated_player(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    user_id: int,
    body: SimulatedPlayerActionRequest,
) -> dict[str, bool]:
    return await _simulated_player_action(
        request, admin, user_id, body, simulated_players_queries.pause_simulated_player_admin
    )


@app.post("/simulated-players/{user_id}/stop")
async def stop_simulated_player(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    user_id: int,
    body: SimulatedPlayerActionRequest,
) -> dict[str, bool]:
    return await _simulated_player_action(
        request, admin, user_id, body, simulated_players_queries.stop_simulated_player_admin
    )


class ResetSimulatedPlayerRequest(BaseModel):
    reason: str


@app.post("/simulated-players/{user_id}/reset")
async def reset_simulated_player(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:manage"))],
    user_id: int,
    body: ResetSimulatedPlayerRequest,
) -> dict[str, str]:
    _require_reason(body.reason)
    try:
        return await simulated_players_queries.reset_simulated_player_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            user_id=user_id,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except simulated_players_queries.SimulatedPlayerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class StopAllSimulatedPlayersRequest(BaseModel):
    reason: str
    confirmation: str


@app.post("/simulated-players/stop-all")
async def stop_all_simulated_players(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("simulated_players:stop_all"))],
    body: StopAllSimulatedPlayersRequest,
) -> dict[str, int]:
    """Same reason+typed-confirmation contract as POST /rooms/{id}/stop --
    the single highest-leverage lever in this screen, superadmin-only per
    rbac.py's own comment on simulated_players:stop_all.
    """
    _require_reason(body.reason)
    if body.confirmation.strip().upper() != "STOP ALL":
        raise HTTPException(status_code=422, detail="confirmation must be exactly 'STOP ALL'")
    try:
        return await simulated_players_queries.stop_all_simulated_players_admin(
            app.state.pool, admin_id=admin.admin_id, reason=body.reason, ip_address=_client_ip(request)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- platform announcement (Mini App scrolling banner) --------------------


@app.get("/announcement")
async def get_announcement(
    admin: Annotated[AdminSession, Depends(require("announcement:view"))],
) -> dict[str, Any]:
    return await announcement_queries.get_announcement_admin(app.state.pool)


class UpdateAnnouncementRequest(BaseModel):
    text: str
    enabled: bool
    reason: str


@app.patch("/announcement")
async def update_announcement(
    request: Request,
    admin: Annotated[AdminSession, Depends(require("announcement:manage"))],
    body: UpdateAnnouncementRequest,
) -> dict[str, Any]:
    _require_reason(body.reason)
    try:
        return await announcement_queries.update_announcement_admin(
            app.state.pool,
            admin_id=admin.admin_id,
            text=body.text,
            enabled=body.enabled,
            reason=body.reason,
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# Mounted last, same reasoning as services/gateway/app.py's own miniapp
# mount: FastAPI matches routes in registration order, so every API route
# above must be registered first or this catch-all would shadow them.
# Protected by _console_frontend_ip_allowlist above, not by StaticFiles
# itself -- the frontend calls back into this same app's API routes for
# actual data, each of which still requires a real session token on top.
app.mount("/console", StaticFiles(directory=ADMIN_WEB_DIR, html=True), name="console")
