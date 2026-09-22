"""Real-browser verification of the manual-payment admin screens (`web/
admin/js/screens/manual_deposits.js`, `manual_withdrawals.js`,
`payment_destinations.js`, `provider_availability.js`) -- the backend
logic these screens drive is already thoroughly covered by
test_payments_manual_deposits.py/test_payments_manual_withdrawals.py/
test_admin_manual_payments.py/test_payment_availability.py; this file's
job is proving the frontend wiring itself actually works end to end in a
real Chromium tab, matching test_admin_console_e2e.py's own established
discipline for every other admin screen.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from packages.core import ledger
from services.payments import manual
from tests.integration.conftest import create_funded_user, create_user
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login
from tests.integration.test_payments_manual_withdrawals import _manual_withdrawal

pytestmark = pytest.mark.e2e

MIN_DEPOSIT = Decimal("10.00")
DAILY_CAP = Decimal("50000.00")


async def _cash(conn, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def test_superadmin_creates_a_manual_payment_destination_over_a_real_browser(
    admin_server, pool, conn, browser
):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept("e2e test: onboarding a new receiving account"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="payment_destinations"]')
    await page.wait_for_selector("#create-destination-form", timeout=10000)

    await page.select_option('select[name="method_kind"]', "cbe_birr")
    await page.fill('input[name="account_ref"]', "1000998877665")
    await page.fill('input[name="account_name"]', "Zemen Game PLC")
    await page.fill('input[name="instructions"]', "Include your player id in the memo")
    await page.click('#create-destination-form button[type="submit"]')

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_selector('td:has-text("1000998877665")', timeout=5000)

    # ORDER BY id DESC -- account_ref isn't unique at the schema level, and
    # this same literal value has been reused across many runs of this
    # test over this shared, never-truncated dev database's history; an
    # unscoped fetchrow() can return a stale row from an earlier run
    # instead of the one this run just created.
    row = await conn.fetchrow(
        "SELECT account_name, is_active FROM manual_payment_destinations WHERE account_ref = $1 "
        "ORDER BY id DESC LIMIT 1",
        "1000998877665",
    )
    assert row is not None
    assert row["account_name"] == "Zemen Game PLC"
    assert row["is_active"] is True

    assert page_errors == [], f"JS errors during destination-create flow: {page_errors}"
    await page.close()


async def test_superadmin_toggles_provider_availability_over_a_real_browser(admin_server, pool, conn, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    before = await conn.fetchval(
        "SELECT enabled FROM payment_provider_availability WHERE provider = 'santimpay' AND direction = 'in'"
    )
    assert before is False

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept("e2e test: santimpay approved for pilot"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="provider_availability"]')
    await page.wait_for_selector('input[data-provider="santimpay"][data-direction="in"]', timeout=10000)
    await page.check('input[data-provider="santimpay"][data-direction="in"]')

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(
        "document.querySelector('input[data-provider=\"santimpay\"][data-direction=\"in\"]').checked === true"
    )

    after = await conn.fetchval(
        "SELECT enabled FROM payment_provider_availability WHERE provider = 'santimpay' AND direction = 'in'"
    )
    assert after is True

    # Restore -- this table is shared, session-wide state.
    await conn.execute(
        "UPDATE payment_provider_availability SET enabled = false WHERE provider = 'santimpay' AND direction = 'in'"
    )
    await page.close()


async def test_finance_approves_a_manual_deposit_over_a_real_browser(admin_server, pool, redis, conn, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_user(conn)
    destination_row = await conn.fetchrow(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name)
        VALUES ('telebirr', '0911000000', 'Zemen Game PLC') RETURNING id
        """
    )
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("250.00"),
        manual_destination_id=destination_row["id"],
        external_reference="FT-E2E-DEP-1",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept("e2e test: matched bank statement line"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="manual_deposits"]')
    payment_row = f'tr[data-payment-id="{intent.payment_id}"]'
    await page.wait_for_selector(payment_row, timeout=10000)
    await page.click(f"{payment_row} .approve-btn")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(f"!document.querySelector('{payment_row}')", timeout=5000)

    assert await _cash(conn, user_id) == Decimal("250.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"

    assert page_errors == [], f"JS errors during manual-deposit-approve flow: {page_errors}"
    await page.close()


async def test_two_different_finance_admins_approve_a_high_value_manual_deposit_over_real_browsers(
    admin_server, pool, redis, conn, browser
):
    # The two-person-approval feature's own e2e capstone: a single admin
    # session clicking Approve twice must never be enough to credit a
    # >= 2,000 ETB manual deposit -- it takes a second, genuinely
    # different admin's browser session and login.
    _, first_username, first_password, first_totp = await create_test_admin(pool, role="finance")
    _, second_username, second_password, second_totp = await create_test_admin(pool, role="finance")
    user_id = await create_user(conn)
    destination_row = await conn.fetchrow(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name)
        VALUES ('telebirr', '0911000000', 'Zemen Game PLC') RETURNING id
        """
    )
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("2500.00"),
        manual_destination_id=destination_row["id"],
        external_reference="FT-E2E-2PERSON-1",
        receipt_telegram_file_id=None,
        min_deposit=MIN_DEPOSIT,
        daily_cap=DAILY_CAP,
    )
    payment_row = f'tr[data-payment-id="{intent.payment_id}"]'

    first_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    first_page.on("dialog", lambda dialog: dialog.accept("e2e test: first look, matches bank statement"))
    await _login(first_page, admin_server, first_username, first_password, first_totp)
    await first_page.wait_for_selector(".stat-grid", timeout=10000)
    await first_page.click('.nav-btn[data-screen="manual_deposits"]')
    await first_page.wait_for_selector(payment_row, timeout=10000)
    await first_page.click(f"{payment_row} .approve-btn")
    await first_page.wait_for_selector("#toast.visible", timeout=5000)
    await first_page.wait_for_selector(f'{payment_row}:has-text("awaiting 2nd approval")', timeout=5000)
    await first_page.close()

    status_after_first = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status_after_first == "review"  # not yet credited
    assert await _cash(conn, user_id) == Decimal("0.00")

    second_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    second_page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    second_page.on("dialog", lambda dialog: dialog.accept("e2e test: confirmed externally by a second reviewer"))
    await _login(second_page, admin_server, second_username, second_password, second_totp)
    await second_page.wait_for_selector(".stat-grid", timeout=10000)
    await second_page.click('.nav-btn[data-screen="manual_deposits"]')
    await second_page.wait_for_selector(payment_row, timeout=10000)
    await second_page.click(f"{payment_row} .approve-btn")
    await second_page.wait_for_selector("#toast.visible", timeout=5000)
    await second_page.wait_for_function(f"!document.querySelector('{payment_row}')", timeout=5000)

    assert await _cash(conn, user_id) == Decimal("2500.00")
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", intent.payment_id)
    assert status == "succeeded"

    assert page_errors == [], f"JS errors during two-person manual-deposit-approve flow: {page_errors}"
    await second_page.close()


async def test_finance_approves_and_settles_a_manual_withdrawal_over_a_real_browser(
    admin_server, pool, redis, conn, browser
):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _manual_withdrawal(pool, redis, user_id, Decimal("120.00"))

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    dialog_texts = iter(["e2e test: verified identity", "TXN-E2E-REAL-1", "e2e test: sent via Telebirr"])

    async def handle_dialog(dialog):
        await dialog.accept(next(dialog_texts))

    page.on("dialog", handle_dialog)

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="manual_withdrawals"]')
    pending_row = f'#manual-withdrawals-pending tr[data-payment-id="{payment_id}"]'
    await page.wait_for_selector(pending_row, timeout=10000)
    await page.click(f"{pending_row} .approve-btn")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    settlement_row = f'#manual-withdrawals-settlement tr[data-payment-id="{payment_id}"]'
    await page.wait_for_selector(settlement_row, timeout=10000)
    await page.click(f"{settlement_row} .settle-btn")

    await page.wait_for_function(f"!document.querySelector('{settlement_row}')", timeout=5000)

    row = await conn.fetchrow("SELECT status, provider_ref FROM payments WHERE id = $1", payment_id)
    assert row["status"] == "succeeded"
    assert row["provider_ref"] == "TXN-E2E-REAL-1"

    locked = await ledger.get_or_create_account(conn, user_id, "user_locked")
    assert await ledger.balance(conn, locked.id) == Decimal("0.00")

    assert page_errors == [], f"JS errors during manual-withdrawal approve+settle flow: {page_errors}"
    await page.close()
