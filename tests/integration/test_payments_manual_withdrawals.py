"""Manual withdrawals: request_withdrawal's force_review path
(services/payments/withdrawals.py) and the two-checkpoint admin
settlement flow (services/admin/queries.py's manual_withdrawals.* /
approve_manual_withdrawal_admin / settle_manual_withdrawal_admin /
fail_manual_withdrawal_admin). Also covers the safety guards this stage
added so a manual withdrawal can never be cross-dispatched to the
automatic Chapa payout path: approve_withdrawal_admin's provider !=
'manual' guard, list_pending_withdrawals excluding manual rows, and
payout_worker.process_one()'s defense-in-depth provider check.
"""

import asyncio
from decimal import Decimal

import httpx
import pytest

from packages.core import ledger
from services.admin import queries
from services.payments import payout_worker, withdrawals
from services.payments.manual_provider import ManualProvider
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_withdrawals import _NullProvider
from tests.integration.test_payout_worker import FakePayoutProvider


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _locked(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_locked")
    return await ledger.balance(conn, account.id)


async def _manual_withdrawal(pool, redis, user_id: int, amount: Decimal) -> tuple[int, str]:
    intent = await withdrawals.request_withdrawal(
        pool,
        redis,
        ManualProvider(),
        user_id=user_id,
        amount=amount,
        method_kind="telebirr",
        account_ref="0911223344",
        holder_name="Test Holder",
        min_withdraw=Decimal("10.00"),
        auto_approve_limit=Decimal("100000.00"),  # would auto-approve if this weren't forced
        kyc_threshold=Decimal("1000000.00"),
        chargeback_window_minutes=0,
        min_account_age_hours=0,
        force_review=True,
    )
    assert intent.status == withdrawals.STATUS_REVIEW
    return intent.payment_id, intent.our_ref


async def test_manual_withdrawal_locks_funds_and_lands_in_review_even_under_the_auto_approve_limit(
    pool, redis, conn
):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("100.00"))

    assert await _locked(conn, user_id) == Decimal("100.00")
    assert await _cash(conn, user_id) == Decimal("400.00")

    row = await conn.fetchrow("SELECT status, provider, review_reason FROM payments WHERE id = $1", payment_id)
    assert row["status"] == "review"
    assert row["provider"] == "manual"
    assert "manual rail requires human settlement" in row["review_reason"]


async def test_admin_approve_is_a_checkpoint_only_no_ledger_row_yet(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("100.00"))

    approved = await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="verified identity", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )
    assert approved == "approved"

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"
    # Still locked -- approving is a decision to pay, not the payment.
    assert await _locked(conn, user_id) == Decimal("100.00")
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"manual-payout-settle-{our_ref}"
    )
    assert txn_count == 0


# --- Two-person approval (amount >= two_person_threshold) --------------


async def test_at_threshold_first_approval_awaits_second_without_approving(pool, redis, conn):
    first_admin, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("5000.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("2000.00"))

    outcome = await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=first_admin, payment_id=payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert outcome == "awaiting_second_approval"

    row = await conn.fetchrow(
        "SELECT status, first_approved_by_admin_id FROM payments WHERE id = $1", payment_id
    )
    assert row["status"] == "review"  # not yet 'approved' -- funds stay locked, unsent
    assert row["first_approved_by_admin_id"] == first_admin
    assert await _locked(conn, user_id) == Decimal("2000.00")


async def test_second_different_admin_approves_exactly_once_above_threshold(pool, redis, conn):
    first_admin, *_ = await create_test_admin(pool)
    second_admin, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("5000.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("2500.00"))

    first_outcome = await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=first_admin, payment_id=payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert first_outcome == "awaiting_second_approval"

    second_outcome = await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=second_admin, payment_id=payment_id, reason="confirmed identity",
        ip_address="10.0.0.2", two_person_threshold=Decimal("2000.00"),
    )
    assert second_outcome == "approved"

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"


async def test_concurrent_approvals_by_two_different_admins_approves_exactly_once_above_threshold(
    pool, redis, conn
):
    first_admin, *_ = await create_test_admin(pool)
    second_admin, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("5000.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("3000.00"))

    async def approve(admin_id: int):
        return await queries.approve_manual_withdrawal_admin(
            pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )

    results = await asyncio.gather(approve(first_admin), approve(second_admin))
    assert sorted(results) == ["approved", "awaiting_second_approval"]

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"


async def test_same_admin_cannot_provide_second_withdrawal_approval_above_threshold(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("5000.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("2000.00"))

    first_outcome = await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert first_outcome == "awaiting_second_approval"

    with pytest.raises(queries.SameAdminCannotProvideSecondApproval):
        await queries.approve_manual_withdrawal_admin(
            pool, redis, admin_id=admin_id, payment_id=payment_id, reason="trying again",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"
    assert await _locked(conn, user_id) == Decimal("2000.00")


async def test_settle_releases_locked_funds_to_the_provider_settlement_account(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("150.00"))
    await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )

    settled = await queries.settle_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, external_reference="TXN-REAL-123",
        reason="sent via Telebirr", ip_address="10.0.0.1",
    )
    assert settled is True

    assert await _locked(conn, user_id) == Decimal("0.00")
    row = await conn.fetchrow("SELECT status, provider_ref FROM payments WHERE id = $1", payment_id)
    assert row["status"] == "succeeded"
    assert row["provider_ref"] == "TXN-REAL-123"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"manual-payout-settle-{our_ref}"
    )
    assert txn_count == 1


async def test_concurrent_double_settlement_pays_exactly_once(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("200.00"))
    await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )

    async def settle():
        return await queries.settle_manual_withdrawal_admin(
            pool, redis, admin_id=admin_id, payment_id=payment_id, external_reference="TXN-RACE-1",
            reason="ok", ip_address="10.0.0.1",
        )

    results = await asyncio.gather(*(settle() for _ in range(20)))
    assert results.count(True) == 1
    assert results.count(False) == 19

    assert await _locked(conn, user_id) == Decimal("0.00")
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"manual-payout-settle-{our_ref}"
    )
    assert txn_count == 1


