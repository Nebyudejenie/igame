"""Telebirr evidence redemption (CTO directive sections 91/100/101/104) --
the money-critical core of the whole feature. A player proves only that
they know a real transaction reference; the amount credited always comes
from the SMS evidence already on file (telebirr_ingest.py), never from
anything the player types.

The entire lock-verify-credit-mark-redeemed sequence is one Postgres
transaction (section 100) with no external network call inside it (section
101) -- notify_user()/publish_balance_update() only run after commit.
Reuses the exact ledger.post() idempotent-transaction shape every other
deposit rail in this codebase already uses (Chapa webhook, Chapa poll,
manual admin-approve), so this is a fourth caller of an already-proven
pattern, not a new one -- and reuses deposits._check_deposit_eligibility
verbatim, so a self-excluded/banned/cooling-off player or one who would
exceed today's deposit cap is blocked here exactly the same way they would
be on every other rail.

No admin_audit_log entry is written here on purpose: that table's admin_id
column is NOT NULL / FK'd to admin_users (verified against the live
schema) -- it exists specifically for admin-triggered actions, the same
reason services/payments/deposits.py's own _apply_confirmed_status() (the
automatic Chapa-webhook credit, also player/system-triggered, never
admin-triggered) never calls audit.record() either. The audit trail for a
redemption is the payment_evidence row itself (redeemed_by_user_id,
redeemed_at, payment_id), the payments row, and the ledger_transactions
row ledger.post() already writes -- together a complete, reconstructible
record of who redeemed what, when, for how much.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import ledger, metrics, rate_limit, tracing
from packages.core.notifications import notify_user
from packages.core.referrals import maybe_grant_referral_bonus, maybe_grant_welcome_bonus
from services.payments.deposits import (
    DailyDepositCapExceeded,
    DepositorBanned,
    DepositorCoolingOff,
    DepositorSelfExcluded,
    UnknownDepositor,
    _check_deposit_eligibility,
)
from services.payments.telebirr_parser import normalize_reference

logger = structlog.get_logger()
_tracer = tracing.get_tracer(__name__)

RedemptionCode = Literal[
    "PAYMENT_REDEEMED",  # success -- just now, or idempotently (already redeemed by this same user)
    "INVALID_REFERENCE",
    "PAYMENT_NOT_FOUND",
    "PAYMENT_ALREADY_REDEEMED",  # redeemed by a DIFFERENT user -- ownership never transfers
    "PAYMENT_BLOCKED",
    "PAYMENT_DISPUTED",
    "PAYMENT_EXPIRED",
    "RATE_LIMITED",
    "DAILY_CAP_EXCEEDED",
    "SELF_EXCLUDED",
    "ACCOUNT_BANNED",
    "COOLING_OFF_ACTIVE",
    "UNKNOWN_USER",
]

# Named the same way services/payments/telebirr_ingest.py's own
# STATUS_*/SOURCE_* constants are (NAME: LiteralAlias = "value") -- so a
# caller outside this module (services/bot/handlers.py's own chat-paste
# redemption path) can branch on outcome.code by imported name rather
# than repeating these strings as bare literals of its own.
CODE_PAYMENT_REDEEMED: RedemptionCode = "PAYMENT_REDEEMED"
CODE_INVALID_REFERENCE: RedemptionCode = "INVALID_REFERENCE"
CODE_PAYMENT_NOT_FOUND: RedemptionCode = "PAYMENT_NOT_FOUND"
CODE_PAYMENT_ALREADY_REDEEMED: RedemptionCode = "PAYMENT_ALREADY_REDEEMED"
CODE_PAYMENT_BLOCKED: RedemptionCode = "PAYMENT_BLOCKED"
CODE_PAYMENT_DISPUTED: RedemptionCode = "PAYMENT_DISPUTED"
CODE_PAYMENT_EXPIRED: RedemptionCode = "PAYMENT_EXPIRED"
CODE_RATE_LIMITED: RedemptionCode = "RATE_LIMITED"
CODE_DAILY_CAP_EXCEEDED: RedemptionCode = "DAILY_CAP_EXCEEDED"
CODE_SELF_EXCLUDED: RedemptionCode = "SELF_EXCLUDED"
CODE_ACCOUNT_BANNED: RedemptionCode = "ACCOUNT_BANNED"
CODE_COOLING_OFF_ACTIVE: RedemptionCode = "COOLING_OFF_ACTIVE"
CODE_UNKNOWN_USER: RedemptionCode = "UNKNOWN_USER"

_STATUS_TO_CODE: dict[str, RedemptionCode] = {
    "blocked": CODE_PAYMENT_BLOCKED,
    "disputed": CODE_PAYMENT_DISPUTED,
    "expired": CODE_PAYMENT_EXPIRED,
}


@dataclass(frozen=True)
class RedemptionOutcome:
    code: RedemptionCode
    amount: Decimal | None
    payment_id: int | None
    our_ref: str | None


async def _eligibility_rejection_code(
    conn: ledger.AsyncpgConnection, *, user_id: int, amount: Decimal, daily_cap: Decimal
) -> RedemptionCode | None:
    """None means eligible. Reuses deposits._check_deposit_eligibility
    verbatim (the same self-exclusion/ban/cooloff/daily-cap gate every
    other deposit rail already enforces) and just translates its
    exception hierarchy into this module's own machine-readable codes
    (section 105) -- deliberately no minimum-deposit check here, unlike
    every other rail: the amount is fixed by a real SMS-evidenced payment
    that already happened, not something a player is choosing to type in,
    so "below the minimum deposit" has no meaningful application to it.
    """
    try:
        await _check_deposit_eligibility(conn, user_id=user_id, amount=amount, daily_cap=daily_cap)
    except UnknownDepositor:
        return "UNKNOWN_USER"
    except DepositorSelfExcluded:
        return "SELF_EXCLUDED"
    except DepositorBanned:
        return "ACCOUNT_BANNED"
    except DepositorCoolingOff:
        return "COOLING_OFF_ACTIVE"
    except DailyDepositCapExceeded:
        return "DAILY_CAP_EXCEEDED"
    return None


async def redeem_evidence(
    pool: asyncpg.Pool, redis: Redis, *, user_id: int, reference: str, daily_cap: Decimal
) -> RedemptionOutcome:
    reference = reference.strip()
    if not reference:
        return RedemptionOutcome(code="INVALID_REFERENCE", amount=None, payment_id=None, our_ref=None)
    normalized = normalize_reference(reference)

    with _tracer.start_as_current_span(
        "telebirr.redeem_evidence", attributes={"user_id": user_id}
    ) as span:
        span.set_attribute("telebirr.reference", normalized)

        if not await rate_limit.allow(redis, "telebirr_redeem", str(user_id), **rate_limit.TELEBIRR_REDEEM):
            span.set_attribute("telebirr.outcome", "RATE_LIMITED")
            return RedemptionOutcome(code="RATE_LIMITED", amount=None, payment_id=None, our_ref=None)

        async with pool.acquire() as conn:
            async with conn.transaction():
                evidence = await conn.fetchrow(
                    "SELECT id, status, amount, redeemed_by_user_id, payment_id "
                    "FROM payment_evidence WHERE external_reference = $1 FOR UPDATE",
                    normalized,
                )
                if evidence is None:
                    span.set_attribute("telebirr.outcome", "PAYMENT_NOT_FOUND")
                    return RedemptionOutcome(
                        code="PAYMENT_NOT_FOUND", amount=None, payment_id=None, our_ref=None
                    )

                status = evidence["status"]

                if status in _STATUS_TO_CODE:
                    span.set_attribute("telebirr.outcome", _STATUS_TO_CODE[status])
                    return RedemptionOutcome(
                        code=_STATUS_TO_CODE[status], amount=None, payment_id=None, our_ref=None
                    )

                if status == "redeemed":
                    if evidence["redeemed_by_user_id"] == user_id:
                        # Section 104: the player's own earlier request
                        # already succeeded (e.g. their connection dropped
                        # before seeing the response) -- return the exact
                        # same successful outcome again, never a fresh
                        # credit.
                        payment = await conn.fetchrow(
                            "SELECT our_ref, amount FROM payments WHERE id = $1", evidence["payment_id"]
                        )
                        assert payment is not None
                        span.set_attribute("telebirr.outcome", "PAYMENT_REDEEMED_IDEMPOTENT")
                        return RedemptionOutcome(
                            code="PAYMENT_REDEEMED",
                            amount=payment["amount"],
                            payment_id=evidence["payment_id"],
                            our_ref=payment["our_ref"],
                        )
                    # Section 91: ownership never silently transfers -- a
                    # second, different claimant is simply rejected.
                    span.set_attribute("telebirr.outcome", "PAYMENT_ALREADY_REDEEMED")
                    return RedemptionOutcome(
                        code="PAYMENT_ALREADY_REDEEMED", amount=None, payment_id=None, our_ref=None
                    )

                assert status == "available"

                rejection_code = await _eligibility_rejection_code(
                    conn, user_id=user_id, amount=evidence["amount"], daily_cap=daily_cap
                )
                if rejection_code is not None:
                    span.set_attribute("telebirr.outcome", rejection_code)
                    return RedemptionOutcome(
                        code=rejection_code, amount=None, payment_id=None, our_ref=None
                    )

                ref_row = await conn.fetchrow(
                    "SELECT 'DEP-' || extract(year from now())::text || '-' || "
                    "lpad(nextval('payment_ref_seq')::text, 6, '0') AS our_ref"
                )
                assert ref_row is not None
                our_ref: str = ref_row["our_ref"]
                span.set_attribute("telebirr.our_ref", our_ref)

                amount = evidence["amount"]
                payment_row = await conn.fetchrow(
                    """
                    INSERT INTO payments (user_id, direction, provider, our_ref, amount, status)
                    VALUES ($1, 'in', 'telebirr_sms', $2, $3, 'succeeded')
                    RETURNING id
                    """,
                    user_id,
                    our_ref,
                    amount,
                )
                assert payment_row is not None
                payment_id: int = payment_row["id"]

                provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
                cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
                txn = await ledger.post(
                    conn,
                    "deposit",
                    [ledger.Entry(provider_account.id, -amount), ledger.Entry(cash_account.id, amount)],
                    idempotency_key=our_ref,
                    payment_id=payment_id,
                )
                await conn.execute(
                    "UPDATE payments SET ledger_txn_id = $2, updated_at = now() WHERE id = $1",
                    payment_id,
                    txn.id,
                )
                await conn.execute(
                    "UPDATE payment_evidence SET status = 'redeemed', redeemed_by_user_id = $2, "
                    "redeemed_at = now(), payment_id = $3, updated_at = now() WHERE id = $1",
                    evidence["id"],
                    user_id,
                    payment_id,
                )
                # Same transaction as the credit above -- see
                # packages/core/referrals.py's own docstring: silent
                # no-ops, never raises.
                await maybe_grant_referral_bonus(conn, user_id=user_id, deposit_amount=amount)
                await maybe_grant_welcome_bonus(conn, user_id=user_id, deposit_amount=amount)

        # Only reachable once the transaction above has actually committed
        # -- same placement deposits.py's own _apply_confirmed_status()
        # and admin/queries.py's approve_manual_deposit_admin() already
        # use, for the same reason (ledger.post() can't safely record this
        # itself when called nested).
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        metrics.telebirr_redemption_outcomes_total.labels(outcome="credited").inc()
        snapshot = await ledger.publish_balance_update(pool, redis, user_id)
        await notify_user(
            pool, redis, user_id=user_id, key="notify.deposit_confirmed",
            amount=str(amount), balance=snapshot["cash"],
        )
        span.set_attribute("telebirr.outcome", "PAYMENT_REDEEMED")
        logger.info(
            "telebirr_redeemed", user_id=user_id, evidence_id=evidence["id"],
            payment_id=payment_id, our_ref=our_ref, amount=str(amount),
        )
        return RedemptionOutcome(code="PAYMENT_REDEEMED", amount=amount, payment_id=payment_id, our_ref=our_ref)
