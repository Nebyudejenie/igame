"""Real-browser verification of the Simulated Players admin screen
(web/admin/js/screens/simulated_players.js), same discipline as
test_admin_console_e2e.py: an actual Chromium tab against the actual
static files and the real admin API/Postgres/Redis, not a mocked DOM.
"""

from __future__ import annotations

import pytest

from services.admin import simulated_players_queries as spq
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login

pytestmark = pytest.mark.e2e


async def _delete_bot_completely(pool, user_id: int) -> None:
    # Same manual unwind as test_simulated_players.py's own fixture --
    # this file doesn't import that fixture directly since it drives bot
    # creation through the browser itself, but the roster is the same
    # real, hard-capped, shared table, so any bot created here must still
    # be fully cleaned up. Only simulated_players needs deleting (it's
    # what the 10-bot cap actually counts) -- ledger_entries is
    # append-only (migrations/versions/b8e4a1f0c3d7_ledger_append_only.py)
    # and was never load-bearing here. DECISIONS.md, 2026-09-17.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM claim_attempts WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_winners WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM round_entries WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM simulated_players WHERE user_id = $1", user_id)


async def test_simulated_players_console_full_flow_over_a_real_browser(admin_server, pool, browser):
    admin_id, username, password, totp_secret = await create_test_admin(pool, role="superadmin")

    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    # Every mutating action on this screen prompts for a reason via
    # window.prompt() -- auto-accept with a real reason string so each
    # dialog goes through instead of the save silently no-op'ing on a
    # cancelled (null) prompt, same pattern test_admin_console_e2e.py's
    # rooms-edit test already established.
    page.on("dialog", lambda dialog: dialog.accept("e2e test: verifying the console screen"))

    created_user_id: int | None = None
    try:
        await _login(page, admin_server, username, password, totp_secret)
        await page.wait_for_selector(".stat-grid", timeout=10000)

        await page.click('.nav-btn[data-screen="simulated_players"]')
        await page.wait_for_selector("#settings-panel form#settings-form", timeout=10000)
        await page.wait_for_selector("#activity-panel", timeout=10000)
        assert "DISABLED" in await page.text_content("#settings-panel")

        # Create a bot through the real form.
        await page.fill('#create-bot-form input[name="display_name"]', "E2E Console Bot")
        await page.select_option('#create-bot-form select[name="strategy"]', "active")
        await page.click('#create-bot-form button[type="submit"]')
        await page.wait_for_selector("#toast.visible", timeout=5000)
        await page.wait_for_selector('tr:has-text("E2E Console Bot")', timeout=10000)

        created_user_id = await pool.fetchval(
            "SELECT id FROM users WHERE display_name = 'E2E Console Bot' AND is_simulated"
        )
        assert created_user_id is not None
        row_selector = f'tr[data-user-id="{created_user_id}"]'
        assert "SIM" in await page.text_content(row_selector)

        # Start it.
        await page.click(f'{row_selector} .bot-action-btn[data-action="start"]')
        await page.wait_for_function(
            f"document.querySelector('{row_selector} .status-badge')?.textContent.trim() === 'idle'",
            timeout=10000,
        )
        started_status = await pool.fetchval(
            "SELECT status FROM simulated_players WHERE user_id = $1", created_user_id
        )
        assert started_status == "idle"

        # Configure its strategy through the panel.
        await page.click(f'{row_selector} .configure-btn')
        await page.wait_for_selector("#strategy-form", timeout=5000)
        prob_input = page.locator('#strategy-form input[name="join_probability_pct"]')
        await prob_input.fill("")
        await prob_input.fill("42")
        await page.fill('#strategy-form input[name="reason"]', "e2e test: tuning strategy")
        await page.click('#strategy-form button[type="submit"]')
        await page.wait_for_selector("#toast.visible", timeout=5000)
        await page.wait_for_function(
            f"document.querySelector('{row_selector}')?.textContent.includes('42%')", timeout=10000
        )
        saved_pct = await pool.fetchval(
            "SELECT join_probability_pct FROM simulated_players WHERE user_id = $1", created_user_id
        )
        assert saved_pct == 42

        # Reset -- forces it back to disabled with the balance restored.
        await page.click(f'{row_selector} .bot-action-btn[data-action="reset"]')
        await page.wait_for_function(
            f"document.querySelector('{row_selector} .status-badge')?.textContent.trim() === 'disabled'",
            timeout=10000,
        )
        reset_row = await pool.fetchrow(
            "SELECT status FROM simulated_players WHERE user_id = $1", created_user_id
        )
        assert reset_row["status"] == "disabled"

        # Start again then Stop, to prove the stop button path independently
        # of reset's own (different) transition.
        await page.click(f'{row_selector} .bot-action-btn[data-action="start"]')
        await page.wait_for_function(
            f"document.querySelector('{row_selector} .status-badge')?.textContent.trim() === 'idle'",
            timeout=10000,
        )
        await page.click(f'{row_selector} .bot-action-btn[data-action="stop"]')
        await page.wait_for_function(
            f"document.querySelector('{row_selector} .status-badge')?.textContent.trim() === 'disabled'",
            timeout=10000,
        )
        stopped_status = await pool.fetchval(
            "SELECT status FROM simulated_players WHERE user_id = $1", created_user_id
        )
        assert stopped_status == "disabled"

        # Enable globally, then use the console's own Stop All confirmation
        # flow -- wrong confirmation text must be rejected before the real one.
        await spq.update_settings_admin(
            pool, admin_id=admin_id, enabled=True, max_concurrent_bots=10,
            reason="e2e setup", ip_address=None,
        )
        await page.reload()
        await page.wait_for_selector(".stat-grid", timeout=10000)
        await page.click('.nav-btn[data-screen="simulated_players"]')
        await page.wait_for_selector("#stop-all-panel form#stop-all-form", timeout=10000)
        assert "ENABLED" in await page.text_content("#settings-panel")

        await page.fill('#stop-all-form input[name="reason"]', "e2e test: stop-all flow")
        await page.fill('#stop-all-form input[name="confirmation"]', "nope")
        await page.click('#stop-all-form button[type="submit"]')
        toast_text = await page.text_content("#toast")
        assert toast_text and "STOP ALL" in toast_text.upper()

        await page.fill('#stop-all-form input[name="confirmation"]', "")
        await page.fill('#stop-all-form input[name="confirmation"]', "STOP ALL")
        await page.click('#stop-all-form button[type="submit"]')
        await page.wait_for_function(
            "document.getElementById('settings-panel')?.textContent.includes('DISABLED')",
            timeout=10000,
        )
        settings_after = await spq.get_settings_admin(pool)
        assert settings_after["enabled"] is False

        assert page_errors == [], f"JS errors during simulated-players console flow: {page_errors}"
    finally:
        await page.close()
        if created_user_id is not None:
            await _delete_bot_completely(pool, created_user_id)
        # Belt-and-suspenders: make sure this test never leaves the global
        # switch enabled for whatever runs against this shared database next.
        await pool.execute("UPDATE simulated_players_settings SET enabled = false WHERE id = 1")
