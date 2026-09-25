"""The Mini App keeps one server-clock offset (state.js serverNow()), reset
from every WebSocket frame that carries server_time. The gateway sends
server_time in milliseconds. The Keno engine's keno.betting.open frame sent
time.time() -- seconds -- and every authenticated connection receives
keno:live, whether or not the player ever opens Keno. Each new Keno round
therefore knocked the shared clock back ~55 years until the next pong
(every 20s) repaired it, and every countdown built on serverNow() showed
garbage in the meantime: Keno's own betting countdown, and Bingo's lobby
countdown for a player who never touched Keno (found 2026-09-25 in the
Keno player-experience pass).

Real browser, real gateway, real Bingo and Keno engines.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pytest

from services.engine.keno_round_engine import KenoRoundEngine
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room, next_telegram_id
from tests.integration.test_keno_miniapp_e2e import _seed_fast_config_and_tier
from tests.integration.test_miniapp_e2e import prepare_page

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
async def _clean_keno(pool, redis):
    from services.engine.keno_lock import LOCK_KEY

    async def clean() -> None:
        await redis.delete(LOCK_KEY)
        await pool.execute("UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed','failed','voided')")

    await clean()
    yield
    await clean()


async def _sample(page, selector: str, seconds: float) -> list[str]:
    samples = []
    for _ in range(int(seconds * 10)):
        samples.append((await page.text_content(selector)) or "")
        await asyncio.sleep(0.1)
    return samples


async def test_a_bingo_lobby_countdown_stays_sane_while_keno_rounds_open(
    gateway_server, browser, pool, redis, card_pool, conn
):
    await _seed_fast_config_and_tier(conn)  # a new Keno round every ~10s
    room = await load_room_config(
        pool, await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=60, is_active=True)
    )
    bingo = RoundEngine(pool, redis, room, card_pool)
    bingo_task = asyncio.create_task(bingo.run_forever())
    keno = KenoRoundEngine(pool, redis)
    keno_task = asyncio.create_task(keno.run_forever())

    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)
    user_id = await create_funded_user(conn, Decimal("100.00"))
    await pool.execute("UPDATE users SET telegram_id = $1, language = 'en' WHERE id = $2", telegram_id, user_id)
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.click(f'.room-card[data-room-id="{room.id}"]')
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[0].click()  # take a card: the lobby countdown only runs for a player in the round
        await page.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)

        samples = await _sample(page, "#lobby-countdown", 14)  # spans at least one Keno betting.open
        await page.screenshot(path="/tmp/bingo-lobby-countdown-with-keno-running.png")
        keno_rounds_opened = await pool.fetchval(
            "SELECT count(*) FROM keno_rounds WHERE betting_opened_at > now() - interval '20 seconds'"
        )
        assert keno_rounds_opened >= 1, "no Keno round opened during the sample window; the test proved nothing"

        seconds = [int(n) for s in samples for n in re.findall(r"\d+", s)]
        assert seconds, f"no countdown values sampled: {samples[:5]}"
        assert max(seconds) <= 60, f"lobby countdown showed {max(seconds)}s (60s lobby); samples: {sorted(set(samples))[-3:]}"
        assert console_errors == [], console_errors
    finally:
        await page.close()
        keno.stop()
        await asyncio.wait_for(keno_task, timeout=15)
        await bingo.stop()
        await asyncio.wait_for(bingo_task, timeout=15)


async def test_the_keno_betting_countdown_stays_within_the_betting_window(gateway_server, browser, pool, redis, conn):
    await _seed_fast_config_and_tier(conn)  # betting_seconds = 3
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)
    user_id = await create_funded_user(conn, Decimal("100.00"))
    await pool.execute("UPDATE users SET telegram_id = $1, language = 'en' WHERE id = $2", telegram_id, user_id)
    keno = KenoRoundEngine(pool, redis)
    keno_task = asyncio.create_task(keno.run_forever())
    try:
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.click("#open-keno-btn")
        await page.wait_for_selector("#screen-keno.active", timeout=10000)
        await page.click("#keno-onboard-skip-btn")

        samples = await _sample(page, "#keno-status-pill", 14)
        await page.screenshot(path="/tmp/keno-betting-countdown.png")
        counts = [int(m) for s in samples for m in re.findall(r"(\d+)s\b", s)]
        assert counts, f"never saw a betting countdown: {sorted(set(samples))}"
        assert max(counts) <= 3, f"betting countdown showed {max(counts)}s (3s window): {sorted(set(samples))[-3:]}"
        assert console_errors == [], console_errors
    finally:
        await page.close()
        keno.stop()
        await asyncio.wait_for(keno_task, timeout=15)
