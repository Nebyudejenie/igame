import asyncio
import itertools
import json
import os
import random
import socket
import uuid
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
import uvicorn

# Fixed before anything imports packages.core.config: get_settings() is
# lru_cache'd, so whatever TELEGRAM_BOT_TOKEN is set to the first time it's
# read is what the whole suite gets -- including the gateway app under test,
# which needs a token the test's own initData-building helper also knows.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token-for-suite")
# Same reasoning for the payments app under test: it builds a real
# ChapaProvider from settings.chapa_api_key at startup, so a webhook test
# needs to know the exact secret to sign its test payloads with.
os.environ.setdefault("CHAPA_API_KEY", "test-chapa-secret-for-suite")
# The bot's own webhook-registration base -- unrelated to deposit
# availability since the callback_url/return_url split (see
# DECISIONS.md), but still read by services/bot/app.py's own set_webhook().
os.environ.setdefault("PUBLIC_BASE_URL", "https://bot.test")
# gateway/app.py's /api/deposit route (and services/payments/
# availability.py's chapa_deposit_configured check the bot's /deposit
# command also relies on) refuses to start a deposit at all unless both
# of these are configured -- same "not available yet" discipline as
# every other empty-setting gate in this codebase -- tests need both set.
os.environ.setdefault("MINIAPP_URL", "https://app.test")
os.environ.setdefault("PAYMENTS_PUBLIC_BASE_URL", "https://payments.test")
# packages/core/phone_crypto.py has no safe empty default (registration
# cannot function without it) -- a fixed, obviously-not-production key so
# every test run derives the exact same encryption/lookup subkeys, the
# same reasoning TELEGRAM_BOT_TOKEN above is fixed rather than randomized.
# Generated fresh by a production-readiness security pass (`python -c
# "import secrets; print(secrets.token_hex(32))"`) specifically to stop
# reusing the value that had briefly leaked into .env.example's own git
# history (removed from HEAD, but a value once committed is recoverable
# from history forever regardless) -- this one has never appeared in any
# committed file anywhere, so it carries no such residual exposure.
os.environ.setdefault(
    "PHONE_ENCRYPTION_KEY", "27700ffefaa807ec0441662e1efb8c8e262152ed804dc7e45f29abf936d3d6cc"
)
# The payments app under test builds its /internal/telebirr/ingest bearer
# check from settings.macrodroid_ingest_token at request time -- a fixed,
# obviously-not-production value so a test can sign requests with the
# exact secret the running payments_server instance was configured with.
os.environ.setdefault("MACRODROID_INGEST_TOKEN", "test-macrodroid-token-for-suite")

from packages.core import ledger
from packages.core.config import get_settings
from packages.core.notifications import NOTIFICATIONS_STREAM
from packages.core.redis_conn import get_redis
from services.engine.round_engine import load_card_pool
from services.payments.withdrawals import PAYOUT_STREAM

# Every test that needs a fresh user picks the next id off this counter,
# seeded randomly so re-runs of the suite never collide with leftover rows
# from a previous run against the same database.
_telegram_id_counter = itertools.count(random.randint(10**9, 2 * 10**9))
_phone_counter = itertools.count(random.randint(10_000_000, 20_000_000))


def next_telegram_id() -> int:
    return next(_telegram_id_counter)


def unique_phone() -> str:
    # phone_e164 is UNIQUE at the database level -- every test that
    # registers a user with a phone number needs its own, the same as it
    # needs its own telegram_id.
    return f"+2519{next(_phone_counter):08d}"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool():
    settings = get_settings()
    p = await asyncpg.create_pool(dsn=settings.database_url, min_size=5, max_size=50)
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def conn(pool):
    async with pool.acquire() as connection:
        yield connection


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def redis():
    r = get_redis()
    yield r
    await r.aclose()


@pytest_asyncio.fixture(autouse=True)
async def clean_payout_stream(redis):
    """The 'payouts' Redis Stream (services/payments/withdrawals.py) is
    real, shared, session-lived state -- a withdrawal test can legitimately
    enqueue a real job onto it without ever consuming it. Clearing it before
    every test keeps payout_worker.process_next()'s "the next job"
    unambiguous, the same reasoning next_telegram_id()/unique_phone() exist
    for uniqueness collisions, just applied to a shared queue instead.
    """
    await redis.delete(PAYOUT_STREAM)


