"""Real-browser, real-round-engine proof of the Keno Mini App screens
(build spec Part 13) -- same discipline as test_miniapp_e2e.py (a real
Chromium tab against the real static files and the real gateway/Postgres/
Redis) combined with test_keno_round_engine.py's own fast-config pattern
(a real KenoRoundEngine.run_forever() as a background task, real seconds,
just compressed) so this exercises the genuine end-to-end pipeline: tap
the board -> POST /api/keno/tickets -> the engine's real draw/settle ->
keno:live/user:{id} WebSocket events -> the result screen, not a mocked
DOM or a stubbed server.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, ledger, responsible_gaming
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from services.engine.keno_round_engine import KenoRoundEngine
from tests.integration.conftest import create_funded_user, next_telegram_id
from tests.integration.test_miniapp_e2e import prepare_page

pytestmark = pytest.mark.e2e

_version_counter = itertools.count(random.randint(7 * 10**6, 8 * 10**6))


def _next_version() -> int:
    return next(_version_counter)


_MULTIPLIERS = {1: Decimal("3.40")}


async def _seed_fast_config_and_tier(
    conn: asyncpg.Connection, *, keno_enabled: bool = True, beta_restricted: bool = False
) -> None:
    version = _next_version()
    await conn.execute(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             min_picks, max_picks, draw_count, number_pool_size,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled,
             beta_restricted, effective_from)
        VALUES ($1, 10, 3, 2, 2, 1, 5, 20, 80, 5, 5000, $2, $3, GREATEST(now() - interval '1 second', (SELECT max(effective_from) FROM keno_configs WHERE effective_from <= now()) + interval '1 microsecond'))
        """,
        version,
        keno_enabled,
        beta_restricted,
    )
    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile, effective_from)
        VALUES (1, $1, 0, 5, 16, ARRAY[10.00, 20.00, 50.00]::numeric(18,2)[], 800, 0.50, 'low_variance',
                GREATEST(now() - interval '1 second', (SELECT max(effective_from) FROM keno_risk_tiers WHERE effective_from <= now()) + interval '1 microsecond'))
        RETURNING id
        """,
        version,
    )
    stats = keno.compute_paytable_stats(1, _MULTIPLIERS)
    await conn.execute(
        """
        INSERT INTO keno_paytables
            (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
             max_multiplier, volatility, effective_from)
        VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6, GREATEST(now() - interval '1 second', (SELECT max(effective_from) FROM keno_paytables WHERE effective_from <= now()) + interval '1 microsecond'))
        """,
        version,
        json.dumps({str(k): str(v) for k, v in _MULTIPLIERS.items()}),
        int(stats.rtp * 10000),
        int(stats.hit_frequency * 10000),
        stats.max_multiplier,
        stats.volatility,
    )
    await conn.execute(
        "INSERT INTO keno_tier_state (id, current_tier_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1",
        tier_id,
    )
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    jackpot = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn,
        "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -Decimal("100000")), ledger.Entry(reserve.id, Decimal("100000"))],
        idempotency_key=f"test-e2e-reserve-{uuid.uuid4()}",
        created_by="test",
    )
    await conn.execute(
        "INSERT INTO keno_jackpot_pool (id, account_id) VALUES (1, $1) ON CONFLICT (id) DO NOTHING", jackpot.id
    )


@pytest.fixture(autouse=True)
async def _clean_keno_lock_and_rounds(pool: asyncpg.Pool, redis):
    async def _clean() -> None:
        await redis.delete(KENO_LOCK_KEY)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed', 'failed', 'voided')"
            )

    await _clean()
    yield
    await _clean()


async def test_keno_screen_full_flow_bet_to_result_over_a_real_browser(gateway_server, pool, redis, browser, conn):
    await _seed_fast_config_and_tier(conn)
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id, first_name="KenoPlayer")

    async with pool.acquire() as setup_conn:
        user_id = await create_funded_user(setup_conn, Decimal("1000.00"))
    # prepare_page's own stub carries a *different* telegram_id than the
    # one this fresh user was created under -- align them so the browser
    # session authenticates as the exact account this test just funded,
    # not a lazily-created zero-balance one.
    await pool.execute("UPDATE users SET telegram_id = $1 WHERE id = $2", telegram_id, user_id)

    engine = KenoRoundEngine(pool, redis)
    engine_task = asyncio.create_task(engine.run_forever())
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('1000')", timeout=10000
        )

        await page.click("#open-keno-btn")
        await page.wait_for_selector("#screen-keno.active", timeout=10000)
        await page.wait_for_function(
            "document.querySelectorAll('#keno-board .keno-cell').length === 80", timeout=10000
        )

        # First-ever visit to this device shows the zero-knowledge
        # onboarding overlay (spec 13) -- dismiss it via Skip before
        # interacting with the board underneath, the same way a real
        # first-time player would.
        await page.click("#keno-onboard-skip-btn")
        await page.wait_for_selector("#keno-onboarding-overlay", state="hidden", timeout=5000)

        # Real board state has to actually reach "betting_open" -- the
        # round engine's own first cycle needs a moment to create it.
        # refreshState() only runs once, from enter() -- board.setLocked()
        # unlocking is the real, observable signal that the WS-driven
        # keno.betting.open handler's own refreshState() has since landed.
        await page.wait_for_selector(".keno-cell:not(.locked)", timeout=10000)

        # Pick exactly one number (matches _MULTIPLIERS' own pick_count=1
        # paytable row) and the one stake option this tier offers.
        await page.click('.keno-cell[aria-label="7"]')
        await page.wait_for_function(
            "document.getElementById('keno-picks-count').textContent.trim() === '1 / 5'", timeout=5000
        )
        await page.click('#keno-stake-chips .amount-chip:has-text("10.00")')
        await page.wait_for_function(
            "!document.getElementById('keno-play-btn').disabled", timeout=5000
        )
        await page.screenshot(path="/tmp/keno-board-screen.png")

        balance_before = await ledger.user_balance_snapshot(pool, user_id)

        await page.click("#keno-play-btn")
        await page.wait_for_function(
            "document.getElementById('keno-my-tickets-section') && "
            "!document.getElementById('keno-my-tickets-section').classList.contains('hidden')",
            timeout=10000,
        )
        # The stake leaves the header balance via a real, independently
        # -timed balance_update WS push (packages/core/ledger.py's
        # publish_balance_update(), the same one every deposit/withdraw
        # already relies on) -- polling here rather than reading once
        # right after the ticket POST resolves avoids a race against
        # whichever of the two arrives first.
        await page.wait_for_function(
            "!document.getElementById('balance-amount').textContent.includes('1000')", timeout=10000
        )

        # The real engine closes betting, draws for real (draw_seconds=2),
        # and settles -- the board should show at least one real "drawn"
        # cell from the live keno.number.drawn stream well before this.
        await page.wait_for_function(
            "document.querySelectorAll('#keno-board .keno-cell.drawn').length > 0", timeout=15000
        )

        await page.wait_for_selector("#screen-keno-result.active", timeout=20000)
        result_amount_text = await page.text_content("#keno-result-amount")
        assert result_amount_text.strip() != ""

        await page.screenshot(path="/tmp/keno-result-screen.png")

        # Verifiable commit-reveal draw check (spec 4.1/4.3/13; never call
        # this "provably fair" -- spec 4.3's own wording): re-derives this exact
        # round's draw server-side from its own revealed seed, the same
        # capability Bingo's own fairness panel already proves for its
        # rounds. keno_round_engine.py's own _settle_and_complete() settles
        # tickets (which is what triggers this test's result screen, via
        # keno.ticket.settled) fractionally *before* the round row's own
        # status actually flips to 'completed' -- round_detail()'s verified
        # field stays None until that UPDATE lands, so this waits for the
        # real DB row rather than racing it the instant the result screen
        # appears, the same gap a real player tapping a moment later would
        # never even notice.
        async def _round_row_completed() -> bool:
            async with pool.acquire() as check_conn:
                status = await check_conn.fetchval(
                    "SELECT status FROM keno_rounds WHERE id = (SELECT round_id FROM keno_tickets WHERE user_id = $1 ORDER BY id DESC LIMIT 1)",
                    user_id,
                )
            return status == "completed"

        deadline = asyncio.get_event_loop().time() + 10.0
        while not await _round_row_completed():
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("round never reached status='completed' within 10s")
            await asyncio.sleep(0.1)

        await page.click("#keno-result-verify-btn")
        await page.wait_for_function(
            "document.getElementById('keno-fairness-hash').textContent.length > 0", timeout=10000
        )
        # The round is already fully "completed" by this point (its
        # result screen is what we're on) -- server_seed/drawn_numbers are
        # both published by then, so this always resolves to a real
        # verified=true, never the "not yet revealed" placeholder.
        # prepare_page's own stub sets language_code "am" (locales/am.json),
        # so this is fairness.yes's real Amharic value, not the English one.
        assert await page.text_content("#keno-fairness-verified") == "✅ አዎ"

        # History/Stats screen -- separate REST reads (rounds/recent,
        # tickets, stats, hot-cold), each on its own tab.
        await page.click("#keno-fairness-close-btn")
        await page.click("#keno-play-again-btn")
        await page.wait_for_selector("#screen-keno.active", timeout=5000)
        await page.click("#keno-history-btn")
        await page.wait_for_selector("#screen-keno-history.active", timeout=5000)
        await page.wait_for_function(
            "document.getElementById('keno-history-rounds-list').children.length > 0", timeout=10000
        )
        await page.click('.keno-history-tab[data-tab="tickets"]')
        await page.wait_for_function(
            "document.getElementById('keno-history-tickets-list').textContent.trim().length > 0", timeout=10000
        )
        await page.click('.keno-history-tab[data-tab="stats"]')
        await page.wait_for_function(
            "document.getElementById('keno-history-stats-grid').children.length === 4", timeout=10000
        )
        await page.click('.keno-history-tab[data-tab="hotcold"]')
        await page.wait_for_function(
            "document.getElementById('keno-hottest-numbers').children.length === 10", timeout=10000
        )
        await page.screenshot(path="/tmp/keno-history-screen.png")

        assert console_errors == [], f"JS errors during Keno flow: {console_errors}"
    finally:
        await page.close()
        engine.stop()
        await asyncio.wait_for(engine_task, timeout=15.0)

    async with pool.acquire() as verify_conn:
        ticket_row = await verify_conn.fetchrow(
            "SELECT status, payout FROM keno_tickets WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_id
        )
    assert ticket_row is not None
    assert ticket_row["status"] in ("won", "lost")
    balance_after = await ledger.user_balance_snapshot(pool, user_id)
    # Either the stake was lost (balance down by 10) or a win landed
    # (balance up by payout - 10) -- either way real money genuinely
    # moved through the real ledger for this real ticket.
    assert balance_after["cash"] != balance_before["cash"]


async def test_keno_button_stays_cleanly_hidden_for_a_non_allowlisted_user(gateway_server, pool, redis, browser, conn):
    """The exact scenario a CTO review asked to see with a screenshot,
    not just a passing assertion: keno_enabled=true (the game is
    genuinely running -- real rounds exist, see the DB check below) but
    this specific user isn't on the beta allowlist. The button must
    stay hidden cleanly -- no console error, no stuck spinner, no
    flash of the button before it disappears -- not merely "the API
    call fails and something downstream happens to look okay."""
    await _seed_fast_config_and_tier(conn, keno_enabled=True, beta_restricted=True)
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id, first_name="NotAllowlisted")

    async with pool.acquire() as setup_conn:
        user_id = await create_funded_user(setup_conn, Decimal("1000.00"))
    await pool.execute("UPDATE users SET telegram_id = $1 WHERE id = $2", telegram_id, user_id)

    engine = KenoRoundEngine(pool, redis)
    engine_task = asyncio.create_task(engine.run_forever())
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")

        # The app must still reach its own normal "ready" state --
        # proving nothing is stuck waiting on the Keno check specifically
        # (that would be the "stuck spinner" failure mode: a broken gate
        # that also wedges the rest of the page, not a clean hide).
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('1000')", timeout=10000
        )

        # A real round genuinely exists and keno_enabled is genuinely
        # true -- this isn't "hidden because nothing exists yet," it's
        # "hidden because this user specifically isn't allowed in."
        round_row = await pool.fetchrow("SELECT id FROM keno_rounds ORDER BY id DESC LIMIT 1")
        assert round_row is not None
        config_row = await pool.fetchrow("SELECT keno_enabled FROM keno_configs WHERE effective_from <= now() ORDER BY effective_from DESC LIMIT 1")
        assert config_row["keno_enabled"] is True

        # checkKenoAvailability() fires once, right after boot, with no
        # visible loading state of its own -- give it a real moment to
        # land, then assert the settled state rather than racing it.
        await page.wait_for_timeout(1500)

        is_hidden = await page.evaluate("document.getElementById('open-keno-btn').classList.contains('hidden')")
        assert is_hidden, "Keno button is visible to a non-allowlisted user"

        await page.screenshot(path="/tmp/keno-button-hidden-not-allowlisted.png")
        assert console_errors == [], f"JS errors while checking Keno availability: {console_errors}"
    finally:
        await page.close()
        engine.stop()
        await asyncio.wait_for(engine_task, timeout=15.0)


@pytest.mark.parametrize("language", ["am", "en"])
async def test_autoplay_start_shows_a_translated_refusal_for_a_self_excluded_player(
    gateway_server, pool, redis, browser, conn, language
):
    """Operator decision, 2026-09-25: a self-excluded player who tries to
    start Autoplay gets a clear message in their own language, not a
    generic failure and not a session that silently stops itself."""
    await _seed_fast_config_and_tier(conn)
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id, first_name="KenoPlayer")

    async with pool.acquire() as setup_conn:
        user_id = await create_funded_user(setup_conn, Decimal("1000.00"))
    await pool.execute("UPDATE users SET telegram_id = $1, language = $3 WHERE id = $2", telegram_id, user_id, language)
    with open(f"web/miniapp/locales/{language}.json", encoding="utf-8") as f:
        expected = json.load(f)["keno.autoplay_error.self_excluded"]

    engine = KenoRoundEngine(pool, redis)
    engine_task = asyncio.create_task(engine.run_forever())
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.click("#open-keno-btn")
        await page.wait_for_selector("#screen-keno.active", timeout=10000)
        await page.click("#keno-onboard-skip-btn")
        await page.wait_for_selector(".keno-cell:not(.locked)", timeout=10000)
        await page.click('.keno-cell[aria-label="7"]')
        await page.click('#keno-stake-chips .amount-chip:has-text("10.00")')
        await page.wait_for_function("!document.getElementById('keno-autoplay-btn').disabled", timeout=5000)

        # Self-excluded from another surface (the bot's /limits) while this
        # tab is already open -- the server check is what refuses it.
        await responsible_gaming.self_exclude(pool, user_id)

        await page.click("#keno-autoplay-btn")
        await page.wait_for_selector("#keno-autoplay-overlay:not(.hidden)", timeout=5000)
        await page.click("#keno-autoplay-setup-start-btn")
        await page.wait_for_selector("#keno-autoplay-setup-status.error", timeout=10000)
        assert (await page.text_content("#keno-autoplay-setup-status")).strip() == expected
        await page.screenshot(path=f"/tmp/keno-autoplay-refused-self-excluded-{language}.png")

        assert await pool.fetchval("SELECT count(*) FROM keno_autoplay_sessions WHERE user_id = $1", user_id) == 0
        assert console_errors == [], f"JS errors: {console_errors}"
    finally:
        await page.close()
        engine.stop()
        await asyncio.wait_for(engine_task, timeout=15.0)