async def test_settle_post_commit_retry_is_a_clean_noop(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("70.00"))
    await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )

    first = await queries.settle_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, external_reference="TXN-777",
        reason="ok", ip_address="10.0.0.1",
    )
    assert first is True

    retry = await queries.settle_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, external_reference="TXN-777",
        reason="ok", ip_address="10.0.0.1",
    )
    assert retry is False
    assert await _locked(conn, user_id) == Decimal("0.00")


async def test_fail_after_approval_returns_the_exact_amount_to_cash(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("90.00"))
    await queries.approve_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )

    failed = await queries.fail_manual_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="destination account closed",
        ip_address="10.0.0.1",
    )
    assert failed is True

    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("500.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "failed"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"manual-payout-fail-{payment_id}"
    )
    assert txn_count == 1


async def test_existing_reject_withdrawal_admin_works_unmodified_on_a_manual_row(pool, redis, conn):
    # Confirms the plan's own finding: reject_withdrawal_admin needs zero
    # changes to correctly reverse a manual withdrawal sitting in
    # 'review' -- its guard query has no provider filter.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("120.00"))

    rejected = await queries.reject_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="could not verify identity",
        ip_address="10.0.0.1",
    )
    assert rejected is True
    assert await _locked(conn, user_id) == Decimal("0.00")
    assert await _cash(conn, user_id) == Decimal("500.00")


async def test_list_pending_withdrawals_excludes_manual_rows(pool, redis, conn):
    user_id = await create_funded_user(conn, Decimal("500.00"))
    manual_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("50.00"))

    rows = await queries.list_pending_withdrawals(pool)
    assert not any(r["id"] == manual_id for r in rows)

    manual_rows = await queries.list_pending_manual_withdrawals(pool)
    assert any(r["id"] == manual_id for r in manual_rows)


async def test_approve_withdrawal_admin_refuses_a_manual_row(pool, redis, conn):
    # The safety guard this stage added: the general (automatic-rail)
    # approve route must never touch a manual-provider row, since its
    # real job past the status flip is enqueueing an automatic payout
    # dispatch that has no business processing a manual payment.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_funded_user(conn, Decimal("500.00"))
    manual_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("50.00"))

    result = await queries.approve_withdrawal_admin(
        pool, redis, admin_id=admin_id, payment_id=manual_id, reason="ok", ip_address="10.0.0.1"
    )
    assert result is False

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", manual_id)
    assert status == "review"  # unchanged


async def test_payout_worker_refuses_to_dispatch_a_provider_mismatched_job(pool, redis, conn):
    # Defense in depth: even if a manual payment's our_ref somehow ended
    # up on the automatic payout stream (it shouldn't, given the guard
    # above), the worker itself must refuse to call create_payout() on
    # the wrong rail's provider instance rather than silently doing it.
    user_id = await create_funded_user(conn, Decimal("500.00"))
    manual_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("50.00"))
    # Force it to 'approved' directly (bypassing the real guard above) to
    # simulate the exact "should never happen" scenario this check exists
    # for, then hand-craft the stream entry the way enqueue_payout would.
    await conn.execute("UPDATE payments SET status = 'approved' WHERE id = $1", manual_id)
    msg_id = await redis.xadd(payout_worker.PAYOUT_STREAM, {"our_ref": our_ref, "payment_id": str(manual_id)})

    provider = FakePayoutProvider()  # a chapa-shaped fake, not the manual rail
    outcome = await payout_worker.process_one(pool, redis, provider, msg_id=msg_id, our_ref=our_ref)
    assert outcome == "skipped"

    # Never touched by the (wrong) provider -- still 'approved', still locked.
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", manual_id)
    assert status == "approved"
    assert await _locked(conn, user_id) == Decimal("50.00")


async def test_support_cannot_settle_manual_withdrawals_over_http(admin_server, pool, redis, conn):
    support_headers = await _auth_headers(admin_server, pool, role="support")
    finance_headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("80.00"))

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/approve",
            headers=finance_headers,
            json={"reason": "identity verified"},
        )
        response = await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/settle",
            headers=support_headers,
            json={"external_reference": "TXN-1", "reason": "sent via bank transfer"},
        )
    assert response.status_code == 403


async def test_finance_can_approve_and_settle_manual_withdrawals_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("80.00"))

    async with httpx.AsyncClient() as client:
        approve_response = await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/approve",
            headers=headers,
            json={"reason": "identity verified"},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["outcome"] == "approved"

        settle_response = await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/settle",
            headers=headers,
            json={"external_reference": "TXN-2", "reason": "sent via bank transfer"},
        )
    assert settle_response.status_code == 200
    assert settle_response.json()["settled"] is True


async def test_settle_requires_a_non_blank_external_reference_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, _ = await _manual_withdrawal(pool, redis, user_id, Decimal("80.00"))

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/approve",
            headers=headers,
            json={"reason": "identity verified"},
        )
        response = await client.post(
            f"{admin_server}/manual-withdrawals/{payment_id}/settle",
            headers=headers,
            # A real, well-formed reason -- this test isolates the
            # external_reference validation specifically, so the reason
            # itself must be one that would otherwise pass cleanly.
            json={"external_reference": "   ", "reason": "sent via bank transfer"},
        )
    assert response.status_code == 422

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "approved"
