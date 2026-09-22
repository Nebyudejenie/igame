"""Manual deposit request creation (services/payments/manual.py) -- the
player-facing half of the P1 "keep taking deposits when Chapa is down"
directive. Admin-side approve/reject is covered in
test_admin_manual_payments.py; this file is the domain layer:
request creation, the shared eligibility gates, and the receipt-photo
correlation helper.
"""

import asyncio
from decimal import Decimal

import pytest

from packages.core import ledger
from services.admin import queries as admin_queries
from services.payments import deposits, manual
from tests.integration.conftest import create_funded_user, create_user
from tests.integration.test_admin_auth import create_test_admin

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _active_destination(conn) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, instructions)
        VALUES ('telebirr', '0911000000', 'Zemen Game PLC', 'Send exactly the requested amount')
        RETURNING id
        """
    )
    return row["id"]


async def test_manual_deposit_request_starts_pending_review_no_credit(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)

    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("200.00"),
        manual_destination_id=destination_id,
        external_reference="FT26123ABC",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "review"
    assert await _cash(conn, user_id) == Decimal("0.00")  # not credited merely for submitting


async def test_daily_cap_counts_deposits_still_sitting_in_review(pool, redis, conn):
    # A code-review pass caught that _check_deposit_eligibility()'s cap
    # query only counted the automatic rail's own lifecycle statuses
    # (pending/processing/succeeded) -- a manual deposit spends its
    # entire pre-approval life at status='review', which wasn't in that
    # list. A player could submit any number of manual deposits while
    # they sat unreviewed, none of them counting against the cap, and
    # end up credited far past it the moment an admin worked through the
    # backlog. This proves the fix without needing an admin to actually
    # approve anything: two 'review' deposits alone must already trip a
    # cap the second one alone wouldn't exceed.
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    cap = Decimal("300.00")

    first = await manual.create_manual_deposit_request(
        pool, redis, user_id=user_id, amount=Decimal("200.00"),
        manual_destination_id=destination_id, external_reference="FT26-first",
        receipt_telegram_file_id=None, min_deposit=MIN_DEPOSIT, daily_cap=cap,
    )
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", first.payment_id)
    assert status == "review"  # still unreviewed -- exactly the state the bug ignored

    with pytest.raises(deposits.DailyDepositCapExceeded):
        await manual.create_manual_deposit_request(
            pool, redis, user_id=user_id, amount=Decimal("200.00"),
            manual_destination_id=destination_id, external_reference="FT26-second",
            receipt_telegram_file_id=None, min_deposit=MIN_DEPOSIT, daily_cap=cap,
        )


async def test_manual_deposit_below_minimum_is_rejected(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)

    with pytest.raises(Exception) as exc_info:
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("1.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )
    from services.payments.deposits import BelowMinimumDeposit

    assert isinstance(exc_info.value, BelowMinimumDeposit)


async def test_manual_deposit_rejects_an_unknown_destination(pool, redis, conn):
    user_id = await create_user(conn)

    with pytest.raises(manual.UnknownManualDestination):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=999999,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_manual_deposit_rejects_a_deactivated_destination(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    await conn.execute("UPDATE manual_payment_destinations SET is_active = false WHERE id = $1", destination_id)

    with pytest.raises(manual.UnknownManualDestination):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_manual_deposit_self_excluded_user_is_rejected(pool, redis, conn):
    # Proves the shared _check_deposit_eligibility gate really is shared,
    # not a second copy -- the exact same responsible-gaming rule an
    # automatic deposit already enforces.
    user_id = await create_user(conn)
    await conn.execute("UPDATE users SET status = 'self_excluded' WHERE id = $1", user_id)
    destination_id = await _active_destination(conn)

    from services.payments.deposits import DepositorSelfExcluded

    with pytest.raises(DepositorSelfExcluded):
        await manual.create_manual_deposit_request(
            pool,
            redis,
            user_id=user_id,
            amount=Decimal("100.00"),
            manual_destination_id=destination_id,
            external_reference="FT26999",
            receipt_telegram_file_id=None,
            min_deposit=MIN_DEPOSIT,
            daily_cap=DAILY_CAP,
        )


async def test_admin_approve_credits_exactly_once(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("300.00"),
        manual_destination_id=destination_id,
        external_reference="FT26555",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    outcome = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="verified externally",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert outcome == "credited"
    assert await _cash(conn, user_id) == Decimal("300.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_concurrent_double_approval_credits_exactly_once(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("120.00"),
        manual_destination_id=destination_id,
        external_reference="FT26777",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    async def approve():
        return await admin_queries.approve_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1",
            two_person_threshold=Decimal("2000.00"),
        )

    results = await asyncio.gather(*(approve() for _ in range(20)))
    assert results.count("credited") == 1
    assert results.count("no_op") == 19

    assert await _cash(conn, user_id) == Decimal("120.00")
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_admin_reject_never_credits(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("80.00"),
        manual_destination_id=destination_id,
        external_reference="FT26888",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    rejected = await admin_queries.reject_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="reference not found",
        ip_address="10.0.0.1",
    )
    assert rejected is True
    assert await _cash(conn, user_id) == Decimal("0.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "rejected"

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 0


async def test_post_commit_retry_is_a_clean_noop_not_a_double_credit(pool, redis, conn):
    # The literal "admin's browser times out after the server already
    # committed" scenario: call approve once for real, then call it again
    # as if it were a client-side retry of a response the admin never saw.
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("50.00"),
        manual_destination_id=destination_id,
        external_reference="FT26321",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    first = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )
    assert first == "credited"

    retry = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok", ip_address="10.0.0.1",
        two_person_threshold=Decimal("2000.00"),
    )
    assert retry == "no_op"  # clean no-op, not an exception, not a second credit
    assert await _cash(conn, user_id) == Decimal("50.00")


async def test_attach_receipt_to_latest_pending_deposit(pool, redis, conn):
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("90.00"),
        manual_destination_id=destination_id,
        external_reference="FT26456",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    attached_to = await manual.attach_receipt_to_latest_pending_deposit(
        pool, user_id=user_id, telegram_file_id="AgACAgQ-fake-file-id"
    )
    assert attached_to == intent.payment_id

    file_id = await conn.fetchval(
        "SELECT receipt_telegram_file_id FROM payments WHERE id = $1", intent.payment_id
    )
    assert file_id == "AgACAgQ-fake-file-id"


async def test_attach_receipt_returns_none_when_nothing_pending(pool, redis, conn):
    user_id = await create_user(conn)  # never made any manual deposit request
    attached_to = await manual.attach_receipt_to_latest_pending_deposit(
        pool, user_id=user_id, telegram_file_id="AgACAgQ-fake-file-id"
    )
    assert attached_to is None


# --- Two-person approval (amount >= two_person_threshold) --------------


async def test_at_threshold_first_approval_awaits_second_without_crediting(pool, redis, conn):
    first_admin, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("2000.00"),  # exactly at the threshold -- >= gates it
        manual_destination_id=destination_id,
        external_reference="FT26-THRESH-1",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    outcome = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=first_admin, payment_id=intent.payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert outcome == "awaiting_second_approval"
    assert await _cash(conn, user_id) == Decimal("0.00")

    row = await conn.fetchrow(
        "SELECT status, first_approved_by_admin_id FROM payments WHERE id = $1", intent.payment_id
    )
    assert row["status"] == "review"
    assert row["first_approved_by_admin_id"] == first_admin


async def test_second_different_admin_credits_exactly_once_above_threshold(pool, redis, conn):
    first_admin, *_ = await create_test_admin(pool)
    second_admin, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("2500.00"),
        manual_destination_id=destination_id,
        external_reference="FT26-THRESH-2",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    first_outcome = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=first_admin, payment_id=intent.payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert first_outcome == "awaiting_second_approval"

    second_outcome = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=second_admin, payment_id=intent.payment_id, reason="confirmed externally",
        ip_address="10.0.0.2", two_person_threshold=Decimal("2000.00"),
    )
    assert second_outcome == "credited"
    assert await _cash(conn, user_id) == Decimal("2500.00")

    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"
    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_concurrent_approvals_by_two_different_admins_credits_exactly_once_above_threshold(
    pool, redis, conn
):
    # Proves the row lock still serializes this correctly even when two
    # distinct admins race for the FIRST approval: exactly one of them
    # wins it (awaiting_second_approval), and the other -- serialized
    # behind the first one's commit -- immediately completes the second
    # approval and credits, rather than either double-crediting or both
    # ending up stuck awaiting a second approval that never comes.
    first_admin, *_ = await create_test_admin(pool)
    second_admin, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("3000.00"),
        manual_destination_id=destination_id,
        external_reference="FT26-THRESH-3",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    async def approve(admin_id: int):
        return await admin_queries.approve_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="ok",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )

    results = await asyncio.gather(approve(first_admin), approve(second_admin))
    assert sorted(results) == ["awaiting_second_approval", "credited"]
    assert await _cash(conn, user_id) == Decimal("3000.00")

    txn_count = await conn.fetchval(
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", intent.our_ref
    )
    assert txn_count == 1


async def test_same_admin_cannot_provide_second_approval_above_threshold(pool, redis, conn):
    admin_id, *_ = await create_test_admin(pool)
    user_id = await create_user(conn)
    destination_id = await _active_destination(conn)
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("2000.00"),
        manual_destination_id=destination_id,
        external_reference="FT26-THRESH-4",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    first_outcome = await admin_queries.approve_manual_deposit_admin(
        pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="first look",
        ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
    )
    assert first_outcome == "awaiting_second_approval"

    with pytest.raises(admin_queries.SameAdminCannotProvideSecondApproval):
        await admin_queries.approve_manual_deposit_admin(
            pool, redis, admin_id=admin_id, payment_id=intent.payment_id, reason="trying again",
            ip_address="10.0.0.1", two_person_threshold=Decimal("2000.00"),
        )

    assert await _cash(conn, user_id) == Decimal("0.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "review"
