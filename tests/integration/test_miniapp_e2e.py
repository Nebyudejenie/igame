"""Real-browser verification of the Mini App (spec's own UI document) --
not a mock DOM, an actual Chromium tab loading the actual gateway-served
static files, talking to a real WebSocket, real Postgres, real Redis, and
a real RoundEngine. This is what "start the dev server and use the feature
in a browser before reporting complete" means for a frontend change.

Telegram's `window.Telegram.WebApp` object is stubbed via an init script
(no real Telegram client exists in a test) -- everything downstream of
that (the WebSocket handshake, state_sync, gameplay) is the genuine app
talking to the genuine backend.

Two traps this file's first draft actually fell into, worth naming so they
don't come back:

1. index.html's real `<script src="https://telegram.org/js/telegram-web-app.js">`
   loads after page.add_init_script()'s stub runs, and overwrites
   `window.Telegram.WebApp.initData` back to "" (there's no real Telegram
   environment) -- silently breaking auth. Fixed by blocking that request
   entirely via page.route() so only our stub exists.
2. `#screen-rooms` used to default to `class="active"` in the raw HTML (so
   *something* was visible before JS ran), which made
   `wait_for_selector("#screen-rooms.active")` pass regardless of whether
   auth had actually succeeded -- and the balance placeholder used to be
   "0.00 ETB", which is indistinguishable from a real zero balance. Fixed
   by not defaulting any screen to active, and using "-- ETB" as the
   loading placeholder so a real "0.00 ETB" is unambiguous proof `authed`
   actually arrived.
"""

import asyncio
import json
from decimal import Decimal

import pytest

from packages.core.bingo import letter_for
from services.admin.simulated_players_queries import active_simulated_player_count
from services.engine import settlement
from services.engine.round_engine import RoundEngine, load_room_config
from services.gateway import queries
from tests.integration.conftest import (
    build_init_data,
    create_funded_user,
    create_room,
    fund_user,
    next_telegram_id,
)

pytestmark = pytest.mark.e2e

TELEGRAM_SDK_URL = "https://telegram.org/js/telegram-web-app.js"


def telegram_stub_script(init_data: str, telegram_id: int, first_name: str) -> str:
    payload = {
        "initData": init_data,
        "initDataUnsafe": {"user": {"id": telegram_id, "first_name": first_name, "language_code": "am"}},
        "themeParams": {},
    }
    return f"""
    window.Telegram = {{
      WebApp: {{
        initData: {json.dumps(payload["initData"])},
        initDataUnsafe: {json.dumps(payload["initDataUnsafe"])},
        themeParams: {json.dumps(payload["themeParams"])},
        ready: function() {{}},
        expand: function() {{}},
        HapticFeedback: {{
          impactOccurred: function() {{}},
          notificationOccurred: function() {{}}
        }},
        BackButton: {{
          show: function() {{}},
          hide: function() {{}},
          // Stores the app's real handler so a test can invoke it directly
          // (window.__triggerBackButton()) to simulate a genuine press --
          // Telegram's own native chrome has no DOM element a Playwright
          // click() could target.
          onClick: function(cb) {{ window.__triggerBackButton = cb; }}
        }},
        // Real Telegram opens its native share sheet for this; there's no
        // such UI in a test browser, so this just records the exact URL
        // the app asked to open, for a test to assert against.
        openTelegramLink: function(url) {{ window.__openedTelegramLinks.push(url); }}
      }}
    }};
    window.__openedTelegramLinks = [];
    """


async def prepare_page(browser, telegram_id: int, first_name: str = "Nebyu"):
    init_data = build_init_data(telegram_id, first_name=first_name)
    page = await browser.new_page(viewport={"width": 390, "height": 780})
    console_errors: list[str] = []
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    # Must be the real Telegram SDK never running at all, not just our
    # stub running "first" -- add_init_script() runs before every page
    # script, but the SDK's own <script> tag would still execute afterward
    # and overwrite window.Telegram.WebApp.initData back to "". A plain
    # `lambda route: route.abort()` creates the coroutine and never awaits
    # or schedules it -- silently a no-op -- hence a real async def here.
    async def block_real_sdk(route):
        await route.abort()

    await page.route(TELEGRAM_SDK_URL, block_real_sdk)
    await page.add_init_script(telegram_stub_script(init_data, telegram_id, first_name))
    return page, console_errors


async def test_miniapp_loads_authenticates_and_shows_balance(gateway_server, browser, conn):
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")

    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    # "-- ETB" is the loading placeholder; a real "0.00 ETB" only appears
    # once the authed message actually arrives and app.js writes it in.
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
    )

    assert console_errors == [], f"JS errors on load: {console_errors}"

    await page.screenshot(path="/tmp/miniapp-rooms.png")
    await page.close()


async def test_a_genuinely_fresh_room_with_no_round_history_can_still_be_entered(
    gateway_server, browser, conn
):
    """A real production incident: every room in a brand-new deployment
    starts with zero round history -- round_engine.py only creates a
    round row lazily, on the very first take_card. Before that,
    build_state_sync() reports status "idle" (queries.py's own default
    when the round_row lookup finds nothing). The state_sync handler
    used to route "idle" straight back to the room list with no other
    path to the card-selection screen anywhere in the client -- a real
    first player could open a room and then never take a card, ever,
    since nothing else in the app ever requests the lobby UI.

    Every other e2e test in this file pre-seeds the room via a direct
    engine.join() before the browser ever connects, which always leaves
    the room already in "lobby" status by the time a real click lands --
    that's exactly why the whole suite never caught this. This test is
    deliberately the one exception: the browser is the *first and only*
    participant to ever touch this room, reproducing the real incident
    precisely (found live, diagnosed with a real Playwright session
    against the actual production domain, capturing the real WebSocket
    frames -- join sent, state_sync came back status: "idle", and the
    screen simply never left #screen-rooms).
    """
    # Deliberately no RoundEngine running for this room at all -- the WS
    # "join" handler serves state_sync straight from Postgres
    # (services/gateway/queries.py::build_state_sync(), by design: "works
    # correctly even if the room's engine just crashed and hasn't been
    # replaced yet"), so reaching the lobby screen genuinely doesn't
    # depend on a live engine. This also keeps the test honest: no engine
    # means no join() call could have silently pre-seeded a round either.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, is_active=True, max_cards_per_player=1,
    )
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('.')", timeout=10000
    )

    room_selector = f'.room-card[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_selector, timeout=10000)
    await page.click(room_selector)

    # The actual bug: this used to stay on #screen-rooms forever, no
    # matter how long you waited, because nothing ever transitioned it.
    await page.wait_for_selector("#screen-lobby.active", timeout=10000)
    cells = await page.query_selector_all(".card-grid-cell")
    assert len(cells) == 432, "the lobby's own card grid must render even with no round yet"

    assert console_errors == [], f"JS errors entering a genuinely fresh room: {console_errors}"
    await page.close()


