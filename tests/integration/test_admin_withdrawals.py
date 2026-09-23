"""Tests for the admin withdrawal review queue (services/admin/queries.py's
approve_withdrawal_admin/reject_withdrawal_admin and the matching routes in
services/admin/app.py): the queue itself, the ledger-backed reversal on
rejection, RBAC over real HTTP, and the audit trail.
"""

from decimal import Decimal

import httpx

from packages.core import ledger
from services.admin import queries
from services.payments import payout_worker, withdrawals
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_payout_worker import FakePayoutProvider


class _NullProvider:
    name = "chapa"

    async def create_checkout(self, **kwargs):
        raise NotImplementedError

    def verify_webhook(self, headers, raw_body):
        raise NotImplementedError

    async def fetch_status(self, our_ref):
        raise NotImplementedError

    async def create_payout(self, **kwargs):
        raise NotImplementedError


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _locked(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_locked")
    return await ledger.balance(conn, account.id)


async def _review_withdrawal(pool, redis, conn, user_id: int, amount: Decimal) -> tuple[int, str]:
    intent = await withdrawals.request_withdrawal(
        pool,
        redis,
        _NullProvider(),
        user_id=user_id,
        amount=amount,
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("0.00"),  # forces status='review' every time
        kyc_threshold=Decimal("1000000.00"),
        chargeback_window_minutes=0,
        min_account_age_hours=0,
    )
    assert intent.status == withdrawals.STATUS_REVIEW
    return intent.payment_id, intent.our_ref


async def test_list_pending_withdrawals_shows_review_items(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    rows = await queries.list_pending_withdrawals(pool)
    match = next((r for r in rows if r["id"] == payment_id and r["our_ref"] == our_ref), None)
    assert match is not None
    # _review_withdrawal forces review via auto_approve_limit=0.00 -- the
    # admin queue must show *why*, not just that it's sitting there.
    assert match["review_reason"] is not None
    assert "auto-approve limit" in match["review_reason"]


async def test_list_stuck_processing_payouts_surfaces_an_unresolved_transfer(pool, redis, conn):
    # A code review pass caught payout_worker.py treating a provider
    # "processing" result as fully settled -- fixed to leave it genuinely
    # unresolved instead (see test_processing_status_is_not_treated_as_
    # settled in test_payout_worker.py for that half). This is the
    # operator-visibility half: with no automated way to ever learn what
    # actually happened to a "processing" transfer, an admin needs at
    # least a way to find these and go check with Chapa directly.
    user_id = await create_funded_user(conn, Decimal("500.00"))
    intent = await withdrawals.request_withdrawal(
        pool,
        redis,
        _NullProvider(),
        user_id=user_id,
        amount=Decimal("100.00"),
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("100000.00"),
        kyc_threshold=Decimal("1000000.00"),
        chargeback_window_minutes=0,
        min_account_age_hours=0,
    )
    assert intent.status == withdrawals.STATUS_APPROVED

    provider = FakePayoutProvider(outcomes={intent.our_ref: "processing"})
    outcome = await payout_worker.process_next(pool, redis, provider, consumer_name="stuck-processing-test")
    assert outcome == "processing"

    # Not yet old enough -- must not flag a transfer that's simply
    # mid-flight through a normal, still-recent dispatch.
    too_soon = await queries.list_stuck_processing_payouts(pool, older_than_seconds=3600)
    assert not any(r["id"] == intent.payment_id for r in too_soon)

    await conn.execute(
        "UPDATE payments SET updated_at = now() - interval '2 hours' WHERE id = $1", intent.payment_id
    )

    stuck = await queries.list_stuck_processing_payouts(pool, older_than_seconds=3600)
    match = next((r for r in stuck if r["id"] == intent.payment_id), None)
    assert match is not None
    assert match["our_ref"] == intent.our_ref
    assert match["amount"] == Decimal("100.00")
    # provider_ref must be there -- it's what an admin actually needs to
    # look this transfer up with Chapa directly.
    assert match["provider_ref"] == f"chapa-{intent.our_ref}"


async def test_approve_withdrawal_admin_enqueues_a_payout_job(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    approved = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="looks fine", ip_address="10.0.0.1"
    )
    assert approved is True

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"

    outcome = await payout_worker.process_next(pool, redis, FakePayoutProvider(), consumer_name="admin-approve-test")
    assert outcome == "succeeded"
    assert await _cash(conn, user_id) == Decimal("400.00")

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row["action"] == "withdrawals.approve"


async def test_approve_withdrawal_admin_is_a_noop_when_not_in_review(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    first = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason=None, ip_address=None
    )
    assert first is True

    second = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason=None, ip_address=None
    )
    assert second is False


async def test_reject_withdrawal_admin_returns_exact_amount_to_cash(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("150.00"))

    assert await _locked(conn, user_id) == Decimal("150.00")
    assert await _cash(conn, user_id) == Decimal("350.00")

    rejected = await queries.reject_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="suspected fraud", ip_address="10.0.0.1"
    )
    assert rejected is True

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("500.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "rejected"

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_id = $1 ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row["action"] == "withdrawals.reject"
    assert audit_row["reason"] == "suspected fraud"


async def test_reject_withdrawal_admin_rejects_empty_reason_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/reject", headers=headers, json={"reason": "   "}
        )
    assert response.status_code == 422


async def test_approve_withdrawal_admin_rejects_empty_reason_over_http(admin_server, pool, redis, conn):
    # A code review pass caught that this route had a required
    # `reason: str` field on its own request model but never actually
    # enforced it -- every sibling route (reject, void, adjust,
    # set-status) already required a non-blank reason for the exact same
    # "no hidden god mode" accountability reason; approving a withdrawal
    # (releasing real money) is not less consequential than rejecting one.
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/approve", headers=headers, json={"reason": "   "}
        )
    assert response.status_code == 422

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"  # unchanged -- the rejected request must not have approved it anyway


async def test_support_cannot_approve_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/approve", headers=headers, json={"reason": "ok"}
        )
    assert response.status_code == 403


async def test_finance_can_approve_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/withdrawals/{payment_id}/approve",
            headers=headers,
            json={"reason": "identity and amount verified"},
        )
    assert response.status_code == 200
    assert response.json()["approved"] is True


async def test_support_can_view_pending_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/withdrawals", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)
