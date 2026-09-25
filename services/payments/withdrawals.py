"""Withdrawal domain logic (spec section 8.3, Prompt 8).

"The single most important step" (the spec's own words): the moment a
withdrawal is requested, funds move user_cash -> user_locked in the same
transaction that creates the payments row. There is no window in which a
player can both request a withdrawal and stake the same birr -- the ledger's
own row-locked, InsufficientFunds-raising post() is what actually
guarantees that, the same mechanism a stake and any other debit already
rely on. bonus funds are structurally excluded: a withdrawal only ever
debits user_cash, never user_bonus.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import ledger, metrics, tracing
from services.payments.provider import PaymentProvider

_tracer = tracing.get_tracer(__name__)

logger = structlog.get_logger()

PAYOUT_STREAM = "payouts"

# The one payout method every provider in the spec's table (Chapa,
# SantimPay, ArifPay) covers -- the bot's /withdraw command doesn't yet
# collect a method choice, so this is the default until it does.
DEFAULT_METHOD_KIND = "telebirr"

# WithdrawalIntent.status values -- named here so callers (services/bot
# /handlers.py in particular) compare against a constant instead of a bare
# string literal, which would otherwise trip that file's AST-based
# no-hardcoded-strings check the same way a raw domain string anywhere else
# in it would.
STATUS_APPROVED = "approved"
STATUS_REVIEW = "review"


class WithdrawalRejected(Exception):
    """Base class -- see services/payments/deposits.py's DepositRejected
    for why this is a class hierarchy rather than one exception with a
    string `.reason` field.
    """


class BelowMinimumWithdrawal(WithdrawalRejected):
    pass


class InsufficientAvailableBalance(WithdrawalRejected):
    pass


class KycLevelTooLow(WithdrawalRejected):
    pass


class RecentReversibleDeposit(WithdrawalRejected):
    pass


class UnknownWithdrawer(WithdrawalRejected):
    pass


class SimulatedPlayerCannotWithdraw(WithdrawalRejected):
    """Defense in depth: nothing today actually drives a bot's own
    Telegram/Mini-App session to reach this function at all (see
    services/admin/simulated_players_queries.py's own module docstring),
    but a simulated player's real user_cash balance -- funded from
    house_float so it can stake through the real game engine -- must
    never be extractable as a real payout regardless of how this
    function ever gets called in the future.
    """


@dataclass(frozen=True)
class WithdrawalIntent:
    payment_id: int
    our_ref: str
    status: str  # 'approved' (queued for payout) | 'review' (admin queue)


async def request_withdrawal(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: PaymentProvider,
    *,
    user_id: int,
    amount: Decimal,
    method_kind: str,
    account_ref: str,
    holder_name: str,
    min_withdraw: Decimal,
    auto_approve_limit: Decimal,
    kyc_threshold: Decimal,
    chargeback_window_minutes: int,
    min_account_age_hours: float = 24.0,
    max_withdrawals_per_day: int = 3,
    force_review: bool = False,
) -> WithdrawalIntent:
    # One span for the whole request -- the withdrawal path spec section
    # 10.4 asks traced "end to end" starts here, not just at the point a
    # payout is actually dispatched. The context manager records any
    # raised exception (BelowMinimumWithdrawal, KycLevelTooLow, ...) onto
    # the span automatically, so every rejection reason is visible in a
    # trace, not just successful requests.
    with _tracer.start_as_current_span(
        "withdrawal.request", attributes={"user_id": user_id, "amount": str(amount)}
    ) as span:
        if amount < min_withdraw:
            raise BelowMinimumWithdrawal(f"amount {amount} is below the minimum {min_withdraw}")

        async with pool.acquire() as conn:
            async with conn.transaction():
                # FOR NO KEY UPDATE, not FOR UPDATE: it still serializes two
                # withdrawal requests (and a KYC or status change) for this
                # player, but doesn't block the FOR KEY SHARE every foreign-key
                # insert referencing the player takes. FOR UPDATE deadlocked
                # this request against Bingo settlement paying the same player:
                # settlement locks user_cash (payout post) and then wants this
                # row for round_winners' foreign key, while this held this row
                # and waited for user_cash (test_player_row_lock_order.py).
                user = await conn.fetchrow(
                    "SELECT kyc_level, created_at, is_simulated FROM users WHERE id = $1 FOR NO KEY UPDATE",
                    user_id,
                )
                if user is None:
                    raise UnknownWithdrawer(f"user {user_id} does not exist")
                if user["is_simulated"]:
                    raise SimulatedPlayerCannotWithdraw(
                        f"user {user_id} is a simulated player and can never withdraw real money"
                    )

                if amount > kyc_threshold and user["kyc_level"] < 2:
                    raise KycLevelTooLow(
                        f"user {user_id} has kyc_level {user['kyc_level']}, needs >= 2 for amount {amount}"
                    )

                recent_deposit = await conn.fetchval(
                    """
                    SELECT 1 FROM payments
                    WHERE user_id = $1 AND direction = 'in' AND status = 'succeeded'
                      AND created_at > now() - make_interval(mins => $2)
                    LIMIT 1
                    """,
                    user_id,
                    chargeback_window_minutes,
                )
                if recent_deposit:
                    raise RecentReversibleDeposit(
                        f"user {user_id} has a succeeded deposit within the last "
                        f"{chargeback_window_minutes} minutes"
                    )

                method_row = await conn.fetchrow(
                    """
                    INSERT INTO payment_methods (user_id, kind, account_ref, holder_name)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id, kind, account_ref) DO UPDATE SET holder_name = EXCLUDED.holder_name
                    RETURNING id
                    """,
                    user_id,
                    method_kind,
                    account_ref,
                    holder_name,
                )
                assert method_row is not None
                method_id: int = method_row["id"]

                ref_row = await conn.fetchrow(
                    "SELECT 'WD-' || extract(year from now())::text || '-' || "
                    "lpad(nextval('payment_ref_seq')::text, 6, '0') AS our_ref"
                )
                assert ref_row is not None
                our_ref: str = ref_row["our_ref"]
                span.set_attribute("withdrawal.our_ref", our_ref)

                cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
                locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
                try:
                    txn = await ledger.post(
                        conn,
                        "withdrawal",
                        [ledger.Entry(cash.id, -amount), ledger.Entry(locked.id, amount)],
                        idempotency_key=our_ref,
                    )
                except ledger.InsufficientFunds as exc:
                    raise InsufficientAvailableBalance(
                        f"user {user_id} does not have {amount} of available cash"
                    ) from exc

                # A code review pass caught this as three near-identical
                # scans over the same payments rows for the same user_id --
                # one FILTER-based query, bucketing by kind/window in a
                # single pass, gives the exact same three figures (same
                # precedent as services/admin/queries.py's own
                # dashboard_summary()) instead of three separate round
                # trips, all held inside this function's own row-locked
                # transaction on the user's account.
                #
                # recent_withdrawal_count: spec 8.4's "Withdrawal velocity
                # > 3/day -> Review". Counts every withdrawal request in
                # the trailing 24h regardless of its own outcome -- a burst
                # of requests is the suspicious signal itself, not just the
                # ones that happened to succeed. This request hasn't been
                # inserted yet, so a count of max_withdrawals_per_day
                # already-existing rows means this one would be the
                # (max + 1)th.
                totals = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(amount) FILTER (WHERE direction = 'in' AND status = 'succeeded'), 0)
                            AS lifetime_in,
                        COALESCE(SUM(amount) FILTER (WHERE direction = 'out' AND status = 'succeeded'), 0)
                            AS lifetime_out,
                        count(*) FILTER (
                            WHERE direction = 'out' AND created_at > now() - interval '24 hours'
                        ) AS recent_withdrawal_count
                    FROM payments
                    WHERE user_id = $1
                    """,
                    user_id,
                )
                assert totals is not None
                lifetime_in = totals["lifetime_in"]
                lifetime_out = totals["lifetime_out"]
                recent_withdrawal_count = totals["recent_withdrawal_count"]
                account_age = datetime.now(UTC) - user["created_at"]
                # A code review pass caught that the four rules below only
                # ever collapsed to one bare boolean -- an admin working
                # the review queue had no way to tell *why* a given
                # request landed there without manually re-deriving it by
                # re-querying every related table by hand. Each failing
                # rule is recorded in review_reason instead, built from
                # the exact same values auto_ok itself is computed from,
                # not a second, separately-invented explanation.
                failed_checks: list[str] = []
                if amount > auto_approve_limit:
                    failed_checks.append(f"amount {amount} exceeds auto-approve limit {auto_approve_limit}")
                if account_age.total_seconds() <= min_account_age_hours * 3600:
                    failed_checks.append(f"account age below {min_account_age_hours:g}h minimum")
                if lifetime_in < lifetime_out:
                    failed_checks.append(
                        f"lifetime withdrawals ({lifetime_out}) exceed lifetime deposits ({lifetime_in})"
                    )
                if recent_withdrawal_count >= max_withdrawals_per_day:
                    failed_checks.append(
                        f"{recent_withdrawal_count} withdrawals in the last 24h "
                        f"(max {max_withdrawals_per_day})"
                    )

                # force_review=True (the manual rail -- see
                # services/payments/manual_provider.py) always lands in
                # review regardless of these checks: a manual withdrawal
                # has no automated dispatch to gate in the first place, a
                # human must act on every single one. failed_checks is
                # still computed and folded into review_reason even then
                # -- an admin working a manual-withdrawal queue should be
                # able to tell it's *also* high-risk on top of just being
                # manual, not see a blank reason.
                auto_ok = not failed_checks and not force_review
                status = STATUS_APPROVED if auto_ok else STATUS_REVIEW
                if force_review:
                    failed_checks = [*failed_checks, "manual rail requires human settlement"]
                review_reason = "; ".join(failed_checks) if failed_checks else None
                span.set_attribute("withdrawal.status", status)
                # A code-review pass caught that review_reason (added
                # alongside the payments row itself, see DECISIONS.md) was
                # never surfaced in the trace this span already carries --
                # an on-call engineer looking at withdrawal.request in
                # Jaeger/Tempo for a review-routed request had no way to
                # see *why* without a separate database round trip, the
                # same visibility gap already closed for the admin review
                # queue itself. OTel attributes don't accept None, so this
                # only sets it when there's an actual reason to show.
                if review_reason is not None:
                    span.set_attribute("withdrawal.review_reason", review_reason)

                payment_row = await conn.fetchrow(
                    """
                    INSERT INTO payments
                        (user_id, direction, provider, our_ref, amount, status, method_id,
                         ledger_txn_id, review_reason)
                    VALUES ($1, 'out', $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    user_id,
                    provider.name,
                    our_ref,
                    amount,
                    status,
                    method_id,
                    txn.id,
                    review_reason,
                )
                assert payment_row is not None
                payment_id: int = payment_row["id"]

        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        await ledger.publish_balance_update(pool, redis, user_id)
        if status == STATUS_APPROVED:
            await enqueue_payout(redis, our_ref=our_ref, payment_id=payment_id)

        return WithdrawalIntent(payment_id=payment_id, our_ref=our_ref, status=status)


async def enqueue_payout(redis: Redis, *, our_ref: str, payment_id: int) -> None:
    await redis.xadd(PAYOUT_STREAM, {"our_ref": our_ref, "payment_id": str(payment_id)})


async def sweep_stuck_approved_payouts(
    pool: asyncpg.Pool, redis: Redis, *, older_than_seconds: int = 60
) -> list[int]:
    """Re-enqueues any 'approved' withdrawal that's been sitting for a
    while with no corresponding payout ever dispatched -- a real gap a
    code review pass caught: enqueue_payout() (the Redis XADD) runs
    *after* request_withdrawal()'s own DB transaction commits, not inside
    it (Redis isn't part of that transaction). A crash or a Redis blip in
    the narrow window between the commit and the XADD leaves a withdrawal
    stuck at status='approved' forever -- funds already locked out of
    user_cash, but nothing ever queued to actually pay them out, and
    nothing else sweeps for this.

    Safe to run on a timer regardless of whether the original enqueue
    landed too, the same "poll as a fallback, not a replacement" design
    services/payments/deposits.py's poll_pending_deposits() already uses:
    a redundant re-enqueue for a withdrawal already sitting in the stream
    is a structural no-op on the processing side --
    payout_worker.process_one()'s own first check
    (payment.status not in _PENDING_STATUSES) safely skips anything
    already settled, and Chapa's own our_ref idempotency covers a
    still-pending one being dispatched to create_payout() more than once.
    Returns the payment ids this pass actually re-enqueued.

    provider != 'manual': a code-review pass caught that a manual
    withdrawal also reaches status='approved' (approve_manual_withdrawal_
    admin), where it's meant to sit -- often far longer than
    older_than_seconds -- until an admin has actually sent the transfer
    by hand and calls settle_manual_withdrawal_admin. Without this
    filter, every pending manual withdrawal got re-enqueued onto
    PAYOUT_STREAM on every single sweep tick, forever, for its entire
    time awaiting settlement -- caught and skipped by payout_worker.
    process_one()'s provider-mismatch guard, but only after a wasted
    XADD/ack cycle and a spurious payout_provider_mismatch error log
    each time. Matches the exact same exclusion admin/queries.py's
    list_pending_withdrawals()/approve_withdrawal_admin() already apply
    to the automatic-rail-only 'approved'/'review' queues.
    """
    rows = await pool.fetch(
        "SELECT id, our_ref FROM payments WHERE direction = 'out' AND status = $1 "
        "AND provider != 'manual' AND updated_at < now() - make_interval(secs => $2)",
        STATUS_APPROVED,
        older_than_seconds,
    )
    for row in rows:
        await enqueue_payout(redis, our_ref=row["our_ref"], payment_id=row["id"])
    return [row["id"] for row in rows]
