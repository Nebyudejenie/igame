"""Admin-side manual deposit review (services/admin/queries.py's
manual_deposit_* functions and the matching /manual-deposits routes in
services/admin/app.py): the queue listing, RBAC over real HTTP, the
audit trail, the live (not stale-flag) duplicate-reference detection, and
the receipt-photo proxy route.
"""

import json
import uuid
from decimal import Decimal

import httpx

from packages.core import ledger
from packages.core.notifications import NOTIFICATIONS_STREAM
from services.admin import queries
from services.payments import manual
from tests.integration.conftest import create_user
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


def _unique_ref() -> str:
    # provider_ref is player-typed free text with no DB-level uniqueness
    # constraint (unlike our_ref) -- a literal hardcoded string here would
    # collide with the exact same literal left behind by an earlier run of
    # this same suite against a shared, never-torn-down dev database, and
    # get flagged as a false possible_duplicate_reference. Real duplicate
    # references (the thing under test) are built by reusing this same
    # value on purpose within one test, not by accident across test runs.
    return f"FT-{uuid.uuid4().hex[:10]}"


async def _active_destination(conn) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, instructions)
        VALUES ('telebirr', '0911000000', 'Zemen Game PLC', 'Send exactly the requested amount')
        RETURNING id
        """
    )
    return row["id"]


async def _pending_manual_deposit(pool, redis, conn, user_id: int, amount: Decimal, ref: str) -> tuple[int, str]:
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=amount,
        manual_destination_id=destination_id,
        external_reference=ref,
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    return intent.payment_id, intent.our_ref


async def test_list_pending_manual_deposits_shows_all_required_fields(pool, redis, conn):
    user_id = await create_user(conn)
    external_reference = _unique_ref()
    payment_id, our_ref = await _pending_manual_deposit(
        pool, redis, conn, user_id, Decimal("150.00"), external_reference
    )

    rows = await queries.list_pending_manual_deposits(pool)
    match = next((r for r in rows if r["id"] == payment_id), None)
    assert match is not None
    assert match["our_ref"] == our_ref
    assert match["user_id"] == user_id
    assert match["amount"] == Decimal("150.00")
    assert match["status"] == "review"
    assert match["method_kind"] == "telebirr"
    assert match["destination_account_ref"] == "0911000000"
    assert match["external_reference"] == external_reference
    assert match["possible_duplicate_reference"] is False


async def test_duplicate_reference_is_flagged_live_and_clears_on_rejection(pool, redis, conn):
    user_id = await create_user(conn)
    shared_ref = _unique_ref()
    first_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("100.00"), shared_ref)
    second_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("100.00"), shared_ref)

    rows = await queries.list_pending_manual_deposits(pool)
    first_row = next(r for r in rows if r["id"] == first_id)
    second_row = next(r for r in rows if r["id"] == second_id)
    assert first_row["possible_duplicate_reference"] is True
    assert second_row["possible_duplicate_reference"] is True

    admin_id, *_ = await create_test_admin(pool)
    rejected = await queries.reject_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=first_id, reason="the other one is the real one",
        ip_address="10.0.0.1",
    )
    assert rejected is True

    # Proves this is computed live, not a stale flag set at insert time:
    # once the earlier conflicting request is rejected, the flag on the
    # remaining one must clear.
    rows_after = await queries.list_pending_manual_deposits(pool)
    second_row_after = next(r for r in rows_after if r["id"] == second_id)
    assert second_row_after["possible_duplicate_reference"] is False


async def test_approve_writes_an_audit_row(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="matched bank statement",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )

    audit_row = await conn.fetchrow(
        "SELECT action, before, after, reason, admin_id FROM admin_audit_log "
        "WHERE target_type = 'payment' AND target_id = $1 ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row is not None
    assert audit_row["action"] == "manual_deposits.approve"
    assert audit_row["admin_id"] == admin_id
    assert audit_row["reason"] == "matched bank statement"


async def test_reject_writes_an_audit_row_with_reason(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    await queries.reject_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="reference does not match any statement line",
        ip_address="10.0.0.1",
    )

    audit_row = await conn.fetchrow(
        "SELECT action, reason FROM admin_audit_log WHERE target_type = 'payment' AND target_id = $1 "
        "ORDER BY id DESC LIMIT 1",
        str(payment_id),
    )
    assert audit_row["action"] == "manual_deposits.reject"
    assert audit_row["reason"] == "reference does not match any statement line"


async def test_approve_enqueues_a_real_telegram_notification(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    before = await redis.xlen(NOTIFICATIONS_STREAM)
    await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )
    after = await redis.xlen(NOTIFICATIONS_STREAM)
    assert after == before + 1

    entries = await redis.xrange(NOTIFICATIONS_STREAM, "-", "+")
    _, fields = entries[-1]
    assert fields["key"] == "notify.deposit_confirmed"


async def test_reject_enqueues_a_real_telegram_notification(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    before = await redis.xlen(NOTIFICATIONS_STREAM)
    await queries.reject_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="no match", ip_address="10.0.0.1"
    )
    after = await redis.xlen(NOTIFICATIONS_STREAM)
    assert after == before + 1

    entries = await redis.xrange(NOTIFICATIONS_STREAM, "-", "+")
    _, fields = entries[-1]
    assert fields["key"] == "notify.manual_deposit_rejected"


async def test_support_can_view_but_not_approve_manual_deposits_over_http(admin_server, pool, redis, conn):
    support_headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        list_response = await client.get(f"{admin_server}/manual-deposits", headers=support_headers)
        assert list_response.status_code == 200

        approve_response = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve",
            headers=support_headers,
            json={"reason": "ok"},
        )
    assert approve_response.status_code == 403


async def test_finance_can_approve_manual_deposits_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve", headers=headers, json={"reason": "ok"}
        )
    assert response.status_code == 200
    assert response.json()["outcome"] == "credited"


async def test_approve_manual_deposit_rejects_empty_reason_over_http(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve", headers=headers, json={"reason": "   "}
        )
    assert response.status_code == 422

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"


async def test_receipt_route_404s_when_nothing_attached(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="support")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("60.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{admin_server}/manual-deposits/{payment_id}/receipt", headers=headers)
    assert response.status_code == 404


async def test_notification_failure_never_blocks_or_reverses_the_credit(pool, redis, conn, monkeypatch):
    # notify_user()'s own contract (packages/core/notifications.py) is to
    # catch *any* exception and just log it -- this proves that contract
    # holds for real from the caller's side too: a genuinely broken
    # notification channel must never roll back, or even just fail to
    # report, a real-money credit that already committed. Breaks only
    # redis.xadd (what notify_user calls) -- publish_balance_update's own
    # redis.publish() call is untouched, so a real balance push still
    # goes out even though the Telegram notification specifically can't.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, our_ref = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("80.00"), _unique_ref())

    async def broken_xadd(*args, **kwargs):
        raise ConnectionError("simulated: notification stream unreachable")

    monkeypatch.setattr(redis, "xadd", broken_xadd)

    approved = await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )
    assert approved == "credited"

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "succeeded"
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", our_ref
    )
    assert txn_count == 1


# --- Two-person approval: HTTP boundary, audit trail, reject interplay --


async def test_same_admin_cannot_double_approve_over_http_returns_409(admin_server, pool, redis, conn):
    headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("2500.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        first = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve", headers=headers, json={"reason": "first look"}
        )
        assert first.status_code == 200
        assert first.json()["outcome"] == "awaiting_second_approval"

        second = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve", headers=headers, json={"reason": "again"}
        )
    assert second.status_code == 409
    assert second.json()["detail"] == "same_admin_cannot_double_approve"

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"  # the rejected second call never moved anything


async def test_two_different_finance_admins_can_approve_the_same_high_value_deposit_over_http(
    admin_server, pool, redis, conn
):
    first_headers = await _auth_headers(admin_server, pool, role="finance")
    second_headers = await _auth_headers(admin_server, pool, role="finance")
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("2500.00"), _unique_ref())

    async with httpx.AsyncClient() as client:
        first = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve", headers=first_headers, json={"reason": "ok"}
        )
        assert first.status_code == 200
        assert first.json()["outcome"] == "awaiting_second_approval"

        second = await client.post(
            f"{admin_server}/manual-deposits/{payment_id}/approve",
            headers=second_headers,
            json={"reason": "confirmed"},
        )
    assert second.status_code == 200
    assert second.json()["outcome"] == "credited"

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "succeeded"


async def test_audit_trail_shows_both_approvers_above_threshold(pool, redis, conn):
    first_admin, *_ = await create_test_admin(pool)
    second_admin, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("2200.00"), _unique_ref())

    await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=first_admin, payment_id=payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=second_admin, payment_id=payment_id, reason="confirmed externally",
        ip_address="10.0.0.2", two_person_threshold=Decimal("2000.00"),
    )

    rows = await conn.fetch(
        "SELECT action, admin_id, before, after FROM admin_audit_log "
        "WHERE target_type = 'payment' AND target_id = $1 ORDER BY id",
        str(payment_id),
    )
    assert [r["action"] for r in rows] == ["manual_deposits.first_approve", "manual_deposits.approve"]
    assert rows[0]["admin_id"] == first_admin
    assert rows[1]["admin_id"] == second_admin

    final_before = json.loads(rows[1]["before"])
    assert final_before["first_approved_by_admin_id"] == first_admin


async def test_reject_still_works_while_awaiting_second_approval(pool, redis, conn):
    # A request sitting in the awaiting-second-approval window is still
    # just status='review' -- reject_manual_deposit_admin's guard doesn't
    # know or care about first_approved_by_admin_id, so declining the
    # request outright must keep working exactly as it always has. Only
    # actually RELEASING the money needs the extra scrutiny.
    first_admin, *_ = await create_test_admin(pool)
    reviewer_admin, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    payment_id, _ = await _pending_manual_deposit(pool, redis, conn, user_id, Decimal("2100.00"), _unique_ref())

    first_outcome = await queries.approve_manual_deposit_admin(
        pool, redis, admin_id=first_admin, payment_id=payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert first_outcome == "awaiting_second_approval"

    rejected = await queries.reject_manual_deposit_admin(
        pool, redis, admin_id=reviewer_admin, payment_id=payment_id, reason="reference turned out to be fraudulent",
        ip_address="10.0.0.3",
    )
    assert rejected is True

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "rejected"
    assert await _cash(conn, user_id) == Decimal("0.00")