async def test_a_room_whose_last_round_ended_can_still_be_reentered(gateway_server, browser, conn):
    """A real, currently-live production incident, worse than the "genuinely
    fresh room" one above: RoundEngine._reset_to_idle() (round_engine.py)
    flips the *in-memory* engine back to "idle" the instant a round voids
    or finishes settling, but build_state_sync() reads Postgres, where that
    same round's row keeps its terminal status ('voided'/'done') forever
    until a brand-new round is inserted. The only thing that ever inserts
    one is RoundEngine.join() (via take_card), which only ever runs once a
    player is actually looking at the lobby/card-grid screen -- and the
    state_sync handler routes "voided"/"done" straight back to the room
    list, never to the lobby. Before the fix, that's a permanent deadlock:
    every room goes unplayable forever the moment its first-ever round
    ends, since nobody can ever reach the lobby again to take a card that
    would start the next one. Not caught earlier because no round had ever
    actually finished end-to-end against a real Telegram client until this
    session's has_main_web_app fix let one complete for the first time --
    the very next real player to tap that same room hit this immediately.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, is_active=True, max_cards_per_player=1,
    )
    await conn.execute(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 1, 'voided', 10.00, 2000, 'test-hash', now())",
        room_id,
    )

    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)
    await page.wait_for_function(
        "document.getElementById('balance-amount').textContent.includes('.')", timeout=10000
    )

    room_selector = f'.room-card[data-room-id="{room_id}"]'
    await page.wait_for_selector(room_selector, timeout=10000)
    await page.click(room_selector)

    # The actual bug: this used to bounce straight back to #screen-rooms
    # (state_sync reporting status "voided" forever) no matter how long you
    # waited or how many times you tapped the room again.
    await page.wait_for_selector("#screen-lobby.active", timeout=10000)
    cells = await page.query_selector_all(".card-grid-cell")
    assert len(cells) == 432, "the lobby's own card grid must render for the next round"

    assert console_errors == [], f"JS errors reentering a room whose last round ended: {console_errors}"
    await page.close()


async def test_a_player_who_took_a_card_in_an_underfilled_round_is_not_left_frozen_at_zero(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real production incident, caught on video from a real Android
    device: a player takes a card, the lobby countdown ticks normally,
    reaches 0 -- and then just freezes there forever. The countdown
    reaching 0 is a client-local computation from lobby_deadline_ms;
    nothing was ever pushed to an already-connected client telling it the
    round had actually voided (too few players) and a new one had already
    opened server-side. Every OTHER termination path already broadcasts
    something (round_end for a winner or an exhausted round); the
    underfilled-lobby path was the one silent one. min_players=2 and only
    this one browser player ever joining guarantees the round underfills.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=4,
        no_player_next_round_delay_seconds=1, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[74].click()  # card #75, matching the real incident

        # Polled, not a single immediate fetch: a tap fires take_card and
        # moves on with no confirm step to wait on, so the async gateway
        # -> engine round trip may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("90.00"):
                break
            await asyncio.sleep(0.05)
        assert balance_after_stake == Decimal("90.00"), "the stake must actually be charged first"

        # The bug: before the fix, the screen just sat here at "0" forever,
        # no matter how long this waited -- there was nothing to wake it up.
        await page.wait_for_selector("#screen-rooms.active", timeout=15000)

        toast_text = await page.text_content("#toast")
        assert toast_text and "refunded" in toast_text.lower(), (
            f"expected a refund toast explaining why, got: {toast_text!r}"
        )

        for _ in range(100):
            balance_after_refund = await pool.fetchval(
                balance_query,
                user_row["id"],
            )
            if balance_after_refund == Decimal("100.00"):
                break
            await asyncio.sleep(0.05)
        assert balance_after_refund == Decimal("100.00"), "the stake must be refunded back in full"

        assert console_errors == [], f"JS errors during the underfilled-round bounce-back: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_taking_a_card_shows_the_generated_bingo_card_immediately_in_the_lobby(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real gap, flagged directly against a user's own screen recording:
    tapping a card number only ever turned that grid cell purple -- the
    actual 5x5 Bingo grid a player is about to play never appeared until
    the round transitioned to #screen-game, minutes later once selection
    closed. A player watching only the number grid change color, with no
    confirmation of what card they actually hold, has no way to tell a
    real assignment from a UI glitch. This proves the generated card
    renders in the lobby itself, immediately after the take_card ack --
    no round start, no screen change, no extra action needed.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=30, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        # Not shown before any card is taken.
        assert await page.is_hidden("#lobby-your-cards-section")

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[74].click()  # card #75, matching the real incident

        # Still on the lobby screen throughout -- this is the whole point.
        await page.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)
        assert await page.get_attribute("#screen-lobby", "class") and "active" in (
            await page.get_attribute("#screen-lobby", "class") or ""
        )

        title_text = await page.text_content("#lobby-your-cards-list .your-card-title")
        assert title_text and "75" in title_text, f"expected the card's own number, got {title_text!r}"

        card_cells = await page.query_selector_all("#lobby-your-cards-list .card-cell")
        assert len(card_cells) == 25, "a real 5x5 grid, not a placeholder"

        assert console_errors == [], f"JS errors showing the lobby card preview: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_solo_player_reaches_active_gameplay_and_a_real_result(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """An explicit, repeated product correction: the live round must never
    be gated on player count beyond what settlement genuinely needs.
    min_players=1 (this same change's own migration widens the room
    constraint to allow it) proves the whole real user-facing flow for a
    single real player, end to end through a real browser: card tap ->
    generated card shown -> selection timer expires with nobody else ever
    joining -> the round still goes ACTIVE, not underfilled-and-refunded
    -> real automatic ball calling -> a real terminal result. This is the
    exact case that used to bounce back to the room list ("not enough
    players joined... refunded") when this room's own min_players was 2.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[0].click()  # card #1
        await page.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)

        # Deliberately do nothing else -- no second player, ever. This is
        # the exact case min_players=2 used to bounce back to the room
        # list for, permanently, once the timer ran out.
        await page.wait_for_selector("#screen-game.active", timeout=20000)
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        result_amount = await page.text_content("#result-amount")
        assert result_amount, "a real terminal result, win or refund, either way not stuck"

        assert console_errors == [], f"JS errors during solo gameplay: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_telegram_back_button_works_from_a_live_game_screen(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real product gap: Telegram's native Back button is shown on the
    game screen (app.v6.js's showScreen() calls tg.BackButton.show() for
    every screen except "rooms"), but its onClick handler used to only
    handle wallet/lobby/result -- pressing it during a live round did
    nothing, despite the button being visibly there. showScreen() itself
    is a pure client-side view switch (no WebSocket/drop_card call), so
    this proves both halves: the button now actually navigates back to
    the room list, and the real round keeps running server-side
    completely unaffected by a player merely looking away from it.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[0].click()  # card #1
        await page.wait_for_selector("#screen-game.active", timeout=20000)

        # Simulate a genuine Telegram Back press -- there's no real DOM
        # element for Telegram's own native chrome, so the stub above
        # captured the app's real onClick handler for direct invocation.
        await page.evaluate("window.__triggerBackButton()")
        await page.wait_for_selector("#screen-rooms.active", timeout=5000)

        # The round itself must be completely unaffected -- still running
        # (or already progressed on its own), never voided/dropped just
        # because the player navigated away from looking at it.
        round_status = await pool.fetchval(
            "SELECT status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_status in ("running", "settling", "done"), round_status
        entry_count = await pool.fetchval(
            "SELECT count(*) FROM round_entries re JOIN rounds r ON r.id = re.round_id "
            "WHERE r.room_id = $1 AND re.user_id = $2",
            room_id, user_row["id"],
        )
        assert entry_count == 1  # card was never dropped by navigating away

        assert console_errors == [], f"JS errors during back-button navigation: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_the_auto_switch_toggles_and_really_persists_both_round_and_user_level(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """The game screen's AUTO toggle (#auto-switch) -- verifying it isn't
    just a label that flips a CSS class: a real click must actually reach
    the server (services/gateway/connection.py's "set_auto" handler) and
    persist in both places the spec calls for -- the current round's own
    round_entries.auto_mark (what the engine's auto-claim logic reads
    right now) and the player's account-level users.auto_mark_preference
    (what their *next* round starts from). 300ms/call (matching this
    file's other post-round_start interaction tests) gives comfortable
    real time to click and check the database mid-round without racing
    the round to its own natural end -- a slower interval was tried first
    and made teardown (engine.stop() only takes effect once the current
    round's own call loop naturally finishes, up to 75 calls away) take
    minutes instead of seconds.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=8, call_interval_ms=300,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        await cells[0].click()  # card #1
        await page.wait_for_selector("#screen-game.active", timeout=20000)

        # Defaults to on -- both the static markup (class="switch on") and
        # get_auto_mark_preference()'s own fallback for a first-time user.
        assert "on" in (await page.get_attribute("#auto-switch", "class") or "")
        assert await page.get_attribute("#auto-switch", "aria-checked") == "true"

        round_id = await pool.fetchval(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_id is not None

        async def auto_mark_in_round() -> bool:
            value = await pool.fetchval(
                "SELECT auto_mark FROM round_entries WHERE round_id = $1 AND user_id = $2",
                round_id, user_row["id"],
            )
            return bool(value)

        async def auto_mark_preference() -> bool:
            value = await pool.fetchval(
                "SELECT auto_mark_preference FROM users WHERE id = $1", user_row["id"]
            )
            return bool(value)

        assert await auto_mark_in_round() is True
        assert await auto_mark_preference() is True

        # Click it off.
        await page.click("#auto-switch")
        await page.wait_for_function(
            "document.getElementById('auto-switch').getAttribute('aria-checked') === 'false'",
            timeout=5000,
        )
        assert "on" not in (await page.get_attribute("#auto-switch", "class") or "")

        async def poll_db_false() -> None:
            for _ in range(50):
                if not await auto_mark_in_round() and not await auto_mark_preference():
                    return
                await asyncio.sleep(0.1)
            raise AssertionError("auto_mark never reached False in the database after clicking off")

        await poll_db_false()

        # Click it back on -- both the UI and the database must recover.
        await page.click("#auto-switch")
        await page.wait_for_function(
            "document.getElementById('auto-switch').getAttribute('aria-checked') === 'true'",
            timeout=5000,
        )
        assert "on" in (await page.get_attribute("#auto-switch", "class") or "")

        async def poll_db_true() -> None:
            for _ in range(50):
                if await auto_mark_in_round() and await auto_mark_preference():
                    return
                await asyncio.sleep(0.1)
            raise AssertionError("auto_mark never reached True in the database after clicking on")

        await poll_db_true()

        assert console_errors == [], f"JS errors toggling AUTO: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=30)


async def test_switching_rooms_does_not_leak_a_stray_rooms_call_broadcast(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """Real reported bug: joinRoom() subscribes this WebSocket connection
    to a room's live "call" broadcasts (services/gateway/connection.py's
    _joined_rooms / FanoutHub.subscribe_room()), but nothing ever called
    leaveRoom() when navigating away from a room -- a player who looked
    into several different rooms in one session (verbatim report: "I
    create 6 rooms and when I enter to each room... room number 6 is
    working crazy, the number of out is 8 but it is over 20 numbers")
    stayed subscribed to every one of them at once, so a fast, unwatched
    room's own independent call sequence kept bleeding into whatever room
    the player was actually looking at.

    Room A ticks unattended, fast, with nobody ever watching it, so by
    the time the player switches away to Room B its own real call_index
    is already far higher than anything Room B could show this early --
    if Room A's calls were still leaking in after leaving it, Room B's
    own displayed count would be contaminated with that much higher
    number instead of matching Room B's own real, still-low progress.
    """
    room_a = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=1, call_interval_ms=150,
        is_active=True,
    )
    room_b = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=8, call_interval_ms=300,
        is_active=True,
    )
    # None until actually created inside the try below -- if anything here
    # raises before both tasks exist, the finally block below has nothing
    # it needs to await/stop, instead of a NameError masking the real
    # failure or (worse) a half-created engine leaking as a detached task.
    task_a = None
    task_b = None
    try:
        room_a_config, room_b_config = await asyncio.gather(
            load_room_config(pool, room_a), load_room_config(pool, room_b)
        )
        engine_a = RoundEngine(pool, redis, room_a_config, card_pool)
        engine_b = RoundEngine(pool, redis, room_b_config, card_pool)
        task_a = asyncio.create_task(engine_a.run_forever())
        task_b = asyncio.create_task(engine_b.run_forever())

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        room_a_selector = f'.room-card[data-room-id="{room_a}"]'
        room_b_selector = f'.room-card[data-room-id="{room_b}"]'

        # Room A is already running and calling on its own, unattended --
        # real time to rack up a real, high call_index before the player
        # ever looks at it (75 calls at 150ms/call = 11.25s to exhaust, so
        # this stays well inside its first, still-running round).
        await asyncio.sleep(3.5)

        await page.wait_for_selector(room_a_selector, timeout=10000)
        await page.click(room_a_selector)
        await page.wait_for_selector("#screen-game.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=10000
        )

        room_a_call_index = await pool.fetchval(
            "SELECT call_index FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_a
        )
        assert room_a_call_index > 15, (
            f"test setup assumption failed: room A should already be well into its own "
            f"call sequence by now, got {room_a_call_index}"
        )

        # Back to the room list -- the real Telegram Back press, the exact
        # navigation that used to leave room A's subscription alive
        # forever (see test_telegram_back_button_works_from_a_live_game_
        # screen for this same real-handler-invocation technique).
        await page.evaluate("window.__triggerBackButton()")
        await page.wait_for_selector("#screen-rooms.active", timeout=5000)

        await page.wait_for_selector(room_b_selector, timeout=10000)
        await page.click(room_b_selector)
        await page.wait_for_selector("#screen-game.active", timeout=15000)
        await page.wait_for_function(
            "document.getElementById('stat-call').textContent !== '0/75'", timeout=15000
        )

        displayed_call_text = await page.text_content("#stat-call")
        displayed_call_index = int((displayed_call_text or "0/75").split("/")[0])
        room_b_real_call_index = await pool.fetchval(
            "SELECT call_index FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_b
        )
        # A tolerant, not exact, comparison -- the DOM read and the DB read
        # happen a moment apart, so being one real call ahead or behind is
        # expected and fine. The bug this guards against would show a
        # number close to (or past) room A's own by-then-even-higher
        # call_index, nowhere close to room B's own real, still-early one.
        assert abs(displayed_call_index - room_b_real_call_index) <= 2, (
            f"displayed {displayed_call_index}, but room B's real call_index is "
            f"{room_b_real_call_index} -- likely contaminated by room A's own calls"
        )
        assert displayed_call_index < room_a_call_index, (
            f"displayed {displayed_call_index} for room B is not below room A's own "
            f"{room_a_call_index} -- looks exactly like the reported cross-room leak"
        )

        assert console_errors == [], f"JS errors switching rooms: {console_errors}"
        await page.close()
    finally:
        # 30s, not this file's usual 15s: engine.stop() only takes effect
        # once each engine's *current* round naturally finishes (up to 75
        # calls away) -- see test_the_auto_switch_toggles_and_really_
        # persists_both_round_and_user_level's own comment on this same
        # asyncio.wait_for above for the full explanation. Both engines
        # tick independently, so stopping and awaiting them concurrently
        # (rather than one full 30s wait_for after another) keeps this
        # teardown's worst case ~30s instead of ~60s -- and task_a/task_b
        # are only ever None if construction itself failed above, in which
        # case there's nothing here to stop.
        if task_a is not None and task_b is not None:
            await asyncio.gather(engine_a.stop(), engine_b.stop())
            await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=30)


async def test_a_late_joiner_sees_an_already_taken_card_as_taken(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A real production gap, caught while building an unrelated real
    two-player production test: Player A takes a card, then Player B
    connects to the same room for the first time. Before this fix, B's
    own state_sync (build_state_sync() in services/gateway/queries.py)
    never included a taken_cards list at all -- only a live card_taken
    broadcast ever taught a client a given card was unavailable, and B
    was never subscribed to receive the one that would have covered A's
    card, since it happened before B ever joined. B's own tap on that
    card would still have been correctly rejected server-side (join()'s
    own PRIMARY KEY on round_entries(round_id, card_no)), but B's grid
    kept showing it as available with nothing telling them otherwise.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=30, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id_a = next_telegram_id()
        page_a, _ = await prepare_page(browser, telegram_id_a, first_name="PlayerA")
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page_a.goto(http_base + "/")
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        user_a = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id_a)
        assert user_a is not None
        await fund_user(conn, user_a["id"], Decimal("100.00"))
        await page_a.reload()
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_a.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page_a.wait_for_selector(room_selector, timeout=10000)
        await page_a.click(room_selector)
        await page_a.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells_a = await page_a.query_selector_all(".card-grid-cell")
        await cells_a[0].click()  # card #1
        await page_a.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)

        # Only now does Player B ever connect -- entirely after A's take_card
        # has already fully landed, so B was never subscribed to receive
        # that specific card_taken broadcast.
        telegram_id_b = next_telegram_id()
        page_b, console_errors_b = await prepare_page(browser, telegram_id_b, first_name="PlayerB")
        await page_b.goto(http_base + "/")
        await page_b.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_b.click(room_selector)
        await page_b.wait_for_selector("#screen-lobby.active", timeout=10000)

        cell_1_b = page_b.locator(".card-grid-cell").first
        await asyncio.wait_for(
            _wait_for_class(cell_1_b, "taken"),
            timeout=5,
        )
        classes = await cell_1_b.get_attribute("class")
        assert "taken" in (classes or ""), f"card #1 must show as taken for a fresh joiner, got: {classes!r}"
        assert "selected" not in (classes or ""), "taken, not B's own selection"

        assert console_errors_b == [], f"JS errors for the late joiner: {console_errors_b}"
        await page_a.close()
        await page_b.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_rooms_screen_shows_a_real_online_and_playing_headcount(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """Per explicit request: the rooms (stake-picker) screen shows how
    alive the platform is the moment it opens. "Online" must be a real
    count of open WebSocket connections (services/gateway/connection.py's
    online_count on the "rooms" push), not a guess -- proven here by
    connecting a genuine second real browser tab and confirming the
    figure the *second* connection sees actually includes the first.
    "Playing" is the sum of every room's own player count, confirmed
    against a real card a first player actually took.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=30, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    # Delta, not an absolute total: this shared dev database accumulates
    # real is_active=true rooms across every test run (the same
    # "3092 leftover test rooms" class of pollution this session already
    # hit elsewhere), so "Playing" sums real activity across every one of
    # them, not just this test's own room. "Online" now also folds in
    # active_simulated_player_count() (idle/joining/playing bots read as
    # "online" too, same as a real connected-but-not-yet-playing player
    # would) -- baselined the same way, since the shared dev DB can have
    # real active bots at test time.
    baseline_playing = sum(r["players"] for r in await queries.list_rooms(pool))
    baseline_online = await active_simulated_player_count(pool)

    try:
        telegram_id_a = next_telegram_id()
        page_a, console_errors_a = await prepare_page(browser, telegram_id_a, first_name="PlayerA")
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page_a.goto(http_base + "/")
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_a.wait_for_function(
            f"document.getElementById('rooms-online-count').textContent === '{baseline_online + 1}'",
            timeout=10000,
        )

        user_a = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id_a)
        assert user_a is not None
        await fund_user(conn, user_a["id"], Decimal("100.00"))
        await page_a.reload()
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_a.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page_a.wait_for_selector(room_selector, timeout=10000)
        await page_a.click(room_selector)
        await page_a.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells_a = await page_a.query_selector_all(".card-grid-cell")
        await cells_a[0].click()  # card #1
        await page_a.wait_for_selector("#lobby-your-cards-section:not(.hidden)", timeout=10000)

        # Player B connects fresh, after A is both online AND holding a
        # real card -- B's own very first "rooms" push must reflect both.
        telegram_id_b = next_telegram_id()
        page_b, console_errors_b = await prepare_page(browser, telegram_id_b, first_name="PlayerB")
        await page_b.goto(http_base + "/")
        await page_b.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_b.wait_for_function(
            f"document.getElementById('rooms-online-count').textContent === '{baseline_online + 2}'",
            timeout=10000,
        )
        playing_text = await page_b.text_content("#rooms-playing-count")
        assert playing_text == str(baseline_playing + 1), (
            f"expected baseline ({baseline_playing}) + A's own real card = {baseline_playing + 1}, "
            f"got {playing_text!r}"
        )

        assert console_errors_a == [], f"JS errors for player A: {console_errors_a}"
        assert console_errors_b == [], f"JS errors for player B: {console_errors_b}"
        await page_a.close()
        await page_b.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_room_list_derash_only_shows_once_running_and_is_the_real_derash_not_the_pot(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """Per explicit request: the stake-selection (Rooms) screen must not
    show a derash figure at all until a room's round has actually passed
    its lobby cutoff and started running (the pot is still filling up
    until then, so advertising a number would be premature) -- and once
    shown, it must be the real, post-house-cut derash (services/gateway/
    queries.py::list_rooms(), via settlement.compute_derash()), not the
    raw gross pot the "Derash up to X" copy used to display.
    """
    # A generous lobby, unlike this file's other fast-timing tests: the
    # "before cutoff" assertion below needs a real, comfortable window to
    # land inside a real second browser connection's own full boot
    # sequence (page load, WS handshake, funding, reload) before the round
    # transitions to running -- a short lobby made this flaky (the round
    # was already running by the time player B's own check ran).
    house_cut_bps = 2000
    room_id = await create_room(
        conn, stake=Decimal("10.00"), house_cut_bps=house_cut_bps, min_players=1,
        lobby_seconds=20, call_interval_ms=300, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    room_selector = f'.room-card[data-room-id="{room_id}"]'

    try:
        telegram_id_a = next_telegram_id()
        page_a, console_errors_a = await prepare_page(browser, telegram_id_a, first_name="PlayerA")
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page_a.goto(http_base + "/")
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        user_a = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id_a)
        assert user_a is not None
        await fund_user(conn, user_a["id"], Decimal("100.00"))
        await page_a.reload()
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_a.wait_for_selector(room_selector, timeout=10000)
        await page_a.click(room_selector)
        await page_a.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells_a = await page_a.query_selector_all(".card-grid-cell")
        await cells_a[0].click()  # stakes into the round -- pot is now 10.00, status "lobby"

        # A second, fresh connection's very first "rooms" push must still
        # hide the derash line for this room: the lobby hasn't closed yet.
        telegram_id_b = next_telegram_id()
        page_b, console_errors_b = await prepare_page(browser, telegram_id_b, first_name="PlayerB")
        await page_b.goto(http_base + "/")
        await page_b.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_b.wait_for_selector(room_selector, timeout=10000)
        derash_text_before = await page_b.text_content(f'{room_selector} .derash-line')
        assert (derash_text_before or "").strip() == "", (
            f"expected no derash shown before the lobby closes, got {derash_text_before!r}"
        )

        # Player A's own client naturally moves to the game screen the
        # instant round_start actually broadcasts -- the real signal the
        # lobby has closed and the round is running, no arbitrary sleep.
        await page_a.wait_for_selector("#screen-game.active", timeout=30000)

        # Player B reloads (re-running boot()'s own refreshRoomList()) --
        # same real "rooms" push, now for a room whose round is running.
        await page_b.reload()
        await page_b.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_b.wait_for_selector(room_selector, timeout=10000)
        await page_b.wait_for_function(
            f"document.querySelector('{room_selector} .derash-line').textContent.trim() !== ''",
            timeout=10000,
        )
        derash_text_after = await page_b.text_content(f'{room_selector} .derash-line')
        expected_derash, _ = settlement.compute_derash(Decimal("10.00"), house_cut_bps)
        assert str(expected_derash) in (derash_text_after or ""), (
            f"expected the real derash ({expected_derash}) in {derash_text_after!r}"
        )
        # The old "up to" pot-based copy must be gone -- this is the real,
        # final derash for a locked-in pot, not an upper bound.
        assert "10.00" not in (derash_text_after or ""), (
            f"expected the derash figure, not the raw pot, got {derash_text_after!r}"
        )

        assert console_errors_a == [], f"JS errors for player A: {console_errors_a}"
        assert console_errors_b == [], f"JS errors for player B: {console_errors_b}"
        await page_a.close()
        await page_b.close()
    finally:
        # 30s, not this file's usual 15s: engine.stop() only sets a flag
        # _run_running()'s own per-call loop never actually checks (only
        # self._status/self._lock.is_held() do) -- it's honored at the
        # *outer* run_forever() loop boundary, i.e. only once the current
        # round naturally finishes. With a single real card and this room's
        # default win_patterns (min_winning_lines=2), that can run the
        # full 75-call sequence at 300ms/call (~22.5s) before a win lands,
        # confirmed directly: this teardown took ~38s wall-clock in
        # isolation, comfortably failing the file's usual 15s budget.
        await engine.stop()
        await asyncio.wait_for(task, timeout=30)


async def test_spectate_shows_the_admin_configured_announcement_marquee(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """The spectate screen's old "Reserve a card" button was replaced
    entirely -- it re-synced the whole view for zero real benefit
    (join() can't assign a card mid-round; the player auto-advances into
    the next round regardless), which looked exactly like an already
    -shown called number flickering away and back. In its place: a
    genuinely useful, admin-configurable scrolling announcement
    (services/admin/announcement_queries.py), fetched once at boot via
    /api/announcement and rendered here.

    Proven against a real running round: with a real announcement set
    and enabled, a spectating player's marquee actually contains that
    exact text, the old button element is gone entirely, and -- the
    original report's own concern -- the called-number board is
    completely unaffected by anything on this screen.
    """
    from services.admin import announcement_queries
    from tests.integration.test_admin_auth import create_test_admin

    admin_id, *_ = await create_test_admin(pool)
    announcement_text = "Deposit bonus this week — ask support for details!"
    await announcement_queries.update_announcement_admin(
        pool, admin_id=admin_id, text=announcement_text, enabled=True,
        reason="e2e test", ip_address=None,
    )

    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=3, call_interval_ms=300,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        telegram_id_a = next_telegram_id()
        page_a, _ = await prepare_page(browser, telegram_id_a, first_name="PlayerA")
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page_a.goto(http_base + "/")
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        user_a = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id_a)
        assert user_a is not None
        await fund_user(conn, user_a["id"], Decimal("100.00"))
        await page_a.reload()
        await page_a.wait_for_selector("#screen-rooms.active", timeout=10000)
        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page_a.wait_for_selector(room_selector, timeout=10000)
        await page_a.click(room_selector)
        await page_a.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells_a = await page_a.query_selector_all(".card-grid-cell")
        await cells_a[0].click()
        await page_a.wait_for_selector("#screen-game.active", timeout=20000)

        # Player B connects fresh while the round is already running and
        # holds no card -- the real, natural way to reach spectate mode.
        telegram_id_b = next_telegram_id()
        page_b, console_errors_b = await prepare_page(browser, telegram_id_b, first_name="PlayerB")
        await page_b.goto(http_base + "/")
        await page_b.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page_b.click(room_selector)
        await page_b.wait_for_selector("#screen-game.active", timeout=10000)
        await page_b.wait_for_selector("#spectate-banner:not(.hidden)", timeout=10000)
        await page_b.wait_for_selector("#announcement-marquee:not(.hidden)", timeout=10000)

        marquee_text = await page_b.text_content("#announcement-marquee-text")
        assert marquee_text == announcement_text, f"unexpected marquee text: {marquee_text!r}"

        # The old button is gone entirely, not just hidden.
        old_button = await page_b.query_selector("#reserve-card-btn")
        assert old_button is None, "the old Reserve button must no longer exist in the DOM"

        # At least one real call must land before this proves the board
        # is genuinely unaffected by anything on this screen.
        await page_b.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        called_after_marquee_shown = await page_b.eval_on_selector_all(
            ".board-cell.called", "els => els.length"
        )
        assert called_after_marquee_shown > 0

        assert console_errors_b == [], f"JS errors while spectating: {console_errors_b}"
        await page_a.close()
        await page_b.close()
    finally:
        await engine.stop()
        # Longer than this file's usual 15s: this test's own setup work
        # (funding a user, a page reload, a second real browser
        # connection) takes several real seconds, during which
        # run_forever()'s own continuous lifecycle loop can complete and
        # restart multiple real rounds back to back at this room's fast
        # call_interval_ms -- stop() genuinely needs longer to reach a
        # safe checkpoint than a test with only one round ever plays out.
        await asyncio.wait_for(task, timeout=45)
        # platform_announcement is a real, shared singleton row -- leaving
        # it enabled would show this test's own placeholder text to every
        # other test (and any manual QA) against this database afterward.
        await pool.execute("UPDATE platform_announcement SET text = '', enabled = false WHERE id = 1")


async def _wait_for_class(locator, class_name: str, interval: float = 0.05) -> None:
    while True:
        classes = await locator.get_attribute("class")
        if class_name in (classes or ""):
            return
        await asyncio.sleep(interval)


async def test_miniapp_shows_a_retry_banner_instead_of_a_permanent_blank_screen_when_ws_never_connects(
    gateway_server, browser
):
    """A production incident (the Mini App opening to a permanently blank
    screen -- header visible, nothing else) traced to boot()'s own
    `await ws.waitForAuth()` hanging forever when the WebSocket can never
    be established at all (as opposed to opening and then getting a
    terminal close code, which was already handled). No screen in the
    raw HTML defaults to `active` (see this file's own module docstring),
    so a boot() that never completes leaves the page showing nothing but
    its own dark background forever -- exactly the reported symptom.

    Simulates a genuinely broken WS endpoint via Playwright's own
    route_web_socket() with a handler that never calls
    connect_to_server() -- confirmed empirically to make every real
    connection attempt fail immediately with a real (non-terminal)
    close code, the same shape a Cloudflare Tunnel WebSocket-upgrade
    misconfiguration or a firewall would produce from the client's own
    point of view. Runs against the real, unmodified 20s production
    timeout (INITIAL_AUTH_TIMEOUT_MS in ws.js) -- not shortened -- so
    this proves the actual production behavior, not a sped-up stand-in.
    """
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    async def never_connect(ws_route):
        pass  # deliberately never calls ws_route.connect_to_server()

    await page.route_web_socket("**/ws", never_connect)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")

    # The bug this test exists to catch: no screen ever becomes active,
    # and no banner ever appears -- the page just silently stays exactly
    # as blank as it started. Confirm the *opposite* happens instead.
    await page.wait_for_function(
        "document.getElementById('connection-banner').classList.contains('visible')", timeout=25000
    )
    assert await page.evaluate(
        "document.getElementById('connection-banner').classList.contains('actionable')"
    ), "the give-up banner must be tappable to retry, not just informational"
    banner_text = await page.text_content("#connection-banner")
    assert banner_text and banner_text.strip(), "banner must show real text, not stay empty"

    # Never reached "authenticated" or any active game screen -- the whole
    # point is this state is reached *instead of* a working boot, not
    # alongside it.
    assert not await page.evaluate("document.getElementById('screen-rooms').classList.contains('active')")

    await page.screenshot(path="/tmp/miniapp-connect-failed-banner.png")
    await page.close()
    await page.close()


async def test_room_card_is_reachable_and_activatable_by_keyboard_alone(
    gateway_server, browser, pool, redis, card_pool, conn
):
    # An architecture audit found room cards, the card-selection grid, and
    # the AUTO toggle were plain <div>s with only mouse/touch click
    # listeners -- nothing in the primary "join a game" flow reachable
    # without a pointer. This is the first control in that flow: a real
    # keyboard-only focus + Enter (no page.click() anywhere in this test)
    # must reach the lobby screen exactly the way a tap already does.
    #
    # A live RoundEngine is required here (matching
    # test_miniapp_full_gameplay_flow's own setup) -- state_sync reads
    # whatever round row currently exists for the room, and run_forever()
    # alone doesn't create one: RoundEngine.join() only starts a new round
    # (idle -> lobby) lazily, on the first actual join, so a room with an
    # engine attached but nobody joined yet still has no round at all. A
    # room with no engine attached has no round ever, so no click -- mouse
    # or keyboard -- would reach the lobby screen either way; the first
    # draft of this test learned that the hard way by confirming a plain
    # page.click() control case failed identically. Joining a second,
    # separate player directly through the engine (like the full-gameplay
    # test does) is what actually forces the round into "lobby" before the
    # browser ever loads the page.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)
        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)

        room_card = page.locator(room_selector)
        assert await room_card.get_attribute("tabindex") == "0"
        assert await room_card.get_attribute("role") == "button"

        await room_card.focus()
        assert await page.evaluate(
            f"document.activeElement === document.querySelector('{room_selector}')"
        )
        await page.keyboard.press("Enter")

        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        assert console_errors == [], f"JS errors during keyboard-only room entry: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_miniapp_language_uses_the_db_value_over_the_telegram_hint(
    gateway_server, browser, pool, conn
):
    # Spec 7.5: "The Mini App reads language_code as a hint but the DB
    # value wins." The stub always reports language_code "am"; after the
    # user row exists we set users.language to "en" directly (the same
    # thing the bot's own /language command would do) and reload -- a real
    # UI string must come back in English, proving the server's value,
    # not the client hint, decided what rendered.
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert user_row is not None, "authed handshake did not create the user row"
    await conn.execute("UPDATE users SET language = 'en' WHERE id = $1", user_row["id"])

    await page.reload()
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    await page.click("#open-wallet-btn")
    await page.wait_for_selector("#screen-wallet.active", timeout=5000)
    wallet_title = await page.text_content('[data-i18n="wallet.title"]')
    assert wallet_title == "Wallet", f"expected the DB's 'en' to win, got {wallet_title!r}"

    assert console_errors == [], f"JS errors on load: {console_errors}"
    await page.close()


async def test_miniapp_language_om_is_accepted_and_falls_back_to_english(
    gateway_server, browser, pool, conn
):
    # web/miniapp/locales/om.json is a deliberate empty stub (mirrors the
    # bot's own om.json/ti.json pattern -- spec 7.5 lists all four
    # languages as supported, but only am/en have real translations so
    # far). Before this, "om" wasn't in i18n.js's SUPPORTED list at all,
    # so a DB value of "om" would have been silently ignored rather than
    # falling through to English -- this proves both that "om" is now
    # accepted and that the fallback chain actually serves English text
    # for every key the stub doesn't define.
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert user_row is not None, "authed handshake did not create the user row"
    await conn.execute("UPDATE users SET language = 'om' WHERE id = $1", user_row["id"])

    await page.reload()
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    await page.click("#open-wallet-btn")
    await page.wait_for_selector("#screen-wallet.active", timeout=5000)
    wallet_title = await page.text_content('[data-i18n="wallet.title"]')
    assert wallet_title == "Wallet", f"expected om's empty stub to fall back to English, got {wallet_title!r}"

    assert console_errors == [], f"JS errors on load: {console_errors}"
    await page.close()


async def test_miniapp_full_gameplay_flow(gateway_server, browser, pool, redis, card_pool, conn):
    # is_active=True: the gateway's own room list (what the miniapp UI
    # browses) reads WHERE is_active = true, unlike every other test using
    # create_room() (see its own docstring for why False is the default).
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        # Fund the player directly through the ledger (deposits are Phase
        # 5-6, not built) -- then reload so the balance shown reflects it.
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        # Target this test's own room specifically -- the dev database
        # accumulates rooms across every previous test run in the session,
        # all at the same 10.00 ETB stake, so `.room-card` alone (the
        # *first* match) is non-deterministic about which room it hits.
        # That was a real, intermittent test bug, not app flakiness: the
        # click could land on some other, already-finished leftover room,
        # whose state_sync never reports status "lobby".
        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)

        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        assert len(cells) == 432

        # The lobby's own header/status bar (video reference: REFRESH,
        # BALANCE, CONNECTED, Stake, Win) -- real values from state_sync/
        # lobby_tick, not placeholders.
        await page.wait_for_function(
            "document.getElementById('lobby-balance-amount').textContent.includes('100.00')", timeout=5000
        )
        await page.wait_for_function(
            "document.getElementById('lobby-stake-amount').textContent.includes('10.00')", timeout=5000
        )
        assert "connected" in (
            await page.get_attribute("#lobby-connection-pill", "class") or ""
        )
        await page.screenshot(path="/tmp/miniapp-lobby.png")

        # Tapping a card number takes it immediately -- no separate
        # confirm step. #screen-game.active below, reachable only once
        # state_sync/round_start reports a real held card, is itself the
        # proof the take_card ack actually landed, not just that the tap
        # happened -- the balance assertion right after is the other half.
        await cells[9].click()  # card #10

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        board_cells = await page.query_selector_all(".board-cell")
        assert len(board_cells) == 75
        # Proof this player actually holds a card (not spectating): the
        # your-card section is the one thing enterSpectate() hides.
        assert not await page.is_hidden("#your-card-section")

        # Confirm the actual stake happened -- not just that the UI moved
        # on to the next screen.
        balance_after_stake = await pool.fetchval(
            """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
            """,
            user_row["id"],
        )
        assert balance_after_stake == Decimal("90.00")

        # Wait for at least one live call to actually render.
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )

        await page.screenshot(path="/tmp/miniapp-game.png")

        assert console_errors == [], f"JS errors during gameplay: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_verify_draw_button_shows_a_verified_seed(gateway_server, browser, pool, redis, card_pool, conn):
    """Spec section 14's definition of done: "a player can independently
    verify any round's draw from the published seed." This is that button,
    clicked for real, against a round that actually ran to completion --
    not just a check that /api/rounds/{id}/fairness returns the right JSON
    (test_gateway_rest.py already proves that), but that the UI a player
    would actually use to see it works.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        await page.click("#verify-draw-btn")
        await page.wait_for_selector("#fairness-panel:not(.hidden)", timeout=5000)
        await page.wait_for_function(
            "document.getElementById('fairness-verified').textContent.length > 0", timeout=10000
        )

        verified_text = await page.text_content("#fairness-verified")
        assert verified_text and "✅" in verified_text, f"draw did not verify: {verified_text!r}"

        seed_hex = await page.text_content("#fairness-seed")
        hash_hex = await page.text_content("#fairness-hash")
        assert seed_hex and len(seed_hex.strip()) == 64  # 32 bytes, hex-encoded
        assert hash_hex and len(hash_hex.strip()) == 64  # sha256, hex-encoded

        await page.screenshot(path="/tmp/miniapp-fairness.png")
        assert console_errors == [], f"JS errors during verify-draw flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_result_screen_shows_the_winning_card_preview(gateway_server, browser, pool, redis, card_pool, conn):
    """Video reference `20260902093014.mp4`'s winner modal (~t=69s):
    a full card preview, not just a text summary, with the winning
    pattern's cells visibly highlighted. round_engine.py's round_end
    broadcast now carries each winner's own grid (self._card_pool,
    already in memory for settlement -- see the commit adding it), and
    render/card.js's renderStaticCard() draws it. This proves the whole
    path end-to-end against a round that actually completes for real.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        # Whether this player won or the other did, round_end always
        # carries at least one winner's grid -- the panel must show it
        # either way (app.js's `shown = mine || winners[0]` logic).
        await page.wait_for_selector("#result-card-panel:not(.hidden)", timeout=10000)

        card_cells = await page.query_selector_all("#result-card .card-cell")
        assert len(card_cells) == 25, f"expected a full 5x5 card preview, got {len(card_cells)} cells"

        free_cell = await page.query_selector("#result-card .card-cell.free")
        assert free_cell is not None
        assert (await free_cell.text_content()) == "F"

        # A real win now needs >= bingo.MIN_WINNING_LINES (2) complete
        # lines, and the result screen highlights every one of them, not
        # just the first (render/card.js's renderStaticCard() unions all
        # comma-joined pattern names from round_engine.py's own
        # PendingWinner.pattern). Two rows or two columns never cross (10
        # distinct cells); every other combination -- row+col, anything
        # +diagonal, both diagonals -- crosses at exactly one cell (9).
        # Either way this can never be 5 (one line) or 0 (no highlight),
        # which is the actual thing this test needs to catch.
        result_meta_text = await page.text_content("#result-meta")
        winning_cells = await page.query_selector_all("#result-card .card-cell.winning")
        assert len(winning_cells) in (9, 10), (
            f"expected 9 or 10 cells highlighted for a real two-line win ({result_meta_text!r}), "
            f"got {len(winning_cells)}"
        )
        # Every highlighted cell must also actually be marked-called --
        # a winning cell that were somehow not "marked" would mean the
        # gold ring and the column-color fill visibly disagree.
        for cell in winning_cells:
            classes = await cell.get_attribute("class")
            assert "marked" in (classes or ""), f"winning cell not marked: {classes}"

        await page.screenshot(path="/tmp/miniapp-result-card.png")
        assert console_errors == [], f"JS errors during result-screen card preview: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_voice_announcement_requests_the_correct_audio_file_for_a_call(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """The Amharic voice-caller feature's own explicit closing bar --
    "test all 75 numbers and verify... the correct Bingo letter" -- for
    the one piece only a real browser can prove: that a live call
    actually triggers a request for the right
    /audio/calls/{LETTER}_{NN}.mp3 file. No real MP3s exist yet (see
    web/miniapp/audio/calls/README.md) -- this only needs to observe the
    *request* Playwright's own page.on("request") sees, not decode audio,
    so it proves the JS wiring is correct independent of whether real
    audio assets have been recorded.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        audio_requests: list[str] = []
        page.on("request", lambda req: audio_requests.append(req.url) if "/audio/calls/" in req.url else None)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        # Visual check of the settings panel this feature adds, not just
        # the request-interception assertions below.
        await page.click("#voice-settings-btn")
        await page.wait_for_selector("#voice-settings-panel:not(.hidden)", timeout=5000)
        await page.screenshot(path="/tmp/miniapp-voice-settings.png")
        await page.click("#voice-settings-btn")
        await page.wait_for_function(
            "document.getElementById('voice-settings-panel').classList.contains('hidden')", timeout=5000
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        # This test's own fast call_interval_ms=15 (real rooms space calls
        # seconds apart) makes voiceCaller's playback queue fall behind the
        # live call stream, since each missing clip only advances the
        # queue after a real network round-trip (a 404) -- so the *current*
        # call badge races ahead of what's actually been requested by now.
        # That's correct queueing behavior under this test's artificially
        # fast pace, not a bug -- wait for a handful of requests to land
        # instead of chasing the latest badge value.
        for _ in range(50):
            if len(audio_requests) >= 5:
                break
            await page.wait_for_timeout(100)

        assert audio_requests, "no /audio/calls/ requests observed during a live call sequence"
        for url in audio_requests:
            filename = url.rsplit("/", 1)[-1]
            stem = filename.removesuffix(".mp3")
            letter, number_str = stem.split("_")
            number = int(number_str)
            assert letter_for(number) == letter, (
                f"{filename}: requested letter {letter!r} but bingo.letter_for({number}) is {letter_for(number)!r}"
            )
        await page.screenshot(path="/tmp/miniapp-voice-game.png")
        assert console_errors == [], f"JS errors during voice announce flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_disabling_voice_makes_zero_audio_requests(gateway_server, browser, pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 2)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        audio_requests: list[str] = []
        page.on("request", lambda req: audio_requests.append(req.url) if "/audio/calls/" in req.url else None)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('0.00')", timeout=10000
        )

        # Turn voice off via the settings panel before ever joining a room.
        await page.click("#voice-settings-btn")
        await page.wait_for_selector("#voice-settings-panel:not(.hidden)", timeout=5000)
        await page.click("#voice-switch-settings")
        assert not await page.evaluate(
            "document.getElementById('voice-switch-settings').classList.contains('on')"
        )

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None, "authed handshake did not create the user row"
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )
        # The setting's own persistence (localStorage) must have survived
        # the reload above -- otherwise this test would prove nothing
        # about the setting actually being off during gameplay.
        assert await page.evaluate("localStorage.getItem('jobingo_voice_enabled')") == "false"
        # Cleared here, not tracked from page load: voiceCaller.preloadAll()
        # deliberately warms all 75 clips on a brand-new session's very
        # first boot (voice defaults on until a player says otherwise),
        # which legitimately requested every clip before this test ever
        # touched the toggle above. What this test actually needs to prove
        # is that *disabled* voice makes zero requests during real
        # gameplay from here on -- not that literally nothing was ever
        # fetched before the player expressed a preference.
        audio_requests.clear()

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)
        cells = await page.query_selector_all(".card-grid-cell")
        await cells[9].click()  # card #10 -- takes it immediately, no confirm step

        await page.wait_for_selector("#screen-game.active", timeout=25000)
        # Same proof-of-liveness the other gameplay tests use -- at least
        # one call actually rendered, so silence below is because voice is
        # off, not because no calls happened yet.
        await page.wait_for_function(
            "document.getElementById('call-badge').textContent.length > 0", timeout=15000
        )
        await page.wait_for_timeout(200)

        assert audio_requests == [], f"voice is off but audio was requested: {audio_requests}"
        assert console_errors == [], f"JS errors with voice disabled: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_player_can_hold_and_play_several_cards_at_once(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """Multi-card-per-player's own real-browser proof (graceful-snacking-
    quail.md's verification checklist: "take 2 cards, verify both render
    independently"). Selects two cards in the lobby in one batched take
    and confirms the game screen renders two fully independent cards,
    each with its own distinct grid and its own BINGO button -- not one
    card silently clobbered by the other, which is exactly the failure
    mode render/card.js's singleton-to-factory rewrite exists to prevent
    (the old module-level `cells`/`currentGrid` meant a *second*
    buildCard() call would have silently repointed every later mark/claim
    check at whichever card was built last).
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=300,
        is_active=True, max_cards_per_player=3,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 5)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        # Each tap takes the card immediately -- no separate confirm step.
        # The balance assertion right below is the real proof both stakes
        # actually landed, not just that the taps happened.
        await cells[9].click()  # card #10
        await cells[19].click()  # card #20

        # Two separate real stakes actually happened -- not just that the
        # UI moved on to the next screen. Polled, not a single immediate
        # fetch: each tap fires its take_card and moves on with no
        # confirm step to wait on anymore, so the async gateway ->
        # engine round trip may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("80.00"):
                break
            await asyncio.sleep(0.1)
        assert balance_after_stake == Decimal("80.00"), "expected two separate 10.00 ETB stakes"

        await page.wait_for_selector("#screen-game.active", timeout=25000)

        card_items = await page.query_selector_all(".your-card-item")
        assert len(card_items) == 2, f"expected 2 independently held cards, got {len(card_items)}"

        # Titles must actually distinguish which card is which (language-
        # agnostic check -- game.your_card_no interpolates the raw card
        # number the same way in every locale).
        titles = [await item.query_selector(".your-card-title") for item in card_items]
        title_texts = [(await t.text_content()) or "" for t in titles if t is not None]
        assert any("10" in txt for txt in title_texts), title_texts
        assert any("20" in txt for txt in title_texts), title_texts

        # Each card must have its own full 5x5 grid and its own BINGO
        # button, and the two grids must actually differ -- the concrete
        # proof the factory conversion isolated per-card state correctly.
        grids = []
        for item in card_items:
            grid_cells = await item.query_selector_all(".card-cell")
            assert len(grid_cells) == 25, f"expected a full 5x5 grid, got {len(grid_cells)}"
            grids.append([await c.text_content() for c in grid_cells])
            assert await item.query_selector(".bingo-btn") is not None

        assert grids[0] != grids[1], "both held cards rendered the same grid -- factory isolation broken"

        await page.screenshot(path="/tmp/miniapp-multi-card-game.png", full_page=True)

        # Let the round actually run to completion -- proves round_end's
        # rewritten multi-win handling (summing every one of this
        # player's own winning entries, not just the first) doesn't crash
        # regardless of which of the two players ends up winning.
        await page.wait_for_selector("#screen-result.active", timeout=90000)

        assert console_errors == [], f"JS errors during multi-card gameplay: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_multi_card_session_loss_reports_the_full_amount_not_one_card(
    gateway_server, browser, pool, redis, card_pool, conn
):
    """A code review pass caught that round_end's session reality-check
    loss branch subtracted a flat single-card `stake` even when the
    losing player held more than one card -- the untested mirror image
    of the winning-side multi-card bug this same feature already fixed
    (see DECISIONS.md's Phase 4 entry). A player holding 2 cards who
    loses both must see the full 2-card loss on the results screen's
    real-money disclosure, not half of it.

    AUTO is turned off for the browser player directly in the DB (the
    toggle itself only lives on the game screen, reachable after this
    round already starts) so their own two cards can never auto-claim
    regardless of what the draw does to them -- the filler player's own
    auto-claim is then the only way this round can end in a win, making
    "the browser player loses on both cards" a deterministic outcome to
    test against, not a race against their own cards' luck.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=8, call_interval_ms=15,
        is_active=True, max_cards_per_player=2,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    try:
        other_player = await create_funded_user(conn)
        assert (await engine.join(other_player, 5, auto_mark=True)).ok

        telegram_id = next_telegram_id()
        page, console_errors = await prepare_page(browser, telegram_id)

        http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
        await page.goto(http_base + "/")
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        assert user_row is not None
        await fund_user(conn, user_row["id"], Decimal("100.00"))
        await conn.execute(
            "UPDATE users SET auto_mark_preference = false WHERE id = $1", user_row["id"]
        )
        await page.reload()
        await page.wait_for_selector("#screen-rooms.active", timeout=10000)
        await page.wait_for_function(
            "document.getElementById('balance-amount').textContent.includes('100.00')", timeout=10000
        )

        room_selector = f'.room-card[data-room-id="{room_id}"]'
        await page.wait_for_selector(room_selector, timeout=10000)
        await page.click(room_selector)
        await page.wait_for_selector("#screen-lobby.active", timeout=10000)

        cells = await page.query_selector_all(".card-grid-cell")
        # Each tap takes the card immediately -- no separate confirm step.
        await cells[9].click()  # card #10
        await cells[19].click()  # card #20

        # Polled, not a single immediate fetch: with no confirm step to
        # wait on anymore, the async gateway -> engine round trip for
        # either tap may still be in flight the instant this runs.
        balance_query = """
            SELECT balance FROM account_balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.user_id = $1 AND a.kind = 'user_cash'
        """
        for _ in range(100):
            balance_after_stake = await pool.fetchval(balance_query, user_row["id"])
            if balance_after_stake == Decimal("80.00"):
                break
            await asyncio.sleep(0.1)
        assert balance_after_stake == Decimal("80.00"), "expected two separate 10.00 ETB stakes"

        await page.wait_for_selector("#screen-result.active", timeout=90000)

        session_text = await page.text_content("#result-session")
        assert "20.00" in (session_text or ""), (
            f"expected the full 2-card 20.00 ETB loss in the session total, got: {session_text!r}"
        )
        assert console_errors == [], f"JS errors during multi-card loss flow: {console_errors}"
        await page.close()
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_opening_directly_in_a_plain_browser_shows_an_accurate_message_not_a_generic_one(
    gateway_server, browser
):
    """Universal-compatibility audit finding (directive section 5): opening
    the Mini App's own URL directly in a plain browser -- no
    window.Telegram at all -- used to show "Unable to connect to the game
    server. Tap to retry.", which is simply false here (nothing about the
    network or the backend is broken) and whose only action reloads into
    the exact same dead end. This is the one real, reachable case that
    exercises boot()'s `!hasInitData` branch without any Telegram stub.
    """
    page = await browser.new_page(viewport={"width": 390, "height": 780})
    console_errors: list[str] = []
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    # No window.Telegram stub at all -- but the real SDK's own <script>
    # tag would otherwise try to fetch telegram.org over the network from
    # this sandbox and add flakiness/latency unrelated to what this test
    # verifies. It has nothing to overwrite here (there is no stub), so
    # blocking it only removes that network dependency, not the app's own
    # no-initData handling.
    async def block_real_sdk(route):
        await route.abort()

    await page.route(TELEGRAM_SDK_URL, block_real_sdk)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")

    await page.wait_for_selector("#boot-shell", timeout=10000)
    title = await page.text_content(".boot-shell-title")
    body = await page.text_content(".boot-shell-body")
    # boot() defaults to "am" when there's no Telegram user object to read
    # a language_code from at all -- so the real, full i18n resolution for
    # this exact no-Telegram case lands on the Amharic copy, not English.
    assert title == "ከቴሌግራም ይክፈቱ"
    assert "ቴሌግራም" in (body or "")
    # The old, inaccurate copy must not be present in any form.
    assert "መገናኘት አልተቻለም" not in (body or "")

    assert console_errors == [], f"JS errors on the no-initData boot path: {console_errors}"

    await page.screenshot(path="/tmp/miniapp-not-in-telegram.png")
    await page.close()


async def test_rooms_screen_invite_button_shares_a_real_referral_link(gateway_server, browser, conn):
    """The Rooms header's new Invite button (services/gateway/queries.py::
    invite_summary(), services/gateway/app.py's /api/invite) -- the same
    ?start=ref_{telegram_id} deep link and join count
    services/bot/handlers.py::cmd_invite() already sends over chat, now one
    tap away in the game itself instead of requiring a trip back to the bot.
    """
    telegram_id = next_telegram_id()
    page, console_errors = await prepare_page(browser, telegram_id)

    http_base = gateway_server.replace("ws://", "http://").replace("/ws", "")
    await page.goto(http_base + "/")
    await page.wait_for_selector("#screen-rooms.active", timeout=10000)

    referrer_id = await conn.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert referrer_id is not None
    for _ in range(2):
        await conn.execute(
            "INSERT INTO users (telegram_id, display_name, referred_by) VALUES ($1, $2, $3)",
            next_telegram_id(), "referred-friend", referrer_id,
        )

    await page.click("#invite-btn")
    await page.wait_for_selector("#invite-panel:not(.hidden)", timeout=5000)
    await page.wait_for_function(
        "document.getElementById('invite-count').textContent.includes('2')", timeout=10000
    )

    await page.click("#invite-share-btn")
    opened = await page.evaluate("window.__openedTelegramLinks")
    assert len(opened) == 1, f"expected exactly one share-sheet open, got {opened!r}"
    assert opened[0].startswith("https://t.me/share/url?")
    assert f"ref_{telegram_id}" in opened[0]
    assert "aradabingo_test_bot" in opened[0]

    assert console_errors == [], f"JS errors during invite flow: {console_errors}"
    await page.close()
