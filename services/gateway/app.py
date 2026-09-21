"""FastAPI gateway app: the WebSocket entrypoint players connect to.

Stateless in the sense that matters for horizontal scaling -- any replica
can serve any player, because `state_sync` is served from Postgres rather
than from in-memory state pinned to a specific replica (queries.py). What is
process-local is the FanoutHub's Redis subscription and the set of
currently-open connections, both scoped to this process's lifetime.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from packages.core import keno_autoplay, keno_queries, keno_tickets, rate_limit, telegram_auth
from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from packages.core.ledger import user_balance_snapshot
from packages.core.redis_conn import get_redis
from services.admin.queries import get_round_fairness
from services.gateway import queries
from services.gateway.connection import ConnectionHandler
from services.gateway.fanout import FanoutHub
from services.payments import availability, deposits, manual, withdrawals
from services.payments.chapa import ChapaProvider
from services.payments.manual_provider import ManualProvider
from services.payments.telebirr_parser import normalize_reference
from services.payments.telebirr_redemption import redeem_evidence

# Anchored to this file's location, not the process's cwd -- the gateway
# must serve the Mini App correctly regardless of the directory it's
# launched from.
MINIAPP_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "miniapp"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.pool = await create_pool(dsn=settings.database_url, min_size=5, max_size=50)
    app.state.redis = get_redis()
    app.state.bot_token = settings.telegram_bot_token
    app.state.chapa = ChapaProvider(settings.chapa_api_key) if settings.chapa_api_key else None
    app.state.hub = FanoutHub(app.state.redis)
    await app.state.hub.start()
    app.state.connections = set()
    try:
        yield
    finally:
        for handler in list(app.state.connections):
            await handler.close_for_shutdown()
        await app.state.hub.stop()
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    handler = ConnectionHandler(
        websocket,
        app.state.pool,
        app.state.redis,
        app.state.hub,
        app.state.bot_token,
        app.state.connections,
    )
    app.state.connections.add(handler)
    try:
        await handler.run()
    finally:
        app.state.connections.discard(handler)


async def _authenticated_user_id(authorization: str = Header(default="")) -> int:
    """Validates the Telegram convention `Authorization: tma <initData>`
    header -- the REST-side equivalent of the WebSocket handshake's auth
    frame, same validate_init_data() boundary, same rules (constant-time
    hash comparison, 24h replay window).
    """
    if not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="missing tma authorization header")
    raw_init_data = authorization[len("tma ") :]
    try:
        data = telegram_auth.validate_init_data(raw_init_data, app.state.bot_token)
    except telegram_auth.InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=f"invalid init data: {exc.reason}") from exc
    return await queries.get_or_create_user_by_telegram_id(
        app.state.pool, data.user.id, data.user.first_name or str(data.user.id)
    )


@app.get("/api/me")
async def api_me(authorization: str = Header(default="")) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    return await user_balance_snapshot(app.state.pool, user_id)


@app.get("/api/history")
async def api_history(authorization: str = Header(default="")) -> list[dict[str, object]]:
    user_id = await _authenticated_user_id(authorization)
    return await queries.user_history(app.state.pool, user_id)


@app.get("/api/rounds/{round_id}/fairness")
async def api_round_fairness(
    round_id: int, authorization: str = Header(default="")
) -> dict[str, Any]:
    """Spec section 14's definition of done: "a player can independently
    verify any round's draw from the published seed." Reuses
    services.admin.queries.get_round_fairness() directly -- the same
    server_seed/hash/draw_order/verified data an admin sees, since none of
    it is sensitive once a round is terminal (that's the entire point of a
    commit-reveal provably-fair scheme: it's meant to be publishable).
    Requires a valid session only to keep this off the open internet, not
    because the data itself is restricted to any particular player or
    round they were in.
    """
    await _authenticated_user_id(authorization)
    fairness = await get_round_fairness(app.state.pool, round_id)
    if fairness is None:
        raise HTTPException(status_code=404, detail="round not found")
    return fairness


@app.get("/api/manual-payment-destinations")
async def api_manual_payment_destinations(
    authorization: str = Header(default=""),
) -> list[dict[str, Any]]:
    await _authenticated_user_id(authorization)
    return await queries.list_active_manual_payment_destinations(app.state.pool)


@app.get("/api/payment-methods")
async def api_payment_methods(authorization: str = Header(default="")) -> dict[str, list[str]]:
    await _authenticated_user_id(authorization)
    return await availability.get_payment_availability(app.state.pool, get_settings())


@app.get("/api/announcement")
async def api_announcement(authorization: str = Header(default="")) -> dict[str, Any]:
    """The admin-configurable scrolling banner (services/admin/
    announcement_queries.py) -- fetched once at boot, same as the other
    /api/* config reads above. disabled/empty is a completely normal,
    common response; the Mini App just shows nothing in that case.
    """
    await _authenticated_user_id(authorization)
    row = await app.state.pool.fetchrow("SELECT text, enabled FROM platform_announcement WHERE id = 1")
    if row is None or not row["enabled"] or not row["text"]:
        return {"text": None}
    return {"text": row["text"]}


@app.get("/api/invite")
async def api_invite(authorization: str = Header(default="")) -> dict[str, Any]:
    """Same `?start=ref_{telegram_id}` deep link and referral count
    services/bot/handlers.py::cmd_invite() already sends over chat --
    surfaced in-app so a player can share it with one tap (native
    Telegram share sheet, see js/app.v6.js) without leaving the game.
    `link: None` only when the bot has no configured username, matching
    cmd_invite()'s own invite.no_username fallback.
    """
    user_id = await _authenticated_user_id(authorization)
    summary = await queries.invite_summary(app.state.pool, user_id)
    settings = get_settings()
    if not settings.telegram_bot_username:
        return {"link": None, "referral_count": summary["referral_count"]}
    link = f"https://t.me/{settings.telegram_bot_username}?start=ref_{summary['telegram_id']}"
    return {"link": link, "referral_count": summary["referral_count"]}


# Every DepositRejected/WithdrawalRejected subclass maps to a short error
# code the Mini App looks up its own translated message for -- the same
# "distinct exception type, not a string reason" pattern
# services/bot/handlers.py uses, just surfaced as JSON instead of a bot
# reply. Provider-side/unknown failures collapse to one generic code,
# matching the bot's own choice not to expose raw internal error text.
_DEPOSIT_ERROR_CODES: dict[type[Exception], str] = {
    deposits.DepositRateLimited: "rate_limited",
    deposits.BelowMinimumDeposit: "below_minimum",
    deposits.DailyDepositCapExceeded: "daily_cap_exceeded",
    deposits.DepositorSelfExcluded: "self_excluded",
    deposits.DepositorCoolingOff: "cooling_off",
    manual.UnknownManualDestination: "no_manual_destination",
}
_WITHDRAWAL_ERROR_CODES: dict[type[Exception], str] = {
    withdrawals.BelowMinimumWithdrawal: "below_minimum",
    withdrawals.InsufficientAvailableBalance: "insufficient_balance",
    withdrawals.KycLevelTooLow: "kyc_required",
    withdrawals.RecentReversibleDeposit: "recent_deposit",
    withdrawals.SimulatedPlayerCannotWithdraw: "simulated_player",
}


class DepositRequest(BaseModel):
    amount: str


@app.post("/api/deposit")
async def api_create_deposit(
    body: DepositRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()
    # A code-review pass caught that this only ever checked static
    # process-startup config (app.state.chapa/miniapp_url/
    # payments_public_base_url), never the admin's own live
    # payment_provider_availability toggle -- GET /api/payment-methods
    # and the bot's /deposit both already gate on
    # availability.get_payment_availability() (the documented single
    # source of truth), but this endpoint, the one that actually moves
    # money, didn't. An admin disabling Chapa deposits (a compromised
    # merchant account, a provider outage) had no effect here: the UI
    # hid the button, but a client that already had the page open (or
    # any direct POST) could still complete a "disabled" deposit.
    # get_payment_availability() already folds in every static
    # reachability check this used to do ad hoc (chapa_api_key,
    # miniapp_url, payments_public_base_url -- see its own
    # chapa_deposit_configured), so this single call replaces the old
    # check rather than adding a second one to drift from it.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if "chapa" not in methods["deposit"]:
        raise HTTPException(status_code=503, detail="deposits are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0:
        raise HTTPException(status_code=422, detail="invalid_amount")

    phone = await queries.user_phone(app.state.pool, user_id)
    if not phone:
        raise HTTPException(status_code=422, detail="phone_required")

    try:
        intent = await deposits.create_deposit_intent(
            app.state.pool,
            app.state.redis,
            app.state.chapa,
            user_id=user_id,
            amount=amount,
            phone_e164=phone,
            return_url=settings.miniapp_url,
            callback_url=f"{settings.payments_public_base_url}/webhooks/chapa",
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRejected as exc:
        code = _DEPOSIT_ERROR_CODES.get(type(exc), "provider_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"checkout_url": intent.checkout_url, "our_ref": intent.our_ref}


class ManualDepositRequest(BaseModel):
    amount: str
    manual_destination_id: int
    external_reference: str


@app.post("/api/deposit/manual")
async def api_create_manual_deposit(
    body: ManualDepositRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()

    # Same gap as api_create_deposit's: an admin's live availability
    # toggle was only ever enforced by the UI hiding the button, never by
    # this endpoint, the one that actually creates the review-queue row.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if "manual" not in methods["deposit"]:
        raise HTTPException(status_code=503, detail="deposits are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0:
        raise HTTPException(status_code=422, detail="invalid_amount")
    if not body.external_reference.strip():
        raise HTTPException(status_code=422, detail="external_reference_required")

    try:
        intent = await manual.create_manual_deposit_request(
            app.state.pool,
            app.state.redis,
            user_id=user_id,
            amount=amount,
            manual_destination_id=body.manual_destination_id,
            external_reference=body.external_reference,
            receipt_telegram_file_id=None,
            min_deposit=settings.min_deposit_etb,
            daily_cap=settings.daily_deposit_cap_etb,
        )
    except deposits.DepositRejected as exc:
        code = _DEPOSIT_ERROR_CODES.get(type(exc), "provider_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"status": "review", "our_ref": intent.our_ref}


class TelebirrRedeemRequest(BaseModel):
    reference: str


# Every non-success RedemptionOutcome.code (services/payments/
# telebirr_redemption.py) maps to a lowercase detail string, same
# uniform-422 convention _DEPOSIT_ERROR_CODES above already uses --
# PAYMENT_REDEEMED is the only code handled outside this dict (a 200,
# not an error).
_TELEBIRR_REDEEM_ERROR_CODES: dict[str, str] = {
    "INVALID_REFERENCE": "invalid_reference",
    "PAYMENT_NOT_FOUND": "payment_not_found",
    "PAYMENT_ALREADY_REDEEMED": "payment_already_redeemed",
    "PAYMENT_BLOCKED": "payment_blocked",
    "PAYMENT_DISPUTED": "payment_disputed",
    "PAYMENT_EXPIRED": "payment_expired",
    "RATE_LIMITED": "rate_limited",
    "DAILY_CAP_EXCEEDED": "daily_cap_exceeded",
    "SELF_EXCLUDED": "self_excluded",
    "ACCOUNT_BANNED": "account_banned",
    "COOLING_OFF_ACTIVE": "cooling_off",
    "UNKNOWN_USER": "unknown_user",
}


@app.post("/api/wallet/deposits/telebirr/redeem")
async def api_redeem_telebirr_reference(
    body: TelebirrRedeemRequest, authorization: str = Header(default="")
) -> dict[str, str | bool]:
    # No amount field on this request model at all: the player proves
    # only that they know the reference, the amount always comes from
    # the SMS evidence already on file (redeem_evidence() itself never
    # even takes an amount parameter -- there is no code path here that
    # could honor a client-supplied one even if the request model had a
    # field for it).
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()

    methods = await availability.get_payment_availability(app.state.pool, settings)
    if "telebirr_sms" not in methods["deposit"]:
        raise HTTPException(status_code=503, detail="deposits are not available yet")

    outcome = await redeem_evidence(
        app.state.pool,
        app.state.redis,
        user_id=user_id,
        reference=body.reference,
        daily_cap=settings.daily_deposit_cap_etb,
    )
    if outcome.code != "PAYMENT_REDEEMED":
        detail = _TELEBIRR_REDEEM_ERROR_CODES.get(outcome.code, "provider_error")
        raise HTTPException(status_code=422, detail=detail)

    assert outcome.amount is not None and outcome.our_ref is not None
    # reference/amount/currency here are purely informational -- the
    # normalized reference and the amount are both read back from the
    # already-committed payment_evidence/payments rows, never from
    # anything the client sent.
    return {
        "success": True,
        "reference": normalize_reference(body.reference),
        "amount": str(outcome.amount),
        "currency": "ETB",
        "our_ref": outcome.our_ref,
    }


class WithdrawRequest(BaseModel):
    amount: str
    account_ref: str
    holder_name: str
    provider: str = "chapa"


@app.post("/api/withdraw")
async def api_create_withdrawal(
    body: WithdrawRequest, authorization: str = Header(default="")
) -> dict[str, str]:
    user_id = await _authenticated_user_id(authorization)
    settings = get_settings()

    if body.provider not in ("chapa", "manual"):
        raise HTTPException(status_code=422, detail="unknown_provider")
    # Same gap as api_create_deposit's, for both rails: this used to only
    # check chapa's own static config, with no check at all for manual --
    # an admin's live availability toggle had no effect on either.
    methods = await availability.get_payment_availability(app.state.pool, settings)
    if body.provider not in methods["withdraw"]:
        raise HTTPException(status_code=503, detail="withdrawals are not available yet")

    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_amount") from None
    if amount <= 0 or not body.account_ref.strip() or not body.holder_name.strip():
        raise HTTPException(status_code=422, detail="invalid_amount")

    provider = ManualProvider() if body.provider == "manual" else app.state.chapa

    try:
        intent = await withdrawals.request_withdrawal(
            app.state.pool,
            app.state.redis,
            provider,
            user_id=user_id,
            amount=amount,
            method_kind=withdrawals.DEFAULT_METHOD_KIND,
            account_ref=body.account_ref,
            holder_name=body.holder_name,
            min_withdraw=settings.min_withdraw_etb,
            auto_approve_limit=settings.auto_approve_withdraw_etb,
            kyc_threshold=settings.kyc_required_above_etb,
            chargeback_window_minutes=settings.withdraw_chargeback_window_minutes,
            max_withdrawals_per_day=settings.max_withdrawals_per_day,
            force_review=(body.provider == "manual"),
        )
    except withdrawals.WithdrawalRejected as exc:
        code = _WITHDRAWAL_ERROR_CODES.get(type(exc), "unknown_error")
        raise HTTPException(status_code=422, detail=code) from exc

    return {"status": intent.status, "our_ref": intent.our_ref}


# --- Keno (build spec Part 10) --------------------------------------------
#
# Every TicketRejected subclass already carries its own .code (set as a
# class attribute on each subclass in packages/core/keno_tickets.py, with
# TicketRejected's own "ticket_rejected" as the base-class fallback) --
# reading it straight off the instance below, rather than a second,
# parallel type->code dict here, is what actually lets
# ResponsibleGamingBlock work: its code varies per instance (whichever of
# 'self_excluded' | 'banned' | 'cooling_off' | 'loss_limit_reached'
# responsible_gaming.check_stake_allowed() returned), which a dict keyed
# on exception *type* can't represent at all.


def _client_ip(request: Request) -> str:
    # Same precedence as services/admin/app.py's own _client_ip(): trusted
    # once behind Cloudflare (which overwrites any client-supplied value
    # for this header), falling back to the raw connection for a direct
    # dev/tunnel-less setup.
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


@app.get("/api/keno/state")
async def api_keno_state(authorization: str = Header(default="")) -> dict[str, Any]:
    await _authenticated_user_id(authorization)
    state = await keno_queries.game_center_state(app.state.pool)
    if state is None:
        raise HTTPException(status_code=503, detail="keno_not_configured")
    return state


class PlaceKenoTicketRequest(BaseModel):
    picks: list[int]
    stake: str
    idempotency_key: str


@app.post("/api/keno/tickets")
async def api_place_keno_ticket(
    body: PlaceKenoTicketRequest, request: Request, authorization: str = Header(default="")
) -> dict[str, Any]:
    user_id = await _authenticated_user_id(authorization)

    # Part 12: rate-limited per user and per IP -- two independent
    # buckets, either one tripping is enough to reject (the IP bucket
    # catches many accounts hammering from one source; the user bucket
    # catches one account hammering from anywhere).
    if not await rate_limit.allow(app.state.redis, "keno_ticket_user", str(user_id), **rate_limit.KENO_TICKET):
        raise HTTPException(status_code=429, detail="rate_limited")
    if not await rate_limit.allow(app.state.redis, "keno_ticket_ip", _client_ip(request), **rate_limit.KENO_TICKET):
        raise HTTPException(status_code=429, detail="rate_limited")

    try:
        stake = Decimal(body.stake)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_stake") from None
    if stake <= 0:
        raise HTTPException(status_code=422, detail="invalid_stake")
    if not body.idempotency_key.strip():
        raise HTTPException(status_code=422, detail="missing_idempotency_key")

    try:
        ticket = await keno_tickets.place_ticket(
            app.state.pool,
            app.state.redis,
            user_id=user_id,
            picks=body.picks,
            stake=stake,
            idempotency_key=body.idempotency_key,
        )
    except keno_tickets.TicketRejected as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc

    return {
        "id": ticket.id,
        "round_id": ticket.round_id,
        "picks": sorted(ticket.picks),
        "stake": str(ticket.stake),
        "status": ticket.status,
    }


class StartKenoAutoplayRequest(BaseModel):
    picks: list[int]
    stake: str
    rounds_total: int | None = None
    stop_on_win_amount: str | None = None
    stop_on_loss_amount: str | None = None


def _autoplay_session_to_dict(session: keno_autoplay.AutoplaySession) -> dict[str, Any]:
    return {
        "id": session.id,
        "picks": sorted(session.picks),
        "stake": str(session.stake),
        "rounds_total": session.rounds_total,
        "rounds_placed": session.rounds_placed,
        "stop_on_win_amount": str(session.stop_on_win_amount) if session.stop_on_win_amount is not None else None,
        "stop_on_loss_amount": str(session.stop_on_loss_amount) if session.stop_on_loss_amount is not None else None,
        "net_position": str(session.net_position),
        "status": session.status,
        "stop_reason": session.stop_reason,
        "last_round_id": session.last_round_id,
    }


@app.post("/api/keno/autoplay")
async def api_start_keno_autoplay(
    body: StartKenoAutoplayRequest, authorization: str = Header(default="")
) -> dict[str, Any]:
    """Starts a new autoplay/multi-race session (build spec-adjacent UX
    research, 2026-09-21) -- one mechanism covering both "autoplay with
    stop-on-win/loss" and "buy N future rounds," driven from
    keno_round_engine.py's own _open_betting(), never from this request
    itself (this call only ever creates the session row)."""
    user_id = await _authenticated_user_id(authorization)
    try:
        stake = Decimal(body.stake)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="invalid_stake") from None
    stop_on_win_amount = None
    if body.stop_on_win_amount is not None:
        try:
            stop_on_win_amount = Decimal(body.stop_on_win_amount)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="invalid_stop_on_win_amount") from None
    stop_on_loss_amount = None
    if body.stop_on_loss_amount is not None:
        try:
            stop_on_loss_amount = Decimal(body.stop_on_loss_amount)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="invalid_stop_on_loss_amount") from None

    try:
        session = await keno_autoplay.start_session(
            app.state.pool,
            user_id=user_id,
            picks=body.picks,
            stake=stake,
            rounds_total=body.rounds_total,
            stop_on_win_amount=stop_on_win_amount,
            stop_on_loss_amount=stop_on_loss_amount,
        )
    except keno_autoplay.AutoplayError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return _autoplay_session_to_dict(session)


@app.delete("/api/keno/autoplay")
async def api_stop_keno_autoplay(authorization: str = Header(default="")) -> dict[str, Any]:
    user_id = await _authenticated_user_id(authorization)
    session = await keno_autoplay.stop_session(app.state.pool, user_id=user_id)
    return {"stopped": session is not None}


@app.get("/api/keno/autoplay")
async def api_get_keno_autoplay(authorization: str = Header(default="")) -> dict[str, Any] | None:
    user_id = await _authenticated_user_id(authorization)
    session = await keno_autoplay.active_session(app.state.pool, user_id=user_id)
    return _autoplay_session_to_dict(session) if session is not None else None


@app.get("/api/keno/tickets")
async def api_my_keno_tickets(
    round_id: int | None = None,
    before_id: int | None = None,
    limit: int = 20,
    authorization: str = Header(default=""),
) -> list[dict[str, Any]]:
    user_id = await _authenticated_user_id(authorization)
    return await keno_queries.my_tickets(app.state.pool, user_id, round_id=round_id, before_id=before_id, limit=limit)


@app.get("/api/keno/stats")
async def api_my_keno_stats(authorization: str = Header(default="")) -> dict[str, Any]:
    user_id = await _authenticated_user_id(authorization)
    return await keno_queries.my_statistics(app.state.pool, user_id)


@app.get("/api/keno/rounds/recent")
async def api_keno_recent_results(limit: int = 20, authorization: str = Header(default="")) -> list[dict[str, Any]]:
    await _authenticated_user_id(authorization)
    return await keno_queries.recent_results(app.state.pool, limit=limit)


@app.get("/api/keno/rounds/hot-cold")
async def api_keno_hot_cold(lookback_rounds: int = 50, authorization: str = Header(default="")) -> dict[str, Any]:
    await _authenticated_user_id(authorization)
    return await keno_queries.hot_cold_numbers(app.state.pool, lookback_rounds=lookback_rounds)


@app.get("/api/keno/rounds/{round_id}")
async def api_keno_round_detail(round_id: int, authorization: str = Header(default="")) -> dict[str, Any]:
    """Round result + independent verification payload in one response --
    Part 4.1's "public verification endpoint." The Mini App's own
    verify-draw button (packages.core.keno.verify_draw, same function
    this reuses) can also recompute it client-side; this is the
    server-side confirmation."""
    await _authenticated_user_id(authorization)
    detail = await keno_queries.round_detail(app.state.pool, round_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="round_not_found")
    return detail


class _RevalidateStaticFiles(StaticFiles):
    """Plain StaticFiles sends no Cache-Control at all, which left Cloudflare
    free to invent its own default (observed live: max-age=14400 -- a
    returning player's browser could sit on a stale bundle for up to 4
    hours after a deploy, entirely invisible from the origin's own logs).
    no-cache forces every request to revalidate against the origin's
    ETag/Last-Modified (already sent by plain StaticFiles) before using a
    cached copy -- a cheap 304 when nothing changed, an immediate fresh
    fetch the moment a deploy changes the file. This is what the
    app.js -> app.v5.js -> app.v6.js rename-per-deploy was actually
    working around; see DECISIONS.md.

    index.html and every .js file are the exception: no-store, not
    no-cache, and the 304-eligible path below is skipped for them
    entirely -- returning the header alone isn't enough, since
    Starlette's own file_response() already decides 304-vs-200 by
    comparing the request's own If-None-Match/If-Modified-Since against
    a freshly computed ETag *before* this override ever gets to touch
    the response; a client whose cached body is already stale or empty
    but whose ETag still happens to match would keep getting a
    bodyless 304 no matter what header rides along on it. A real
    production report (a genuinely blank Mini App: index.html served
    304, then not one script or stylesheet request ever followed)
    pointed at exactly this -- some WebView's own cache entry for "/"
    was stale or empty while its ETag still matched, so revalidation
    "succeeded" against nothing to render. index.html was fixed first;
    .js followed once a *second* real report -- a player's own client
    still running old, already-fixed-on-the-server application logic
    (proven by a real WebSocket trace showing a stale response shape)
    -- showed the identical WebView-cache-corruption pattern applies to
    the actual code just as easily as to the shell that loads it. CSS/
    locale/font files stay on no-cache: lower-severity if briefly stale
    (a delayed style or translation, not broken logic), and the cheap
    304 revalidation is worth keeping for those. These are all small
    files on every load either way; there's no real cost to never
    letting the ones that matter be conditionally-cached at all, only
    downside in trusting a client cache this fragile with the code
    every single boot depends on.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: MutableMapping[str, Any],
        status_code: int = 200,
    ) -> Response:
        response: Response
        path_str = os.fspath(full_path)
        if os.path.basename(path_str) == "index.html" or path_str.endswith(".js"):
            response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
            response.headers["Cache-Control"] = "no-store"
            return response
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mounted last: FastAPI matches routes in registration order, and static
# files are served at "/" -- every /api/* and /ws route above must be
# registered first or the static mount would shadow them.
app.mount("/", _RevalidateStaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