@pytest_asyncio.fixture(autouse=True)
async def clean_notifications_stream(redis):
    """Same reasoning as clean_payout_stream, for
    packages/core/notifications.py's 'bot_notifications' stream -- a
    deposit/withdrawal test can enqueue a real notification without ever
    consuming it, and a stale one from an earlier test was confirmed to
    make notification_relay.process_next() deliver the wrong message to
    the wrong chat in a later test before this fixture existed.
    """
    await redis.delete(NOTIFICATIONS_STREAM)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def card_pool(pool):
    return await load_card_pool(pool)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def gateway_server():
    """Runs the real gateway app (services/gateway/app.py) via uvicorn, on
    this same event loop -- not a subprocess, not a separate thread, so
    there's no cross-event-loop asyncpg/redis connection hazard. Tests
    connect to it as a genuine WebSocket client would.
    """
    from services.gateway.app import app as gateway_app

    port = _free_port()
    config = uvicorn.Config(gateway_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("gateway server did not start in time")

    yield f"ws://127.0.0.1:{port}/ws"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def admin_server():
    """Runs the real admin API (services/admin/app.py) via uvicorn, on this
    same event loop -- same rationale as gateway_server: tests exercise the
    genuine HTTP/RBAC/dependency-injection stack, not an in-process shortcut.
    """
    from services.admin.app import app as admin_app

    port = _free_port()
    config = uvicorn.Config(admin_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("admin server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def sms_server():
    """Runs the real SMS Control Plane API (services/sms/app.py) via
    uvicorn -- same rationale as gateway_server/admin_server: both the
    admin-facing routes and the node protocol are tested as genuine HTTP
    endpoints, not in-process function calls.
    """
    from services.sms.app import app as sms_app

    port = _free_port()
    config = uvicorn.Config(sms_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("sms server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def payments_server():
    """Runs the real payments API (services/payments/app.py) via uvicorn --
    same rationale as gateway_server/admin_server: the webhook route is the
    one HTTP surface a payment provider's server actually reaches, so it's
    tested as a genuine HTTP endpoint, not an in-process function call.
    """
    from services.payments.app import app as payments_app

    port = _free_port()
    config = uvicorn.Config(payments_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("payments server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


def _find_fallback_chromium() -> str | None:
    """Playwright's default launch() wants a small 'headless shell' binary;
    some environments only manage to download the full Chromium build (a
    flaky/rate-limited network can abort the headless-shell download
    partway through while the earlier, larger Chromium download already
    succeeded). If the full build is present, use it directly rather than
    failing outright.
    """
    import glob
    import pathlib

    cache = pathlib.Path.home() / ".cache" / "ms-playwright"
    matches = sorted(glob.glob(str(cache / "chromium-*" / "chrome-linux64" / "chrome")))
    return matches[-1] if matches else None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            instance = await p.chromium.launch(args=["--no-sandbox"])
        except Exception:
            fallback = _find_fallback_chromium()
            if fallback is None:
                raise
            instance = await p.chromium.launch(executable_path=fallback, args=["--no-sandbox"])
        yield instance
        await instance.close()


def build_init_data(telegram_id: int, *, first_name: str = "Test", auth_date: int | None = None) -> str:
    """Builds a correctly HMAC-signed initData string against this test
    session's TELEGRAM_BOT_TOKEN, the same way a real Telegram client would.
    """
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    bot_token = get_settings().telegram_bot_token
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": f"AAQ{uuid.uuid4().hex[:16]}",
        "user": json.dumps({"id": telegram_id, "first_name": first_name}),
    }
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def create_user(conn: asyncpg.Connection) -> int:
    telegram_id = next_telegram_id()
    row = await conn.fetchrow(
        """
        INSERT INTO users (telegram_id, display_name)
        VALUES ($1, $2)
        RETURNING id
        """,
        telegram_id,
        f"test-user-{telegram_id}",
    )
    return row["id"]


async def fund_user(conn: asyncpg.Connection, user_id: int, amount: Decimal) -> None:
    """Deposits amount into user_id's cash balance via a real ledger
    transaction -- joining a round debits user_cash for real, so a fresh
    test user needs real money in it first, the same as production.
    """
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    provider = await ledger.get_or_create_account(conn, None, "provider_settlement")
    await ledger.post(
        conn,
        "deposit",
        [ledger.Entry(provider.id, -amount), ledger.Entry(cash.id, amount)],
        idempotency_key=f"test-fund-{user_id}-{uuid.uuid4()}",
    )


async def create_funded_user(conn: asyncpg.Connection, amount: Decimal = Decimal("1000.00")) -> int:
    user_id = await create_user(conn)
    await fund_user(conn, user_id, amount)
    return user_id


async def recv_balance_update(redis, user_id: int, trigger) -> dict:
    """Subscribes to this user's live balance channel (packages.core.ledger
    .publish_balance_update()'s target, the same one services/gateway
    /connection.py subscribes a real WebSocket connection to at handshake),
    awaits `trigger()` -- the action expected to push an update -- and
    returns the decoded payload. Talks to Redis pub/sub directly rather
    than through a full WebSocket + gateway harness, for callers (round
    engine, withdrawal, payout tests) that already have `redis` in hand
    and don't otherwise need a live connection.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"user:{user_id}")
    try:
        await trigger()
        # get_message(ignore_subscribe_messages=True) only polls *once* --
        # if that single poll happens to read the subscribe-ack (a real
        # race here, not hypothetical: it's still sitting unread on the
        # socket from the subscribe() call above), it discards it and
        # returns None for that call rather than continuing to wait out
        # the rest of `timeout` for a real message. Loop against an
        # overall deadline instead of trusting one call to find it.
        deadline = asyncio.get_running_loop().time() + 5.0
        message = None
        while asyncio.get_running_loop().time() < deadline:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message is not None:
                break
        assert message is not None, f"no balance_update seen on user:{user_id}"
        payload = json.loads(message["data"])
        assert payload["t"] == "balance_update"
        return payload
    finally:
        await pubsub.unsubscribe(f"user:{user_id}")
        await pubsub.aclose()


async def create_room(
    conn: asyncpg.Connection,
    *,
    stake: Decimal = Decimal("20.00"),
    house_cut_bps: int = 2000,
    min_players: int = 2,
    max_players: int = 100,
    lobby_seconds: int = 1,
    call_interval_ms: int = 20,
    result_seconds: int = 0,
    win_patterns: list[str] | None = None,
    is_active: bool = False,
    max_cards_per_player: int = 1,
    no_player_next_round_delay_seconds: int = 1,
    min_winning_lines: int = 2,
) -> int:
    """Fast-timing test room by default -- lobby closes in 1s, a number is
    called every 20ms (so all 75 calls take ~1.5s worst case), and there's
    no lingering result display. Override per test where the timing itself
    is what's under test.

    is_active defaults to False, deliberately overriding the schema's own
    `DEFAULT true` -- a real code review pass caught that every one of the
    dozens of tests calling this helper was silently leaving its room
    `is_active = true` forever (nothing here or in any test ever flips it
    back), and no test actually needs that: every one of them reaches its
    own room by the id this function already returns, never through
    services/engine/worker.py's `WHERE is_active = true` scan. This
    session's shared dev database had accumulated 3092 such rows before
    that was caught, and run_active_rooms() (the only thing that ever
    queries that column in bulk) trying to claim all of them at once was
    enough to genuinely exhaust a real Redis client's connection pool
    during a full test run -- not just slow, an actual test failure. Pass
    is_active=True explicitly for the one or two tests that specifically
    exercise that scan (or dashboard_summary()'s own active_rooms count).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO rooms
            (code, stake, house_cut_bps, min_players, max_players,
             lobby_seconds, call_interval_ms, result_seconds, win_patterns, is_active,
             max_cards_per_player, no_player_next_round_delay_seconds, min_winning_lines)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        f"test-room-{uuid.uuid4()}",
        stake,
        house_cut_bps,
        min_players,
        max_players,
        lobby_seconds,
        call_interval_ms,
        result_seconds,
        json.dumps(win_patterns if win_patterns is not None else ["row", "col", "diag"]),
        is_active,
        max_cards_per_player,
        no_player_next_round_delay_seconds,
        min_winning_lines,
    )
    return row["id"]


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _assert_ledger_reconciles_at_suite_end(pool):
    """Permanent regression guard for the 2026-09-17 incident (see
    DECISIONS.md and migrations/versions/b8e4a1f0c3d7_ledger_append_only.py):
    whatever else ran during this test session, the ledger's cached
    balances must agree with its own entries by the time it ends. Runs
    once, in teardown, after every other test in the session has already
    run -- autouse+session-scoped so it applies with zero opt-in from any
    individual test file, the same reasoning the append-only trigger
    itself doesn't depend on every future call site remembering to be
    careful. A failure here means some test (or, as this incident showed,
    some out-of-band manual action) left the ledger inconsistent -- fix
    the root cause, never silence this assertion.
    """
    yield
    async with pool.acquire() as conn:
        mismatches = await ledger.reconcile(conn)
    assert mismatches == [], (
        f"ledger.reconcile() found {len(mismatches)} mismatched account(s) at test-suite end: {mismatches!r} -- "
        "the ledger's cached balances no longer agree with its own entries. This must never be silenced; "
        "find and fix the root cause (see DECISIONS.md's 2026-09-17 entry for the last time this happened)."
    )
