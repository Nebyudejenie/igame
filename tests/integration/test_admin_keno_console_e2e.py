"""Real-browser verification of the admin console's Keno screen
(web/admin/js/screens/keno.js and keno/*.js) against the real admin API,
Postgres and Redis -- the same discipline as test_admin_console_e2e.py."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.integration.conftest import create_funded_user
from tests.integration.test_admin_auth import create_test_admin
from tests.integration.test_admin_console_e2e import _login

pytestmark = pytest.mark.e2e

SECTIONS = ["overview", "rounds", "paytables", "tiers", "simulator", "reports"]


async def _seed_completed_round(conn) -> int:
    config_id = await conn.fetchval("SELECT id FROM keno_configs ORDER BY id DESC LIMIT 1")
    tier_id = await conn.fetchval("SELECT id FROM keno_risk_tiers ORDER BY id DESC LIMIT 1")
    paytable_id = await conn.fetchval("SELECT id FROM keno_paytables ORDER BY id DESC LIMIT 1")
    round_id = await conn.fetchval(
        "INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed, server_seed_hash, drawn_numbers) "
        "VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'completed', $1, $2, $3, 'h', $4) RETURNING id",
        config_id, tier_id, b"seed", [3, 17, 42],
    )
    user_id = await create_funded_user(conn, Decimal("0"))
    await conn.execute(
        "INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, status, payout, idempotency_key) "
        "VALUES ($1, $2, $3, 1, 10, 'lost', 0, $4)",
        round_id, user_id, paytable_id, f"test-e2e-keno-admin-{uuid.uuid4()}",
    )
    return round_id


async def _open_keno(page, section: str) -> None:
    await page.click('.nav-btn[data-screen="keno"]')
    await page.wait_for_selector("#keno-subnav .subnav-btn", timeout=10000)
    await page.click(f'#keno-subnav [data-section="{section}"]')
    await page.wait_for_selector(f'#keno-subnav [data-section="{section}"].active', timeout=10000)
    await page.wait_for_function("!document.querySelector('#keno-section .loading')", timeout=15000)


async def test_every_keno_section_renders_without_errors_for_superadmin(admin_server, pool, conn, browser):
    round_id = await _seed_completed_round(conn)
    _, username, password, totp = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await _login(page, admin_server, username, password, totp)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)

    for section in SECTIONS:
        await _open_keno(page, section)
        assert await page.locator("#keno-section .error-banner").count() == 0, f"{section} showed an error banner"
        await page.screenshot(path=f"/tmp/admin-keno-{section}.png", full_page=True)

    await _open_keno(page, "rounds")
    await page.click(f'#round-list tr[data-id="{round_id}"]')
    await page.wait_for_selector("#round-detail .number-chip", timeout=10000)
    assert "42" in await page.text_content("#round-detail")

    assert errors == [], f"JS errors: {errors}"
    await page.close()


async def test_paytable_editor_blocks_saving_outside_the_rtp_guardrail(admin_server, pool, browser):
    _, username, password, totp = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await _login(page, admin_server, username, password, totp)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await _open_keno(page, "paytables")

    await page.select_option('#paytable-form select[name="profile"]', "low_variance")
    await page.select_option('#paytable-form select[name="pick_count"]', "1")
    # 1 pick: P(hit) = 0.25, so a 3.28x multiplier is 82% RTP -- inside [75%, 97%].
    await page.fill('#multiplier-inputs input[name="1"]', "3.28")
    await page.wait_for_selector("#preview .guardrail-ok", timeout=10000)
    assert await page.is_enabled("#save-paytable")

    # 5x is 125% RTP: the save button must lock and the preview must say why.
    await page.fill('#multiplier-inputs input[name="1"]', "5")
    await page.wait_for_selector("#preview .guardrail-bad", timeout=10000)
    assert await page.is_disabled("#save-paytable")

    assert errors == [], f"JS errors: {errors}"
    await page.close()


async def test_allowlist_add_through_the_ui_writes_the_real_row(admin_server, pool, conn, browser):
    user_id = await create_funded_user(conn, Decimal("0"))
    _, username, password, totp = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    await _login(page, admin_server, username, password, totp)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await _open_keno(page, "tiers")

    await page.fill('#allowlist-form input[name="user_id"]', str(user_id))
    await page.fill('#allowlist-form input[name="reason"]', "internal Stage 1 tester")
    await page.click('#allowlist-form button[type="submit"]')
    await page.wait_for_selector(f'[data-remove-user="{user_id}"]', timeout=10000)

    row = await conn.fetchrow("SELECT reason FROM keno_beta_allowlist WHERE user_id = $1", user_id)
    assert row["reason"] == "internal Stage 1 tester"
    await page.close()


async def test_support_role_sees_no_action_controls(admin_server, pool, browser):
    _, username, password, totp = await create_test_admin(pool, role="support")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await _login(page, admin_server, username, password, totp)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)
    await _open_keno(page, "overview")

    assert await page.locator('#keno-subnav [data-section="simulator"]').count() == 0
    assert await page.locator("#kill-switch-form, #reserve-form").count() == 0
    await _open_keno(page, "tiers")
    assert await page.locator("#allowlist-form, [data-set-tier]").count() == 0
    await _open_keno(page, "paytables")
    assert await page.locator("#paytable-form").count() == 0

    assert errors == [], f"JS errors: {errors}"
    await page.close()


async def test_simulator_runs_and_tier_version_drift_is_flagged(admin_server, pool, conn, browser):
    # A newer version of the *current* tier that the engine isn't using yet.
    current = await conn.fetchrow(
        "SELECT t.* FROM keno_tier_state s JOIN keno_risk_tiers t ON t.id = s.current_tier_id WHERE s.id = 1"
    )
    await conn.execute(
        "INSERT INTO keno_risk_tiers (tier_number, version, min_reserve, max_pick_count, max_top_multiplier, "
        "stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile) "
        "SELECT tier_number, (SELECT MAX(version) + 1 FROM keno_risk_tiers WHERE tier_number = $1), min_reserve, "
        "max_pick_count, max_top_multiplier, stake_options, max_win_per_ticket, max_round_exposure_pct, "
        "paytable_profile FROM keno_risk_tiers WHERE id = $2",
        current["tier_number"], current["id"],
    )
    _, username, password, totp = await create_test_admin(pool, role="superadmin")
    page = await browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await _login(page, admin_server, username, password, totp)
    await page.wait_for_selector("#app-shell:not([hidden])", timeout=10000)

    await _open_keno(page, "tiers")
    warning = await page.text_content("#keno-section .warning-text")
    assert "not in use" in warning
    assert await page.locator("text=Adopt latest version").count() == 1
    await page.screenshot(path="/tmp/admin-keno-tier-drift.png", full_page=True)

    await _open_keno(page, "simulator")
    await page.fill('#sim-form input[name="num_simulations"]', "200")
    await page.click('#sim-form button[type="submit"]')
    await page.wait_for_selector("#sim-result .stat-card", timeout=30000)
    assert "breaching the floor" in await page.text_content("#sim-result")
    await page.screenshot(path="/tmp/admin-keno-simulator-result.png", full_page=True)

    assert errors == [], f"JS errors: {errors}"
    await page.close()
