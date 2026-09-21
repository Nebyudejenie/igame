"""Real-browser verification of the admin console frontend (`web/admin/`,
mounted at `/console` by `services/admin/app.py`) -- not a mock DOM, an
actual Chromium tab loading the actual static files, talking to the real
admin API, real Postgres, real Redis. Same discipline
test_miniapp_e2e.py already established for the player-facing frontend:
"start the dev server and use the feature in a browser" means an actual
browser here, not just the API-level tests in test_admin_app.py/test_
admin_queries.py.

This file didn't exist when web/admin/ shipped -- the only verification
at the time was a one-off Playwright script run by hand, never committed,
giving zero regression protection going forward. This is that missing
permanent coverage.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pyotp
import pytest

from packages.core import ledger
from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_withdrawals import _review_withdrawal

pytestmark = pytest.mark.e2e


async def _login(page, admin_server: str, username: str, password: str, totp_secret: str) -> None:
    await page.goto(admin_server + "/console/")
    await page.wait_for_selector("#login-screen form")
    await page.fill('input[name="username"]', username)
    await page.fill('input[name="password"]', password)
    await page.fill('input[name="totp_code"]', pyotp.TOTP(totp_secret).now())
    await page.click('button[type="submit"]')


async def test_admin_console_login_and_dashboard_load(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)

    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await page.wait_for_selector(".stat-grid", timeout=10000)
    # The login card must actually be gone, not just covered -- this is
    # exactly the real bug a first real-browser pass caught before this
    # file existed (a CSS specificity issue left #login-screen visible
    # underneath #app-shell after a successful login; see DECISIONS.md).
    assert await page.is_hidden("#login-screen")

    assert page_errors == [], f"JS errors on load: {page_errors}"
    await page.close()


async def test_admin_console_dashboard_shows_real_system_health_badges(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector("#system-health-row .badge", timeout=10000)

    badge_text = await page.text_content("#system-health-row")
    # Database and Redis are real, live connections in this test
    # environment -- both must show green. Telegram has no real bot
    # token configured here, so "unknown" (not a false green/red) is the
    # only honest state this environment can produce.
    assert "Database" in badge_text
    assert "Redis" in badge_text
    assert "Telegram" in badge_text
    assert "Bingo" in badge_text

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_kyc_action_changes_real_database_state(admin_server, pool, conn, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    row = await conn.fetchrow("SELECT display_name, kyc_level FROM users WHERE id = $1", user_id)
    assert row["kyc_level"] == 0

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="users"]')
    await page.fill("#user-search-input", row["display_name"])
    await page.click('#user-search-form button[type="submit"]')
    await page.click(f'.clickable-row[data-user-id="{user_id}"]')
    await page.wait_for_selector("#kyc-select", timeout=10000)

    await page.select_option("#kyc-select", "2")
    await page.fill("#kyc-reason", "e2e test: ID documents reviewed and verified")
    await page.click("#kyc-submit")

    # The toast is the UI-level confirmation; the database row is the
    # real one -- both must agree, not just the friendlier of the two.
    # users.js's loadDetail() briefly replaces the whole detail panel
    # with a loading placeholder before rebuilding it with fresh values,
    # so this predicate has to tolerate #kyc-select not existing for a
    # moment -- optional chaining, not an assumption it's always there.
    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(
        "document.getElementById('kyc-select')?.value === '2'", timeout=5000
    )
    updated = await conn.fetchval("SELECT kyc_level FROM users WHERE id = $1", user_id)
    assert updated == 2

    await page.close()


async def test_admin_console_adjust_balance_credits_a_real_user_over_a_real_browser(
    admin_server, pool, conn, browser
):
    # Real-browser proof for the users.js adjust-balance form specifically
    # (no prior e2e coverage existed for it at all): that crypto.randomUUID()
    # is genuinely available and the request body it builds actually
    # satisfies the backend's now-required request_id field, not just that
    # the idempotency mechanism is correct in isolation (already proven at
    # the API level in test_admin_app.py/test_admin_queries.py -- this test's
    # job is the actual browser wiring, not re-proving that property).
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    user_id = await create_funded_user(conn, Decimal("10.00"))
    row = await conn.fetchrow("SELECT display_name FROM users WHERE id = $1", user_id)

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="users"]')
    await page.fill("#user-search-input", row["display_name"])
    await page.click('#user-search-form button[type="submit"]')
    await page.click(f'.clickable-row[data-user-id="{user_id}"]')
    await page.wait_for_selector("#adjust-submit", timeout=10000)

    await page.fill("#adjust-amount", "15.00")
    await page.fill("#adjust-reason", "e2e test: goodwill credit")
    await page.click("#adjust-submit")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    # .field-value is shared by several fields (KYC level, Cash, Bonus,
    # Locked, Net LTV, Last seen) with no distinguishing id/data attribute
    # -- find the one whose sibling .field-label actually reads "Cash"
    # rather than assuming DOM order.
    await page.wait_for_function(
        """() => {
            const label = Array.from(document.querySelectorAll('.field-label'))
                .find(el => el.textContent === 'Cash');
            return label?.nextElementSibling?.textContent.includes('25.00');
        }""",
        timeout=5000,
    )
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    assert await ledger.balance(conn, cash.id) == Decimal("25.00")

    assert page_errors == [], f"JS errors during adjust-balance flow: {page_errors}"
    await page.close()


async def test_admin_console_rooms_edit_changes_a_real_room_over_a_real_browser(
    admin_server, pool, conn, browser
):
    # An architecture audit found the rooms screen had create + activate/
    # deactivate but no edit UI at all, despite update_room_admin()/
    # _UPDATABLE_ROOM_FIELDS fully supporting it (spec 11: "create/edit
    # stakes, cut, timings, patterns") -- this is the first real-browser
    # coverage of the new edit form, including the window.prompt() reason
    # dialog neither this action nor the pre-existing activate/deactivate
    # button had any Playwright coverage for before.
    from tests.integration.conftest import create_room

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="ops")
    room_id = await create_room(conn, stake=Decimal("10.00"), house_cut_bps=2000)

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    # window.prompt() is auto-dismissed (returns null) with no handler --
    # this is what actually lets the edit form's reason prompt go through
    # rather than the save silently no-op'ing on a cancelled dialog.
    page.on("dialog", lambda dialog: dialog.accept("e2e test: correcting the stake"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="rooms"]')
    room_row = f'tr[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_row, timeout=10000)
    await page.click(f'{room_row} .edit-room-btn')

    await page.wait_for_selector("#edit-room-form", timeout=5000)
    stake_input = page.locator('#edit-room-form input[name="stake"]')
    await stake_input.fill("")
    await stake_input.fill("25.00")
    await page.click('#edit-room-form button[type="submit"]')

    await page.wait_for_selector("#toast.visible", timeout=5000)
    await page.wait_for_function(
        f"""() => {{
            const row = document.querySelector('tr[data-room-id="{room_id}"]');
            return row && row.textContent.includes('25.00');
        }}""",
        timeout=5000,
    )

    updated_stake = await conn.fetchval("SELECT stake FROM rooms WHERE id = $1", room_id)
    assert updated_stake == Decimal("25.00")

    audit_row = await conn.fetchrow(
        "SELECT reason FROM admin_audit_log WHERE admin_id = $1 AND action = 'rooms.update' "
        "ORDER BY id DESC LIMIT 1",
        admin_id,
    )
    assert audit_row is not None
    assert "correcting the stake" in audit_row["reason"]

    assert page_errors == [], f"JS errors during rooms-edit flow: {page_errors}"
    await page.close()


async def test_admin_console_rbac_denial_shows_a_real_message_not_a_blank_screen(
    admin_server, pool, redis, conn, browser
):
    # support has payments:view (sees the screen, the list loads) but not
    # payments:approve -- an *action*-level denial, not a view-level one:
    # the nav-hiding fix below (services/admin/rbac.py's own risk:view
    # gap this test used to exercise) now hides that screen from support
    # entirely, so a real, still-reachable-via-the-actual-nav denial is
    # needed to keep proving the same underlying property this test has
    # always been about -- a real backend 403 renders as a real message,
    # not a blank screen or a silently-succeeded action. js/screens/
    # payments.js's decide() catches its own error and toasts the real
    # backend detail directly.
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="support")
    user_id = await create_funded_user(conn, Decimal("500.00"))
    payment_id, our_ref = await _review_withdrawal(pool, redis, conn, user_id, Decimal("100.00"))

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept("e2e test: attempting approval"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="payments"]')
    payment_row = f'tr[data-payment-id="{payment_id}"]'
    await page.wait_for_selector(payment_row, timeout=10000)
    await page.click(f"{payment_row} .approve-btn")

    await page.wait_for_selector("#toast.visible", timeout=5000)
    toast_text = await page.text_content("#toast")
    assert toast_text and "payments:approve" in toast_text

    # The denial has to be real, not just a UI-level message with the
    # action secretly going through anyway.
    status = await conn.fetchval("SELECT status FROM payments WHERE id = $1", payment_id)
    assert status == "review"

    await page.close()


async def test_admin_console_nav_hides_screens_the_current_role_cant_view(admin_server, pool, browser):
    # An architecture audit caught every screen shown to every role
    # regardless -- a support admin saw "Reports"/"Risk"/"Audit" in the
    # nav despite services/admin/rbac.py granting none of those *:view
    # permissions to that role, discovering the denial only after
    # clicking. Confirms both directions: hidden for the role that
    # genuinely lacks them, and still present for superadmin (who has
    # every permission) -- so this is proven to be real filtering, not a
    # nav that's just broken/empty for everyone.
    support_id, support_user, support_pw, support_totp = await create_test_admin(pool, role="support")
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    support_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(support_page, admin_server, support_user, support_pw, support_totp)
    await support_page.wait_for_selector(".stat-grid", timeout=10000)
    for hidden_screen in ("reports", "risk", "audit"):
        assert await support_page.query_selector(f'.nav-btn[data-screen="{hidden_screen}"]') is None
    for visible_screen in ("dashboard", "users", "payments", "rounds", "rooms"):
        assert await support_page.query_selector(f'.nav-btn[data-screen="{visible_screen}"]') is not None
    await support_page.close()

    superadmin_page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(superadmin_page, admin_server, username, password, totp_secret)
    await superadmin_page.wait_for_selector(".stat-grid", timeout=10000)
    for screen in ("dashboard", "users", "payments", "rounds", "rooms", "reports", "risk", "audit"):
        assert await superadmin_page.query_selector(f'.nav-btn[data-screen="{screen}"]') is not None
    await superadmin_page.close()


async def test_admin_console_logout_returns_to_the_login_screen(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click("#logout-btn")
    await page.wait_for_selector("#login-screen form", timeout=10000)
    assert await page.is_hidden("#app-shell")

    await page.close()


async def test_admin_console_telebirr_evidence_view_raw_sms_and_resolve(admin_server, pool, browser):
    from services.payments.telebirr_ingest import ingest_sms_evidence

    reference = f"DI{random.randint(8 * 10**7, 9 * 10**7):08d}"
    recipient = f"ConsoleTest{reference}"
    raw_sms = (
        f"Dear {recipient} \n"
        "You have received ETB 20.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )
    outcome = await ingest_sms_evidence(pool, raw_sms=raw_sms, source="macrodroid", source_ref="console-e2e")
    assert outcome.status == "ingested_rejected"

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="finance")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)
    await page.click('.nav-btn[data-screen="telebirr_evidence"]')

    row_selector = f'tr[data-evidence-id="{outcome.evidence_id}"]'
    await page.wait_for_selector(row_selector, timeout=10000)
    assert reference in await page.text_content(row_selector)

    await page.click(f'{row_selector} .view-raw-btn')
    await page.wait_for_selector("pre.code-block", timeout=10000)
    assert reference in await page.text_content("pre.code-block")

    page.once("dialog", lambda dialog: dialog.accept("verified manually via e2e test"))
    await page.click(f'{row_selector} .resolve-btn[data-to="available"]')
    # Optional chaining: the list is torn down and rebuilt from a fresh
    # fetch after a successful resolve (loadFirstPage()), so the row
    # briefly doesn't exist at all during that reload -- Playwright's
    # wait_for_function treats a thrown exception as fatal rather than
    # "not yet true, keep polling", so a bare .textContent here would
    # intermittently fail the whole test on that transient null.
    await page.wait_for_function(
        f"document.querySelector('{row_selector} .badge')?.textContent.trim().toLowerCase() === 'available'",
        timeout=10000,
    )

    row = await pool.fetchrow("SELECT status FROM payment_evidence WHERE id = $1", outcome.evidence_id)
    assert row["status"] == "available"

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_payment_agent_create_and_deactivate_over_a_real_browser(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    telegram_user_id = 700_000_000 + admin_id

    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)
    await page.click('.nav-btn[data-screen="payment_agents"]')
    await page.wait_for_selector("#create-agent-form", timeout=10000)

    await page.fill('input[name="telegram_user_id"]', str(telegram_user_id))
    await page.fill('input[name="display_name"]', "E2E Field Agent")
    await page.click('#create-agent-form button[type="submit"]')

    # :has-text() is Playwright's own selector engine, valid for
    # page.wait_for_selector() (unlike the plain-JS string a
    # wait_for_function() predicate evaluates natively) -- waits for the
    # real async POST + reload to actually land before the DB is checked.
    await page.wait_for_selector(f'tr:has-text("{telegram_user_id}")', timeout=10000)

    agent_row = await pool.fetchrow(
        "SELECT id, is_active, display_name FROM payment_agents WHERE telegram_user_id = $1", telegram_user_id
    )
    assert agent_row is not None
    assert agent_row["is_active"] is True
    assert agent_row["display_name"] == "E2E Field Agent"

    row_selector = f'tr[data-agent-id="{agent_row["id"]}"]'

    await page.click(f'{row_selector} .toggle-active-btn')
    # Optional chaining: same transient-null-during-reload reasoning as
    # the telebirr evidence test above.
    await page.wait_for_function(
        f"document.querySelector('{row_selector} .toggle-active-btn')?.textContent.trim() === 'Activate'",
        timeout=10000,
    )

    deactivated = await pool.fetchval("SELECT is_active FROM payment_agents WHERE id = $1", agent_row["id"])
    assert deactivated is False

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_telegram_health_screen_shows_live_webhook_status(
    admin_server, pool, browser, monkeypatch
):
    from aiogram import Bot
    from aiogram.types import WebhookInfo

    from packages.core.config import get_settings

    # Same reasoning as test_telegram_diagnostics_admin.py: conftest.py's
    # shared TELEGRAM_BOT_TOKEN default doesn't match Telegram's real
    # token shape, so aiogram.Bot's own construction-time validation
    # would reject it before ever reaching get_webhook_info() -- a
    # well-formed fake token plus a patched get_webhook_info() avoids any
    # real network call while still exercising the genuine code path.
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:FAKE-TEST-TOKEN")

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return WebhookInfo(
            url="https://bot.test/webhook",
            has_custom_certificate=False,
            pending_update_count=7,
            ip_address="203.0.113.1",
            last_error_date=None,
            last_error_message=None,
            last_synchronization_error_date=None,
            max_connections=40,
            allowed_updates=None,
        )

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="ops")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="telegram_health"]')
    await page.wait_for_selector(".badge-healthy", timeout=10000)

    stat_values = await page.eval_on_selector_all(".stat-value", "els => els.map(e => e.textContent)")
    assert any("7" in v for v in stat_values), stat_values
    assert "https://bot.test/webhook" in await page.text_content(".stat-grid")

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_telegram_commands_screen_edit_and_disable_over_a_real_browser(
    admin_server, pool, browser, monkeypatch
):
    from aiogram import Bot
    from aiogram.types import WebhookInfo

    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123456:FAKE-TEST-TOKEN")

    async def fake_get_webhook_info(self: Bot) -> WebhookInfo:
        return WebhookInfo(
            url="https://bot.test/webhook", has_custom_certificate=False, pending_update_count=0,
            ip_address=None, last_error_date=None, last_error_message=None,
            last_synchronization_error_date=None, max_connections=40, allowed_updates=None,
        )

    monkeypatch.setattr(Bot, "get_webhook_info", fake_get_webhook_info)

    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("dialog", lambda dialog: dialog.accept("e2e test: temporary maintenance"))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="telegram_health"]')
    await page.wait_for_selector('tr[data-handler="cmd_rules"]', timeout=10000)

    # Open the /rules row's detail panel and edit its description.
    await page.click('tr[data-handler="cmd_rules"] .details-btn')
    await page.wait_for_selector('tr[data-detail-for="cmd_rules"] .edit-form', timeout=5000)
    description_input = page.locator('tr[data-detail-for="cmd_rules"] input[name="description"]')
    await description_input.fill("")
    await description_input.fill("Edited via e2e test")
    await page.click('tr[data-detail-for="cmd_rules"] button[type="submit"]')
    await page.wait_for_selector("#toast.visible", timeout=5000)

    updated_description = await pool.fetchval(
        "SELECT description FROM bot_commands WHERE handler_name = 'cmd_rules'"
    )
    assert updated_description == "Edited via e2e test"

    # A successful save reloads the whole table (renderCommands()), which
    # collapses whatever detail row was open -- re-open it before
    # continuing.
    await page.wait_for_selector('tr[data-handler="cmd_rules"] .details-btn', timeout=5000)
    await page.click('tr[data-handler="cmd_rules"] .details-btn')

    # Load a real preview -- proves the backend round trip renders through
    # a real browser click, not just a direct API call.
    await page.wait_for_selector('tr[data-detail-for="cmd_rules"] .preview-btn', timeout=5000)
    await page.click('tr[data-detail-for="cmd_rules"] .preview-btn')
    await page.wait_for_selector('tr[data-detail-for="cmd_rules"] .preview-result .field-value', timeout=5000)
    preview_text = await page.text_content('tr[data-detail-for="cmd_rules"] .preview-result .field-value')
    assert preview_text and len(preview_text) > 0

    # Disable /balance (window.prompt auto-accepted above with a reason)
    # and confirm the badge flips to "disabled" in the live UI.
    await page.click('tr[data-handler="cmd_balance"] .details-btn')
    await page.wait_for_selector('tr[data-detail-for="cmd_balance"] .toggle-enabled-btn', timeout=5000)
    await page.click('tr[data-detail-for="cmd_balance"] .toggle-enabled-btn')
    await page.wait_for_function(
        """() => {
            const row = document.querySelector('tr[data-handler="cmd_balance"]');
            return row && row.textContent.includes('disabled');
        }""",
        timeout=5000,
    )

    disabled_in_db = await pool.fetchval(
        "SELECT enabled FROM bot_commands WHERE handler_name = 'cmd_balance'"
    )
    assert disabled_in_db is False
    # Re-enable before continuing -- the next step edits a different
    # command (cmd_support) and doesn't need cmd_balance disabled anymore.
    await pool.execute("UPDATE bot_commands SET enabled = true WHERE handler_name = 'cmd_balance'")

    # Phase 3: set a real cooldown + rate limit on a third command through
    # the same edit form, and confirm the saved values round-trip back
    # into the reopened detail panel's own input fields.
    await page.click('tr[data-handler="cmd_support"] .details-btn')
    await page.wait_for_selector('tr[data-detail-for="cmd_support"] input[name="cooldown_seconds"]', timeout=5000)
    cooldown_input = page.locator('tr[data-detail-for="cmd_support"] input[name="cooldown_seconds"]')
    await cooldown_input.fill("15")
    rate_limit_input = page.locator('tr[data-detail-for="cmd_support"] input[name="rate_limit_per_minute"]')
    await rate_limit_input.fill("4")
    await page.click('tr[data-detail-for="cmd_support"] button[type="submit"]')
    await page.wait_for_selector("#toast.visible", timeout=5000)

    saved = await pool.fetchrow(
        "SELECT cooldown_seconds, rate_limit_per_minute FROM bot_commands WHERE handler_name = 'cmd_support'"
    )
    assert saved["cooldown_seconds"] == 15
    assert saved["rate_limit_per_minute"] == 4

    await page.wait_for_selector('tr[data-handler="cmd_support"] .details-btn', timeout=5000)
    await page.click('tr[data-handler="cmd_support"] .details-btn')
    await page.wait_for_selector('tr[data-detail-for="cmd_support"] input[name="cooldown_seconds"]', timeout=5000)
    reopened_cooldown = await page.input_value('tr[data-detail-for="cmd_support"] input[name="cooldown_seconds"]')
    reopened_rate_limit = await page.input_value('tr[data-detail-for="cmd_support"] input[name="rate_limit_per_minute"]')
    assert reopened_cooldown == "15"
    assert reopened_rate_limit == "4"

    # Restore real, shared state for every other test/process using this
    # database.
    await pool.execute(
        "UPDATE bot_commands SET enabled = true, description = 'Static Bingo rules text' "
        "WHERE handler_name IN ('cmd_balance', 'cmd_rules')"
    )
    await pool.execute(
        "UPDATE bot_commands SET cooldown_seconds = 0, rate_limit_per_minute = NULL "
        "WHERE handler_name = 'cmd_support'"
    )

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_global_search_via_command_palette_over_a_real_browser(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    # Ctrl+K opens the overlay (Meta+K is the Mac equivalent -- Control
    # is what a real Linux/Windows Chromium session under test actually
    # sends either way).
    await page.keyboard.press("Control+k")
    await page.wait_for_selector("#global-search-overlay:not([hidden])", timeout=5000)

    await page.fill("#global-search-input", "cmd_balance")
    await page.wait_for_selector(".search-result-row", timeout=5000)
    result_text = await page.text_content("#global-search-results")
    assert "/balance" in result_text or "cmd_balance" in result_text

    # Escape closes it without navigating anywhere.
    await page.keyboard.press("Escape")
    # state="attached", not the default "visible" -- an element carrying
    # the hidden attribute is by definition never "visible", so waiting
    # for the default state here would be a self-contradictory wait that
    # can never resolve; wait_for_selector's own default doesn't fit an
    # assertion about disappearing, only appearing.
    await page.wait_for_selector("#global-search-overlay[hidden]", state="attached", timeout=5000)

    # Reopen and click through to the Telegram screen.
    await page.keyboard.press("Control+k")
    await page.fill("#global-search-input", "cmd_balance")
    await page.wait_for_selector(".search-result-row", timeout=5000)
    await page.click(".search-result-row")
    await page.wait_for_selector('tr[data-handler="cmd_balance"]', timeout=10000)

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()


async def test_admin_console_announcement_screen_saves_and_shows_current_state(
    admin_server, pool, conn, browser
):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="ops")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    try:
        await _login(page, admin_server, username, password, totp_secret)
        await page.wait_for_selector(".stat-grid", timeout=10000)

        await page.click('.nav-btn[data-screen="announcement"]')
        await page.wait_for_selector("#announcement-form", timeout=10000)
        assert "DISABLED" in await page.text_content("#announcement-panel")

        await page.fill('#announcement-form input[name="text"]', "Weekend tournament — join now!")
        await page.check('#announcement-form input[name="enabled"]')
        await page.fill('#announcement-form input[name="reason"]', "e2e test: launch promo")
        await page.click('#announcement-form button[type="submit"]')

        await page.wait_for_selector("#toast.visible", timeout=5000)
        await page.wait_for_function(
            "document.getElementById('announcement-panel')?.textContent.includes('ENABLED')",
            timeout=10000,
        )

        row = await conn.fetchrow("SELECT text, enabled FROM platform_announcement WHERE id = 1")
        assert row["text"] == "Weekend tournament — join now!"
        assert row["enabled"] is True

        audit_row = await conn.fetchrow(
            "SELECT reason FROM admin_audit_log WHERE action = 'announcement.update' "
            "ORDER BY id DESC LIMIT 1"
        )
        assert audit_row is not None
        assert "launch promo" in audit_row["reason"]

        assert page_errors == [], f"JS errors: {page_errors}"
    finally:
        await pool.execute("UPDATE platform_announcement SET text = '', enabled = false WHERE id = 1")
        await page.close()


async def test_admin_console_refresh_stays_on_the_current_screen(admin_server, pool, browser):
    # Previously showApp() unconditionally called showScreen("dashboard")
    # on every load with no URL state at all, so a refresh always bounced
    # an admin mid-task on some other screen back to the dashboard.
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    await _login(page, admin_server, username, password, totp_secret)
    await page.wait_for_selector(".stat-grid", timeout=10000)

    await page.click('.nav-btn[data-screen="rooms"]')
    await page.wait_for_selector('.nav-btn[data-screen="rooms"].active', timeout=10000)
    assert page.url.endswith("#rooms")

    await page.reload()
    await page.wait_for_selector('.nav-btn[data-screen="rooms"].active', timeout=10000)
    # Landed back on Rooms (its create-room form), not the Dashboard's own
    # stat grid.
    await page.wait_for_selector("#create-room-form", timeout=10000)

    assert page_errors == [], f"JS errors: {page_errors}"
    await page.close()
