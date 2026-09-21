"""End-to-end bot handler tests: a real aiogram Dispatcher/Router, real
Postgres and Redis, and a fake Telegram Bot API session so nothing ever
touches the network. This is what proves the spec's own Prompt 5 test
list: a contact from a different user is rejected, a typed phone number is
rejected with the correct re-prompt, and a duplicate update_id is
processed exactly once.
"""

import asyncio
import itertools
import random
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.send_message import SendMessage
from aiogram.types import Chat, Contact, Message, PhotoSize, TelegramObject, Update, User

from packages.core import ledger
from packages.core.config import Settings
from packages.core.phone_crypto import decrypt_phone
from services.admin import queries as admin_queries
from services.bot.app import build_dispatcher
from services.bot.notifier import Notifier
from services.payments import manual
from services.payments.telebirr_ingest import ingest_sms_evidence
from tests.integration.conftest import fund_user, next_telegram_id
from tests.integration.test_admin_auth import create_test_admin

# Randomized start, not itertools.count(1): update_ids feed the dedup
# middleware's Redis keys (seen:tg:{update_id}, 10 minute TTL). A counter
# that restarts from 1 on every pytest invocation would collide with
# still-live dedup keys from a run moments earlier and get silently
# dropped as "already seen" -- exactly the bug this comment is here to
# stop someone from reintroducing.
_id_counter = itertools.count(random.randint(10**8, 2 * 10**8))
_phone_counter = itertools.count(random.randint(10_000_000, 20_000_000))


def unique_phone() -> str:
    # phone_e164 is UNIQUE at the database level -- every test that
    # registers a user needs its own number, the same as it needs its own
    # telegram_id.
    return f"+2519{next(_phone_counter):08d}"


class FakeSession(BaseSession):
    """Records every SendMessage instead of hitting Telegram's real API."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendMessage):
            self.sent.append(method)
            return Message(
                message_id=next(_id_counter),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        raise NotImplementedError(f"FakeSession doesn't support {type(method).__name__}")

    async def stream_content(self, *args: object, **kwargs: object):
        raise NotImplementedError
        yield b""  # pragma: no cover -- makes this a generator function


def make_text_update(
    telegram_id: int, text: str, *, first_name: str = "Test", language_code: str | None = None
) -> Update:
    user = User(id=telegram_id, is_bot=False, first_name=first_name, language_code=language_code)
    message = Message(
        message_id=next(_id_counter),
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=next(_id_counter), message=message)


def make_contact_update(
    telegram_id: int,
    *,
    contact_user_id: int | None,
    phone: str,
    first_name: str = "Test",
    language_code: str | None = None,
) -> Update:
    user = User(id=telegram_id, is_bot=False, first_name=first_name, language_code=language_code)
    contact = Contact(phone_number=phone, first_name=first_name, user_id=contact_user_id)
    message = Message(
        message_id=next(_id_counter),
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=user,
        contact=contact,
    )
    return Update(update_id=next(_id_counter), message=message)


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(token="123456:FAKE-TEST-TOKEN", session=session)
    return bot, session


async def _settle(messages: int = 1) -> None:
    import asyncio

    # notifier.py rate-limits successful sends to one every
    # MIN_INTERVAL_SECONDS (1/25s) -- a handler that queues more than one
    # message needs proportionally more real wall-clock time for all of
    # them to actually drain, not just the fixed budget a single-message
    # handler needs. Pass the number of messages a given call under test
    # actually sends; every existing call site keeps the same 0.05s
    # default it always had.
    await asyncio.sleep(0.05 + max(0, messages - 1) * 0.05)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def bot_setup(pool, redis):
    """One Dispatcher for the whole test file/session.

    aiogram's Router can only ever be attached to one Dispatcher for its
    lifetime (services/bot/handlers.py's `router` is a module-level
    singleton, the normal aiogram pattern) -- building a fresh Dispatcher
    per test would try to re-attach the same router repeatedly and aiogram
    correctly refuses that. One shared Dispatcher matches how the bot
    actually runs in production anyway (one long-lived instance).
    """
    settings = Settings(
        telegram_bot_token="123456:FAKE-TEST-TOKEN",
        # Truthy by default -- most of this file's deposit-command tests
        # need Chapa deposits to actually be "available" (services/
        # payments/availability.py's chapa_deposit_configured now requires
        # both miniapp_url and payments_public_base_url, the latter
        # already resolving from conftest.py's PAYMENTS_PUBLIC_BASE_URL
        # env default). The two tests that specifically want this empty
        # (test_play_command_reports_not_available_when_miniapp_url_unset,
        # test_deposit_shows_not_available_when_no_provider_and_no_
        # miniapp_url) monkeypatch it back to "" for themselves.
        miniapp_url="https://miniapp.test",
        telegram_bot_username="jobingo_bot",
        agent_portal_base_url="https://agent.test",
    )
    bot, session = make_bot()
    notifier = Notifier(bot)
    notifier.start()
    dp = build_dispatcher(pool, redis, notifier, settings)
    yield dp, bot, session
    await notifier.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def bot_ctx(bot_setup):
    dp, bot, session = bot_setup
    session.sent.clear()
    return dp, bot, session


async def test_typed_phone_number_is_rejected_with_reprompt(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_text_update(telegram_id, "0912345678")
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "Share Phone Number" in session.sent[0].text or "ስልክ ቁጥር አጋራ" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert row is None  # typing a number must never register anyone


async def test_contact_from_a_different_user_is_rejected(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    someone_elses_id = next_telegram_id()
    update = make_contact_update(telegram_id, contact_user_id=someone_elses_id, phone=unique_phone())
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "not your own" in session.sent[0].text or "የእርስዎ አይደለም" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert row is None


async def test_valid_contact_completes_registration(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    phone = unique_phone()
    update = make_contact_update(
        telegram_id, contact_user_id=telegram_id, phone=phone, first_name="Nebyu"
    )
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "Nebyu" in session.sent[0].text

    row = await pool.fetchrow(
        "SELECT phone_e164_encrypted, display_name FROM users WHERE telegram_id = $1", telegram_id
    )
    assert row is not None
    assert decrypt_phone(bytes(row["phone_e164_encrypted"])) == phone
    assert row["display_name"] == "Nebyu"


async def test_registration_success_uses_the_stored_default_language_not_the_clients_hint(pool, bot_ctx):
    # Real bug (2026-09-14): on_contact() used to build its success
    # message and main menu from the sender's raw Telegram language_code,
    # not from users.language (defaults to 'am' at the schema level --
    # migration 81d041ff4513 -- and is what every other command, plus
    # this same reply-keyboard's own text-matching in on_menu_text,
    # actually reads). A brand-new user whose device reports
    # language_code="en" (common regardless of the owner's real language)
    # saw an English success message and an English main menu, whose
    # button labels then matched nothing in on_menu_text's Amharic-keyed
    # mapping -- every reply-keyboard button silently did nothing until
    # the player typed a slash command like /play directly, which
    # bypasses text-matching and "auto-fixed" the language on the spot.
    # This proves both halves: the message language, and that the
    # rendered keyboard's own button actually works when pressed.
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    phone = unique_phone()
    update = make_contact_update(
        telegram_id, contact_user_id=telegram_id, phone=phone, first_name="Nebyu", language_code="en"
    )
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    sent = session.sent[0]
    assert sent.text == "እንኳን ደስ አለዎት፣ Nebyu! ምዝገባዎ ተጠናቅቋል።"
    assert sent.reply_markup is not None
    button_texts = {button.text for row in sent.reply_markup.keyboard for button in row}
    assert "▶️ ይጀምሩ" in button_texts

    session.sent.clear()
    await dp.feed_update(bot, make_text_update(telegram_id, "▶️ ይጀምሩ"))
    await _settle(messages=2)
    assert len(session.sent) == 2
    assert "Nebyu" in session.sent[0].text
    assert "Nebyu" in session.sent[1].text


async def test_contact_with_an_unrecognizable_phone_number_is_rejected(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone="123")
    await dp.feed_update(bot, update)
    await _settle()

    assert len(session.sent) == 1
    assert "valid Ethiopian phone" in session.sent[0].text or "ትክክለኛ የኢትዮጵያ ስልክ" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    assert row is None


async def test_contact_reusing_an_already_registered_phone_is_rejected(pool, bot_ctx):
    # A real UniqueViolationError on phone_lookup_hash, not a contrived
    # one: register a first user with a specific phone, then have a
    # second, different telegram account try to register with that exact
    # same number.
    dp, bot, session = bot_ctx
    phone = unique_phone()
    first_telegram_id = next_telegram_id()
    await dp.feed_update(
        bot,
        make_contact_update(first_telegram_id, contact_user_id=first_telegram_id, phone=phone),
    )
    await _settle()
    session.sent.clear()

    second_telegram_id = next_telegram_id()
    await dp.feed_update(
        bot,
        make_contact_update(second_telegram_id, contact_user_id=second_telegram_id, phone=phone),
    )
    await _settle()

    assert len(session.sent) == 1
    assert "Something went wrong" in session.sent[0].text or "የሆነ ስህተት" in session.sent[0].text

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", second_telegram_id)
    assert row is None


async def test_start_command_welcomes_back_an_already_registered_user(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/start"))
    await _settle(messages=2)

    # Two messages, not one: a real production incident found a
    # player's client still showing a stale persistent keyboard from
    # before MINIAPP_URL was ever configured, which never opened the
    # Mini App no matter how many times the bot resent a visually-
    # identical-looking keyboard -- _send_refreshed_main_menu() now
    # always precedes the real keyboard with an explicit
    # ReplyKeyboardRemove to force the client to actually discard
    # whatever it was holding onto. Both messages carry the same
    # welcome text; only the keyboard attached differs.
    assert len(session.sent) == 2
    assert "Welcome back" in session.sent[0].text or "እንኳን ደህና መጡ" in session.sent[0].text
    assert "Welcome back" in session.sent[1].text or "እንኳን ደህና መጡ" in session.sent[1].text


async def test_start_command_also_sends_a_direct_link_fallback_when_short_name_is_configured(
    bot_ctx, monkeypatch
):
    # Same fallback as /play (see that test's own comment for the full
    # production incident this is about) -- /start's returning-user
    # welcome sends the same main_menu_keyboard() button and should carry
    # the same fallback link.
    dp, bot, session = bot_ctx
    monkeypatch.setattr(dp["settings"], "telegram_miniapp_short_name", "arada")
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/start"))
    await _settle(messages=3)

    # 3 messages: the keyboard-refresh pair (remove, then real keyboard),
    # then the direct-link fallback.
    assert len(session.sent) == 3
    assert "https://t.me/jobingo_bot/arada" in session.sent[2].text


async def test_start_button_press_welcomes_back_an_already_registered_user(pool, bot_ctx):
    # main_menu_keyboard()'s first button is now "Start" (replacing the
    # old web_app "Play" button -- launching the Mini App moved to the
    # bot's chat-menu button and the /play command instead). Pressing it
    # must behave exactly like typing /start for an already-registered
    # user: on_menu_text() dispatches MenuAction.START to the real
    # cmd_start() handler via a synthetic CommandObject, not a
    # reimplementation of the welcome-back logic.
    from services.bot.i18n import t

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    # _register leaves the user's language at "am" (make_contact_update's
    # fake sender carries no language_code).
    button_text = t("menu.start", "am")
    await dp.feed_update(bot, make_text_update(telegram_id, button_text))
    await _settle(messages=2)

    assert len(session.sent) == 2
    assert "እንኳን ደህና መጡ" in session.sent[0].text
    assert "እንኳን ደህና መጡ" in session.sent[1].text


async def test_referral_credit_survives_a_failed_registration_attempt(pool, bot_ctx):
    # Regression: a real code review pass caught that the pending referral
    # was popped (deleted from Redis) *before* attempting registration,
    # so a retryable failure (contact mismatch, invalid phone -- both
    # explicitly designed to let the user try again) silently lost the
    # referral credit on the very next, successful attempt, with no error
    # surfaced to anyone.
    dp, bot, session = bot_ctx
    referrer_telegram_id = await _register(dp, bot, session)
    referrer_id = await pool.fetchval(
        "SELECT id FROM users WHERE telegram_id = $1", referrer_telegram_id
    )

    new_telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(new_telegram_id, f"/start ref_{referrer_telegram_id}"))
    await _settle()
    session.sent.clear()

    # A mismatched contact -- a retryable failure that must not consume
    # the pending referral.
    someone_elses_id = next_telegram_id()
    await dp.feed_update(
        bot, make_contact_update(new_telegram_id, contact_user_id=someone_elses_id, phone=unique_phone())
    )
    await _settle()
    session.sent.clear()

    row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", new_telegram_id)
    assert row is None  # the failed attempt must not have registered anyone

    # The real, valid contact -- this must still get credit to the
    # original referrer, not register with no referrer at all.
    await dp.feed_update(
        bot,
        make_contact_update(new_telegram_id, contact_user_id=new_telegram_id, phone=unique_phone()),
    )
    await _settle()

    row = await pool.fetchrow(
        "SELECT referred_by FROM users WHERE telegram_id = $1", new_telegram_id
    )
    assert row is not None
    assert row["referred_by"] == referrer_id


async def test_duplicate_update_id_processed_exactly_once(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, update)
    await dp.feed_update(bot, update)  # same update_id, fed again
    await _settle()

    assert len(session.sent) == 1  # not 2

    count = await pool.fetchval("SELECT count(*) FROM users WHERE telegram_id = $1", telegram_id)
    assert count == 1


async def test_start_shows_registration_prompt_for_new_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_text_update(telegram_id, "/start")
    await dp.feed_update(bot, update)
    await _settle()

    combined = " ".join(m.text or "" for m in session.sent)
    assert "register" in combined.lower() or "መመዝገብ" in combined or "ስልክ" in combined


async def test_start_registration_prompt_includes_the_18_plus_declaration(bot_ctx):
    # spec section 12's age gate: the registration prompt a brand-new user
    # sees before sharing their contact must state the 18+ requirement,
    # not just silently register anyone who taps the button.
    #
    # cmd_start() sends two messages back to back (welcome, then the
    # registration prompt this test cares about), and Notifier enforces a
    # real inter-message pacing gap (services/bot/notifier.py's own
    # MIN_INTERVAL_SECONDS) before the second is actually dequeued and
    # sent -- _settle()'s fixed short sleep is fine for every other,
    # single-message assertion in this file, but two messages need a real
    # deadline poll, not a fixed guess, especially under this sandbox's
    # documented host contention.
    import asyncio

    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    update = make_text_update(telegram_id, "/start")
    await dp.feed_update(bot, update)

    deadline = asyncio.get_running_loop().time() + 2.0
    while len(session.sent) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)

    combined = " ".join(m.text or "" for m in session.sent)
    assert "18" in combined


async def test_balance_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/balance"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_balance_reflects_real_ledger_state(pool, conn, bot_ctx):
    from tests.integration.conftest import fund_user

    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    contact_update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, contact_update)
    await _settle()

    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("75.00"))

    session.sent.clear()
    balance_update = make_text_update(telegram_id, "/balance")
    await dp.feed_update(bot, balance_update)
    await _settle()

    assert len(session.sent) == 1
    assert "75.00" in session.sent[0].text


async def test_balance_command_increments_real_per_command_metrics(pool, conn, bot_ctx):
    """Telegram command latency diagnosis pass: proves perf.perf_middleware
    is actually wired into the real dispatcher end to end -- feeding a
    genuine Update through it (not calling perf_middleware directly, that's
    tests/unit/test_perf.py's job) and checking the same metrics a real
    Prometheus scrape of the bot's own /metrics endpoint would report.
    """
    from packages.core import metrics
    from tests.integration.conftest import fund_user

    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(
        bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    )
    await _settle()
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("10.00"))

    commands_before = metrics.telegram_commands_total.labels(handler="cmd_balance")._value.get()
    success_before = metrics.telegram_command_success_total.labels(handler="cmd_balance")._value.get()
    latency_sum_before = metrics.telegram_command_latency_seconds.labels(handler="cmd_balance")._sum.get()
    api_sum_before = metrics.telegram_api_duration_seconds._sum.get()

    session.sent.clear()
    await dp.feed_update(bot, make_text_update(telegram_id, "/balance"))
    await _settle()

    assert len(session.sent) == 1
    assert metrics.telegram_commands_total.labels(handler="cmd_balance")._value.get() == commands_before + 1
    assert (
        metrics.telegram_command_success_total.labels(handler="cmd_balance")._value.get()
        == success_before + 1
    )
    # A real handler execution always takes *some* measurable time (at
    # minimum, the ledger.user_balance_snapshot() query) -- the sum only
    # ever moves forward.
    assert metrics.telegram_command_latency_seconds.labels(handler="cmd_balance")._sum.get() > latency_sum_before
    # This test's own bot_ctx uses a FakeSession (no real network), but
    # Notifier._run() still times its own await self._bot.send_message()
    # call around that fake response -- proving the instrumentation added
    # to notifier.py fires for every real code path through Notifier, not
    # only when a genuine Telegram API round trip is involved.
    assert metrics.telegram_api_duration_seconds._sum.get() > api_sum_before


async def _register(dp, bot, session) -> int:
    telegram_id = next_telegram_id()
    contact_update = make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
    await dp.feed_update(bot, contact_update)
    await _settle()
    session.sent.clear()
    return telegram_id


async def test_deposit_command_rate_limited_after_five_in_a_row(pool, bot_ctx, monkeypatch):
    # bot_setup's shared Settings has real-looking (but fake) Chapa
    # credentials -- tests/integration/conftest.py sets them so the
    # payments app's lifespan can construct a provider at all -- so
    # cmd_deposit's own real ChapaProvider(settings.chapa_api_key) would
    # otherwise make a genuine, slowly-timing-out network call (confirmed
    # directly: ConnectTimeout after several seconds). cmd_deposit
    # constructs the provider inline rather than taking it as an injected
    # dependency, so the only way to exercise the real
    # create_deposit_intent() path here without touching the network is
    # to monkeypatch the class itself, the same fake-the-network-boundary
    # discipline every other provider-touching test in this codebase uses.
    #
    # This proves two real things through the actual bot dispatch path,
    # not just through deposits.py directly: cmd_deposit's new
    # `redis: Redis` parameter resolves correctly via aiogram's real
    # dependency injection (a wiring mistake here would raise, not
    # silently no-op), and the spec section 9.2 "deposit 5/hour" rate
    # limit genuinely blocks a 6th rapid attempt end to end.
    class _FakeChapaProvider:
        # A class attribute, matching the real ChapaProvider's own shape
        # -- services/bot/handlers.py's availability check reads
        # ChapaProvider.name off the class itself, before any instance
        # exists, the same way it reads it off the real class.
        name = "chapa"

        def __init__(self, api_key: str) -> None:
            pass

        async def create_checkout(self, *, amount, user_ref, our_ref, return_url, callback_url):
            from services.payments.provider import CheckoutResult

            return CheckoutResult(
                checkout_url=f"https://pay.test/{our_ref}", provider_ref=our_ref, raw_response={}
            )

    monkeypatch.setattr("services.bot.handlers.ChapaProvider", _FakeChapaProvider)

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    for i in range(5):
        await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
        await _settle()
        assert len(session.sent) == 1, f"attempt {i + 1}: {session.sent}"
        assert "Tap below" in session.sent[0].text or "ከታች ያለውን ይጫኑ" in session.sent[0].text, (
            f"attempt {i + 1} should have succeeded: {session.sent[0].text}"
        )
        session.sent.clear()

    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
    await _settle()
    assert len(session.sent) == 1
    assert "wait a bit" in session.sent[0].text or "ትንሽ ቆይተው" in session.sent[0].text


async def test_deposit_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_deposit_command_requires_an_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit"))
    await _settle()
    assert len(session.sent) == 1
    assert "Usage:" in session.sent[0].text or "አጠቃቀም" in session.sent[0].text


async def test_deposit_command_rejects_invalid_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit abc"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def _deposit_hitting(monkeypatch, dp, bot, session, telegram_id, exc_cls) -> str:
    async def _raise(*args, **kwargs):
        raise exc_cls("boom")

    monkeypatch.setattr("services.bot.handlers.deposits.create_deposit_intent", _raise)
    session.sent.clear()
    await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
    await _settle()
    assert len(session.sent) == 1
    return session.sent[0].text


async def test_deposit_command_reports_below_minimum(bot_ctx, monkeypatch):
    from services.payments import deposits

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _deposit_hitting(
        monkeypatch, dp, bot, session, telegram_id, deposits.BelowMinimumDeposit
    )
    assert "minimum deposit" in text or "ዝቅተኛው ገቢ" in text


async def test_deposit_command_reports_daily_cap_exceeded(bot_ctx, monkeypatch):
    from services.payments import deposits

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _deposit_hitting(
        monkeypatch, dp, bot, session, telegram_id, deposits.DailyDepositCapExceeded
    )
    assert "deposit limit" in text or "የገቢ ገደብ" in text


async def test_deposit_command_reports_self_excluded(bot_ctx, monkeypatch):
    from services.payments import deposits

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _deposit_hitting(
        monkeypatch, dp, bot, session, telegram_id, deposits.DepositorSelfExcluded
    )
    assert "self-excluded" in text or "በራስ-ገደብ" in text


async def test_deposit_command_reports_cooloff_active(bot_ctx, monkeypatch):
    from services.payments import deposits

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _deposit_hitting(
        monkeypatch, dp, bot, session, telegram_id, deposits.DepositorCoolingOff
    )
    assert "cool-off" in text or "እረፍት ጊዜ" in text


async def test_deposit_command_reports_provider_error_as_generic_message(bot_ctx, monkeypatch):
    # DepositorBanned, UnknownDepositor, and DepositProviderError all share
    # one except clause and one message -- DepositProviderError here is
    # representative of all three, not a gap in the other two.
    from services.payments import deposits

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _deposit_hitting(
        monkeypatch, dp, bot, session, telegram_id, deposits.DepositProviderError
    )
    assert "Couldn't start your deposit" in text or "ገቢዎን አሁን መጀመር አልተቻለም" in text


async def test_withdraw_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw 100 0911223344 Abebe Kebede"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_withdraw_command_requires_three_arguments(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw 100"))
    await _settle()
    assert len(session.sent) == 1
    assert "Usage:" in session.sent[0].text or "አጠቃቀም" in session.sent[0].text


async def test_withdraw_command_rejects_invalid_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw abc 0911223344 Abebe Kebede"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def test_withdraw_command_succeeds_and_creates_a_real_request(pool, conn, bot_ctx):
    # A real, unmocked call all the way through withdrawals.request_withdrawal()
    # -- proves cmd_withdraw's own argument parsing (amount/account_ref/holder_name
    # split) and its `redis: Redis` dependency both wire correctly through
    # aiogram's real DI, and that a real payments row lands with the right
    # amount and status. fund_user() credits the ledger directly with no
    # accompanying payments row, so this can't accidentally trip
    # RecentReversibleDeposit (it only looks at the payments table).
    #
    # Expects "review", not "approved": request_withdrawal()'s own
    # min-account-age rule always fails for a user registered seconds ago,
    # regardless of amount -- that's the real, correct outcome for a fresh
    # account, not a reason to force an artificially old one just to see
    # the other branch (already covered directly in
    # test_payments_withdrawals.py; this test's own job is proving
    # cmd_withdraw's wiring, not re-deriving the auto-approve rules).
    from tests.integration.conftest import fund_user

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    await fund_user(conn, user_row["id"], Decimal("500.00"))

    await dp.feed_update(
        bot, make_text_update(telegram_id, "/withdraw 300 0911223344 Abebe Kebede")
    )
    await _settle()

    assert len(session.sent) == 1
    assert "under review" in session.sent[0].text or "እየተገመገመ" in session.sent[0].text

    payment = await pool.fetchrow(
        "SELECT amount, status, direction FROM payments WHERE user_id = $1", user_row["id"]
    )
    assert payment is not None
    assert payment["direction"] == "out"
    assert payment["amount"] == Decimal("300.00")
    assert payment["status"] == "review"


async def _withdraw_hitting(monkeypatch, dp, bot, session, telegram_id, exc_cls) -> str:
    async def _raise(*args, **kwargs):
        raise exc_cls("boom")

    monkeypatch.setattr("services.bot.handlers.withdrawals.request_withdrawal", _raise)
    session.sent.clear()
    await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw 100 0911223344 Abebe Kebede"))
    await _settle()
    assert len(session.sent) == 1
    return session.sent[0].text


async def test_withdraw_command_reports_approved_status(bot_ctx, monkeypatch):
    # The real end-to-end test above can only ever observe "review" (a
    # brand-new account always fails request_withdrawal()'s min-account-age
    # rule) -- this covers cmd_withdraw's other message-selection branch
    # directly, the same "mock the collaborator, test this unit's own
    # dispatch logic" reasoning as the rejection-mapping tests below.
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    async def _approve(*args, **kwargs):
        return withdrawals.WithdrawalIntent(
            payment_id=1, our_ref="test-ref", status=withdrawals.STATUS_APPROVED
        )

    monkeypatch.setattr("services.bot.handlers.withdrawals.request_withdrawal", _approve)
    await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw 100 0911223344 Abebe Kebede"))
    await _settle()

    assert len(session.sent) == 1
    assert "approved" in session.sent[0].text or "ጸድቋል" in session.sent[0].text


async def test_withdraw_command_reports_below_minimum(bot_ctx, monkeypatch):
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _withdraw_hitting(
        monkeypatch, dp, bot, session, telegram_id, withdrawals.BelowMinimumWithdrawal
    )
    assert "minimum withdrawal" in text or "ዝቅተኛው ወጪ" in text


async def test_withdraw_command_reports_insufficient_balance(bot_ctx, monkeypatch):
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _withdraw_hitting(
        monkeypatch, dp, bot, session, telegram_id, withdrawals.InsufficientAvailableBalance
    )
    assert "Insufficient balance" in text or "በቂ ቀሪ ሂሳብ" in text


async def test_withdraw_command_reports_kyc_required(bot_ctx, monkeypatch):
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _withdraw_hitting(
        monkeypatch, dp, bot, session, telegram_id, withdrawals.KycLevelTooLow
    )
    assert "identity verification" in text or "የማንነት ማረጋገጫ" in text


async def test_withdraw_command_reports_recent_deposit(bot_ctx, monkeypatch):
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _withdraw_hitting(
        monkeypatch, dp, bot, session, telegram_id, withdrawals.RecentReversibleDeposit
    )
    assert "after a deposit" in text or "ገቢ ካደረጉ" in text


async def test_withdraw_command_reports_unknown_withdrawer_as_generic_error(bot_ctx, monkeypatch):
    from services.payments import withdrawals

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    text = await _withdraw_hitting(
        monkeypatch, dp, bot, session, telegram_id, withdrawals.UnknownWithdrawer
    )
    assert "Something went wrong" in text or "የሆነ ስህተት" in text


async def test_limits_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 500"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_limits_loss_rejects_an_invalid_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/limits loss abc"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def test_limits_loss_rejects_a_non_positive_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/limits loss 0"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def test_limits_loss_sets_the_cap_instantly_on_first_use(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits loss 500"))
    await _settle()

    assert len(session.sent) == 1
    assert "500" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    cap = await pool.fetchval(
        "SELECT daily_loss_cap FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert cap == Decimal("500.00")


async def test_limits_loss_increase_is_deferred_and_says_so(pool, bot_ctx):
    # Same "tighten now, loosen later" rule as the deposit cap -- a lower
    # loss limit protects the player immediately; a higher one only takes
    # effect after the cooling-off window, so it can't be used to escape
    # an existing cap mid-session.
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits loss 100"))
    await _settle()
    session.sent.clear()

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits loss 900"))
    await _settle()

    assert len(session.sent) == 1
    assert "900" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    row = await pool.fetchrow(
        "SELECT daily_loss_cap, pending_daily_loss_cap FROM responsible_gaming_limits "
        "WHERE user_id = $1",
        user_id,
    )
    assert row["daily_loss_cap"] == Decimal("100.00")  # unchanged for now
    assert row["pending_daily_loss_cap"] == Decimal("900.00")


async def test_limits_deposit_rejects_an_invalid_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit abc"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def test_limits_deposit_rejects_a_non_positive_amount(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 0"))
    await _settle()
    assert len(session.sent) == 1
    assert "valid amount" in session.sent[0].text or "ትክክለኛ መጠን" in session.sent[0].text


async def test_limits_deposit_sets_the_cap_instantly_on_first_use(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 500"))
    await _settle()

    assert len(session.sent) == 1
    assert "500" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    cap = await pool.fetchval(
        "SELECT daily_deposit_cap FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert cap == Decimal("500.00")


async def test_limits_deposit_increase_is_deferred_and_says_so(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 100"))
    await _settle()
    session.sent.clear()

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits deposit 900"))
    await _settle()

    assert len(session.sent) == 1
    assert "900" in session.sent[0].text

    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    row = await pool.fetchrow(
        "SELECT daily_deposit_cap, pending_daily_deposit_cap FROM responsible_gaming_limits "
        "WHERE user_id = $1",
        user_id,
    )
    assert row["daily_deposit_cap"] == Decimal("100.00")  # unchanged for now
    assert row["pending_daily_deposit_cap"] == Decimal("900.00")


async def test_limits_cooloff_invalid_duration_is_rejected(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits cooloff nextweek"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    row = await pool.fetchrow(
        "SELECT * FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert row is None  # nothing was ever created -- the command never got that far


async def test_limits_cooloff_valid_duration_sets_it(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits cooloff 24h"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    cooloff_until = await pool.fetchval(
        "SELECT cooloff_until FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    assert cooloff_until is not None


async def test_limits_selfexclude_without_confirm_does_nothing(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits selfexclude"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    status = await pool.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "active"


async def test_limits_selfexclude_wrong_word_prompts_for_the_real_confirmation(pool, bot_ctx):
    # Different from the bare "/limits selfexclude" case above: this one
    # parses as a real SELF_EXCLUDE action with a value that just isn't
    # "confirm" -- parse_limits_command()'s own len(parts) != 2 check
    # means a bare "/limits selfexclude" never even reaches this branch,
    # it falls out as "no action" (limits.usage) instead.
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits selfexclude yes"))
    await _settle()

    assert len(session.sent) == 1
    assert "cannot be undone" in session.sent[0].text or "መቀልበስ አይቻልም" in session.sent[0].text
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    status = await pool.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "active"


async def test_limits_selfexclude_confirm_applies_it(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/limits selfexclude confirm"))
    await _settle()

    assert len(session.sent) == 1
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
    status = await pool.fetchval("SELECT status FROM users WHERE id = $1", user_id)
    assert status == "self_excluded"


async def test_play_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/play"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_play_command_reports_not_available_when_miniapp_url_unset(bot_ctx, monkeypatch):
    # bot_setup's shared Settings defaults miniapp_url to a real value (most
    # of this file's deposit-command tests need it truthy) -- forced back
    # to empty here for this one test's own scenario; the "available"
    # branch's keyboard is already covered directly by test_keyboards.py's
    # own tests of main_menu_keyboard().
    dp, bot, session = bot_ctx
    monkeypatch.setattr(dp["settings"], "miniapp_url", "")
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/play"))
    await _settle()

    assert len(session.sent) == 1
    assert "isn't open yet" in session.sent[0].text or "አልተከፈተም" in session.sent[0].text


async def test_play_command_sends_a_direct_link_fallback_when_short_name_is_configured(bot_ctx, monkeypatch):
    # A real production incident: the persistent menu button and this same
    # web_app keyboard button both delivered no initData at all for some
    # clients until the Mini App was also registered via BotFather's
    # /newapp -- the direct t.me/<bot>/<short_name> link uses that
    # registration and is confirmed to work even when the button alone
    # doesn't. bot_setup's shared Settings leaves telegram_miniapp_short_name
    # unset (every other /play test in this file expects exactly one
    # message), so this monkeypatches it on for just this test -- same
    # pattern as test_play_command_reports_not_available_when_miniapp_url_
    # unset above, not a second Dispatcher (the shared router can only ever
    # attach to bot_setup's one Dispatcher for the whole session).
    dp, bot, session = bot_ctx
    monkeypatch.setattr(dp["settings"], "telegram_miniapp_short_name", "arada")
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/play"))
    await _settle(messages=3)

    # 3 messages: the keyboard-refresh pair (an explicit ReplyKeyboardRemove
    # first, then the real keyboard -- see _send_refreshed_main_menu()'s own
    # comment for the real production incident this guards against), then
    # the direct-link fallback.
    assert len(session.sent) == 3
    assert "https://t.me/jobingo_bot/arada" in session.sent[2].text


async def test_history_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/history"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_history_command_reports_empty_for_a_user_with_no_rounds(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/history"))
    await _settle()

    assert len(session.sent) == 1
    assert "No activity yet" in session.sent[0].text or "እንቅስቃሴ የለም" in session.sent[0].text


async def test_history_command_lists_a_real_completed_round(pool, conn, card_pool, bot_ctx):
    from tests.integration.conftest import create_room

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)

    room_id = await create_room(conn)
    round_row = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 1, 'done', 20.00, 2000, 'test-hash', now()) RETURNING id",
        room_id,
    )
    await conn.execute(
        "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, 5, $2)",
        round_row["id"],
        user_id,
    )
    await conn.execute(
        "INSERT INTO round_winners (round_id, user_id, card_no, pattern, won_on_call, amount) "
        "VALUES ($1, $2, 5, 'row', 12, 32.00)",
        round_row["id"],
        user_id,
    )

    await dp.feed_update(bot, make_text_update(telegram_id, "/history"))
    await _settle()

    assert len(session.sent) == 1
    text = session.sent[0].text
    assert "20.00" in text
    assert "won" in text or "አሸንፈዋል" in text


async def test_history_command_lists_a_round_once_even_with_two_winning_cards(
    pool, conn, card_pool, bot_ctx
):
    # A player can hold several cards in the same round now -- the old
    # LEFT JOIN (round_id, user_id) matched every entry row against
    # every one of the user's own winner rows in that round, so 2 cards
    # x 2 winning cards produced 4 result rows for what is genuinely one
    # round. Proves cmd_history()'s own copy of the fixed query (see its
    # comment pointing at services/gateway/queries.py::user_history())
    # still sends exactly one "Round #1" line, not two or four.
    from tests.integration.conftest import create_room

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)

    room_id = await create_room(conn)
    round_row = await conn.fetchrow(
        "INSERT INTO rounds (room_id, seq, status, stake, house_cut_bps, server_seed_hash, ended_at) "
        "VALUES ($1, 1, 'done', 20.00, 2000, 'test-hash', now()) RETURNING id",
        room_id,
    )
    await conn.execute(
        "INSERT INTO round_entries (round_id, card_no, user_id) VALUES ($1, 5, $2), ($1, 6, $2)",
        round_row["id"],
        user_id,
    )
    await conn.execute(
        "INSERT INTO round_winners (round_id, user_id, card_no, pattern, won_on_call, amount) "
        "VALUES ($1, $2, 5, 'row', 12, 16.00), ($1, $2, 6, 'col', 14, 16.00)",
        round_row["id"],
        user_id,
    )

    await dp.feed_update(bot, make_text_update(telegram_id, "/history"))
    await _settle()

    assert len(session.sent) == 1
    text = session.sent[0].text
    assert text.count("#1") == 1, f"expected exactly one round line, got: {text!r}"


async def test_invite_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/invite"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_invite_command_shows_a_real_referral_link_and_count(bot_ctx):
    # bot_setup's shared Settings has telegram_bot_username="jobingo_bot"
    # set, so cmd_invite's "no_username" branch isn't reachable from this
    # shared fixture -- same constraint as cmd_play's own tests above.
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/invite"))
    await _settle()

    assert len(session.sent) == 1
    text = session.sent[0].text
    assert f"start=ref_{telegram_id}" in text
    assert "0" in text  # no referrals yet


async def test_rules_command_sends_the_rules_text(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/rules"))
    await _settle()
    assert len(session.sent) == 1
    assert "row, column, diagonal" in session.sent[0].text or "መስመር" in session.sent[0].text


async def test_support_command_sends_the_support_contact(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/support"))
    await _settle()
    assert len(session.sent) == 1
    assert "@AradaBingo_Support" in session.sent[0].text


async def test_support_button_press_sends_the_support_contact(bot_ctx):
    # main_menu_keyboard()'s deposit row now ends in "Support" (replacing
    # the old "Withdraw" button -- withdrawing still works via the
    # /withdraw command). Pressing it must dispatch to the real
    # cmd_support() handler, same as typing /support.
    from services.bot.i18n import t

    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    button_text = t("menu.support", "am")
    await dp.feed_update(bot, make_text_update(telegram_id, button_text))
    await _settle()
    assert len(session.sent) == 1
    assert "@AradaBingo_Support" in session.sent[0].text


async def test_stale_cached_keyboard_play_and_withdraw_button_text_still_routes(bot_ctx):
    # A client that hasn't redrawn its ReplyKeyboardMarkup yet can still
    # send the old "Play"/"Withdraw" button text after this relabeling --
    # on_menu_text() deliberately keeps routing both to their real
    # handlers (see its own mapping dict comment) instead of leaving a
    # stale client's taps silently unrecognized.
    from services.bot.i18n import t

    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, t("menu.withdraw", "am")))
    await _settle()
    assert len(session.sent) == 1
    assert "መጠን" in session.sent[0].text  # cmd_withdraw's usage prompt (no amount/args supplied)


async def test_language_command_with_no_args_shows_the_prompt(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/language"))
    await _settle()
    assert len(session.sent) == 1
    assert "Choose your language" in session.sent[0].text or "ቋንቋ ይምረጡ" in session.sent[0].text


async def test_language_command_rejects_an_unsupported_language(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/language fr"))
    await _settle()
    assert len(session.sent) == 1
    assert "Unknown language" in session.sent[0].text or "ያልታወቀ ቋንቋ" in session.sent[0].text


async def test_language_command_sets_a_supported_language_and_persists_it(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/language en"))
    await _settle()

    assert len(session.sent) == 1
    assert session.sent[0].text == "Your language has been set to English."

    stored = await pool.fetchval("SELECT language FROM users WHERE telegram_id = $1", telegram_id)
    assert stored == "en"


async def test_change_username_command_rejects_unregistered_user(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = next_telegram_id()
    await dp.feed_update(bot, make_text_update(telegram_id, "/change_username Bob"))
    await _settle()
    assert len(session.sent) == 1
    assert "register first" in session.sent[0].text or "ይመዝገቡ" in session.sent[0].text


async def test_change_username_command_requires_a_name(bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/change_username"))
    await _settle()
    assert len(session.sent) == 1
    assert "Usage:" in session.sent[0].text or "አጠቃቀም" in session.sent[0].text


async def test_change_username_command_rejects_a_too_long_name(bot_ctx):
    # MAX_DISPLAY_NAME_LENGTH is 32.
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "/change_username " + "x" * 33))
    await _settle()
    assert len(session.sent) == 1
    assert "too long" in session.sent[0].text or "በጣም ረጅም" in session.sent[0].text


async def test_change_username_command_updates_the_real_display_name(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, "/change_username Bereket"))
    await _settle()

    assert len(session.sent) == 1
    assert "Bereket" in session.sent[0].text

    stored = await pool.fetchval("SELECT display_name FROM users WHERE telegram_id = $1", telegram_id)
    assert stored == "Bereket"


async def test_registered_user_sending_unrecognized_text_gets_no_reply(bot_ctx):
    # The phone-number-typed-instead-of-shared case (the other branch of
    # this same handler) is already covered by
    # test_typed_phone_number_is_rejected_with_reprompt; this is the
    # "already registered" early return, spec-silent on purpose.
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    await dp.feed_update(bot, make_text_update(telegram_id, "hello there"))
    await _settle()
    assert session.sent == []


# --- manual deposit receipt photo (P1: keep taking deposits when the
# automatic provider is unavailable) --------------------------------


def make_photo_update(telegram_id: int, *, file_id: str, first_name: str = "Test") -> Update:
    user = User(id=telegram_id, is_bot=False, first_name=first_name)
    photo = [PhotoSize(file_id=file_id, file_unique_id=f"{file_id}-unique", width=90, height=90)]
    message = Message(
        message_id=next(_id_counter),
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=user,
        photo=photo,
    )
    return Update(update_id=next(_id_counter), message=message)


async def test_photo_with_no_pending_manual_deposit_gets_a_clear_reply(pool, bot_ctx):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_photo_update(telegram_id, file_id="AgACAgQ-no-pending"))
    await _settle()

    assert len(session.sent) == 1
    assert "pending" in session.sent[0].text or "ቀሪ" in session.sent[0].text


async def test_photo_attaches_to_the_players_most_recent_pending_manual_deposit(pool, redis, bot_ctx, conn):
    dp, bot, session = bot_ctx
    telegram_id = await _register(dp, bot, session)
    user_id = await pool.fetchval("SELECT id FROM users WHERE telegram_id = $1", telegram_id)

    destination_row = await conn.fetchrow(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name) "
        "VALUES ('telebirr', '0911000000', 'Arada Bingo PLC') RETURNING id"
    )
    intent = await manual.create_manual_deposit_request(
        pool,
        redis,
        user_id=user_id,
        amount=Decimal("120.00"),
        manual_destination_id=destination_row["id"],
        external_reference="FT-BOT-PHOTO",
        receipt_telegram_file_id=None,
        min_deposit=Decimal("10.00"),
        daily_cap=Decimal("50000.00"),
    )

    await dp.feed_update(bot, make_photo_update(telegram_id, file_id="AgACAgQ-real-receipt"))
    await _settle()

    assert len(session.sent) == 1
    assert "received" in session.sent[0].text or "ደርሷል" in session.sent[0].text

    stored = await conn.fetchval(
        "SELECT receipt_telegram_file_id FROM payments WHERE id = $1", intent.payment_id
    )
    assert stored == "AgACAgQ-real-receipt"


# --- dynamic provider availability in /deposit and /withdraw (P1) ------
# payment_provider_availability is shared, session-wide state -- every
# test below disables exactly the row it needs and restores it in a
# finally block, the same discipline test_payment_availability.py's own
# toggle tests already use.


async def _set_chapa_availability(pool, *, direction: str, enabled: bool) -> None:
    admin_id, *_ = await create_test_admin(pool)
    updated = await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="chapa", direction=direction, enabled=enabled,
        reason="test toggle", ip_address=None,
    )
    assert updated is True


async def test_deposit_redirects_to_the_wallet_when_only_manual_is_available(pool, bot_ctx, monkeypatch):
    dp, bot, session = bot_ctx
    settings = dp["settings"]
    monkeypatch.setattr(settings, "miniapp_url", "https://miniapp.test/")

    await _set_chapa_availability(pool, direction="in", enabled=False)
    try:
        telegram_id = await _register(dp, bot, session)
        await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
        await _settle()

        assert len(session.sent) == 1
        sent = session.sent[0]
        assert "wallet" in sent.text or "ቦርሳ" in sent.text
        assert sent.reply_markup is not None
        button = sent.reply_markup.inline_keyboard[0][0]
        assert button.web_app is not None
        # "?target=deposit" (2026-09-14): lands the player directly on
        # the wallet's Deposit tab instead of Balance with a further tap
        # still needed -- see open_wallet_keyboard's own docstring for
        # why this is a query param, not a URL fragment.
        assert button.web_app.url == "https://miniapp.test/?target=deposit"
    finally:
        await _set_chapa_availability(pool, direction="in", enabled=True)


async def test_deposit_redirects_to_the_wallet_when_only_telebirr_sms_is_available(pool, bot_ctx, monkeypatch):
    # Real bug (2026-09-14): the gate here used to check ManualProvider
    # only, so Chapa off + manual off + telebirr_sms on fell through to
    # "Deposits are launching soon" even though the wallet was actually
    # taking Telebirr deposits fine -- see handlers.py's own comment.
    dp, bot, session = bot_ctx
    settings = dp["settings"]
    monkeypatch.setattr(settings, "miniapp_url", "https://miniapp.test/")

    await _set_chapa_availability(pool, direction="in", enabled=False)
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="manual", direction="in", enabled=False,
        reason="test: telebirr_sms-only deposit path", ip_address=None,
    )
    # telebirr_sms deposits ship disabled by default (see migration
    # 9c1f4d7a2b3e) -- an admin has to turn the row on, same as the real
    # Provider Availability screen this session's earlier bug was in.
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=True,
        reason="test: telebirr_sms-only deposit path", ip_address=None,
    )
    try:
        telegram_id = await _register(dp, bot, session)
        await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
        await _settle()

        assert len(session.sent) == 1
        sent = session.sent[0]
        assert "wallet" in sent.text or "ቦርሳ" in sent.text
        assert sent.reply_markup is not None
        button = sent.reply_markup.inline_keyboard[0][0]
        assert button.web_app is not None
        assert button.web_app.url == "https://miniapp.test/?target=deposit"
    finally:
        await _set_chapa_availability(pool, direction="in", enabled=True)
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=False,
            reason="test cleanup", ip_address=None,
        )
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="manual", direction="in", enabled=True,
            reason="test cleanup", ip_address=None,
        )


async def test_deposit_shows_not_available_when_no_provider_and_no_miniapp_url(pool, bot_ctx, monkeypatch):
    dp, bot, session = bot_ctx
    settings = dp["settings"]
    monkeypatch.setattr(settings, "miniapp_url", "")

    await _set_chapa_availability(pool, direction="in", enabled=False)
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="manual", direction="in", enabled=False,
        reason="test: simulate manual disabled too", ip_address=None,
    )
    try:
        telegram_id = await _register(dp, bot, session)
        await dp.feed_update(bot, make_text_update(telegram_id, "/deposit 100"))
        await _settle()

        assert len(session.sent) == 1
        assert "launching soon" in session.sent[0].text or "በቅርቡ" in session.sent[0].text
    finally:
        await _set_chapa_availability(pool, direction="in", enabled=True)
        await admin_queries.set_payment_provider_availability_admin(
            pool, admin_id=admin_id, provider="manual", direction="in", enabled=True,
            reason="test cleanup", ip_address=None,
        )


async def test_withdraw_uses_the_manual_rail_when_chapa_is_unavailable(pool, conn, bot_ctx):
    # Proves the exact same command syntax seamlessly falls through to
    # ManualProvider()+force_review=True -- no Mini-App redirect needed
    # here, unlike deposit, since a manual withdrawal needs nothing this
    # command doesn't already collect.
    dp, bot, session = bot_ctx
    await _set_chapa_availability(pool, direction="out", enabled=False)
    try:
        telegram_id = await _register(dp, bot, session)
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        await fund_user(conn, user_row["id"], Decimal("500.00"))

        await dp.feed_update(bot, make_text_update(telegram_id, "/withdraw 300 0911223344 Abebe Kebede"))
        await _settle()

        assert len(session.sent) == 1
        assert "under review" in session.sent[0].text or "እየተገመገመ" in session.sent[0].text

        payment = await pool.fetchrow(
            "SELECT amount, status, provider FROM payments WHERE user_id = $1", user_row["id"]
        )
        assert payment is not None
        assert payment["provider"] == "manual"
        assert payment["status"] == "review"  # always review for manual, regardless of amount
        assert payment["amount"] == Decimal("300.00")
    finally:
        await _set_chapa_availability(pool, direction="out", enabled=True)


# --- Telegram payment-agent SMS-forwarding channel (services/bot/
# handlers.py's on_agent_sms) ------------------------------------------


def _agent_sms(reference: str) -> str:
    # The real Telebirr "money received" template (see services/payments/
    # telebirr_parser.py) -- a dedicated, per-test recipient name so this
    # never accidentally matches a manual_payment_destinations row another
    # test happened to configure.
    return (
        f"Dear NoOneConfigured{reference} \n"
        f"You have received ETB 10.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def test_authorized_agent_sms_is_ingested_and_replied_to(pool, bot_ctx):
    dp, bot, session = bot_ctx
    agent_telegram_id = next_telegram_id()
    await pool.execute(
        "INSERT INTO payment_agents (telegram_user_id, display_name, is_active) VALUES ($1, $2, true)",
        agent_telegram_id,
        "Test Agent",
    )
    reference = f"DI{agent_telegram_id % 10**8:08d}"

    await dp.feed_update(bot, make_text_update(agent_telegram_id, _agent_sms(reference)))
    await _settle()

    assert len(session.sent) == 1
    assert reference in session.sent[0].text

    row = await pool.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "rejected"  # no matching recipient configured -- still ingested and replied to


async def test_unauthorized_sender_sms_text_is_never_ingested(pool, bot_ctx):
    # Not in payment_agents at all -- the filter must fall through to
    # on_menu_text instead of treating this as a payment submission. An
    # unregistered sender's non-menu text triggers on_menu_text's own
    # "please register" reply, proving propagation genuinely reached it
    # rather than silently vanishing.
    dp, bot, session = bot_ctx
    stranger_telegram_id = next_telegram_id()
    reference = f"DI{stranger_telegram_id % 10**8:08d}"

    await dp.feed_update(bot, make_text_update(stranger_telegram_id, _agent_sms(reference)))
    await _settle()

    assert len(session.sent) == 1
    assert "Share Phone Number" in session.sent[0].text or "ስልክ ቁጥር አጋራ" in session.sent[0].text

    row = await pool.fetchrow(
        "SELECT id FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is None


async def test_pasting_the_same_complete_sms_twice_produces_one_record_not_two(pool, bot_ctx):
    # CTO directive section 29: an agent pasting the exact same complete
    # SMS twice (a real, expected occurrence -- e.g. double-tapping paste)
    # must produce exactly one payment_evidence row, and the bot's own
    # second reply must say so, not silently look identical to a fresh
    # success.
    dp, bot, session = bot_ctx
    agent_telegram_id = next_telegram_id()
    await pool.execute(
        "INSERT INTO payment_agents (telegram_user_id, display_name, is_active) VALUES ($1, $2, true)",
        agent_telegram_id,
        "Test Agent",
    )
    reference = f"DI{agent_telegram_id % 10**8:08d}"
    sms = _agent_sms(reference)

    await dp.feed_update(bot, make_text_update(agent_telegram_id, sms))
    await _settle()
    await dp.feed_update(bot, make_text_update(agent_telegram_id, sms))
    await _settle()

    assert len(session.sent) == 2
    assert reference in session.sent[0].text
    assert reference in session.sent[1].text
    assert session.sent[0].text != session.sent[1].text  # the second reply must read differently

    count = await pool.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert count == 1


async def test_portal_command_sends_a_login_link_only_to_an_active_agent(pool, redis, bot_ctx):
    dp, bot, session = bot_ctx
    agent_telegram_id = next_telegram_id()
    await pool.execute(
        "INSERT INTO payment_agents (telegram_user_id, display_name, is_active) VALUES ($1, $2, true)",
        agent_telegram_id,
        "Portal Test Agent",
    )

    await dp.feed_update(bot, make_text_update(agent_telegram_id, "/portal"))
    await _settle()

    assert len(session.sent) == 1
    assert "https://agent.test/login?token=" in session.sent[0].text
    # Command("portal") must win over on_agent_sms's own bare F.text
    # catch-all -- this is the actual regression this test guards against:
    # without registering the command handler *before* on_agent_sms, "/portal"
    # would be swallowed as if it were a forwarded (unparseable) SMS.
    unparseable_count = await pool.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE source_ref = $1", str(agent_telegram_id)
    )
    assert unparseable_count == 0


async def test_portal_command_ignored_for_an_unregistered_sender(pool, bot_ctx):
    dp, bot, session = bot_ctx
    stranger_telegram_id = next_telegram_id()

    await dp.feed_update(bot, make_text_update(stranger_telegram_id, "/portal"))
    await _settle()

    # Falls through to on_menu_text's own "please register" reply, exactly
    # like an unregistered sender's forwarded-SMS text does above -- never
    # a portal link handed to someone not in payment_agents.
    assert len(session.sent) == 1
    assert "https://agent.test/login?token=" not in session.sent[0].text


@pytest.mark.load
async def test_15_concurrent_balance_commands_all_succeed_with_measured_latency(pool, conn, bot_ctx):
    """A real, modest concurrency measurement -- not the parent directive's
    own 500/1000-concurrent figures, deliberately: this exact shared dev
    database was independently found, mid-way through this same pass's own
    regression testing, to already carry 5,379 accumulated room rows from
    this session's cumulative testing (a real, live instance of the same
    class of incident DECISIONS.md already documents once). A genuine
    500-1000 concurrent Telegram-update load test belongs in a dedicated,
    disposable environment, not layered onto a database already showing
    that exact failure mode.

    A first draft of this test used 50 concurrent commands and caused a
    real, self-inflicted regression: it forced the session-scoped `pool`
    fixture (shared by every other test file in this same run, several of
    which -- gateway_server, admin_server, payments_server -- hold their
    own separately-sized pools concurrently) to open up to 50 new
    physical Postgres connections all at once, and the combined total
    across every session-scoped pool active at that moment exceeded this
    Postgres instance's own real max_connections=100 ceiling
    (`TooManyConnectionsError: sorry, too many clients already`),
    cascading into unrelated test failures across the whole suite. 15
    concurrent real users through the real dispatcher is still enough to
    prove no crash, no lost reply, and no cross-request state corruption
    under real concurrency, and to produce a genuine (not estimated)
    latency distribution for /balance specifically, without risking the
    same shared-connection-budget exhaustion.
    """
    from packages.core import metrics
    from tests.integration.conftest import fund_user

    dp, bot, session = bot_ctx
    concurrency = 15
    telegram_ids = [next_telegram_id() for _ in range(concurrency)]

    for telegram_id in telegram_ids:
        await dp.feed_update(
            bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
        )
    await _settle(messages=concurrency)
    for telegram_id in telegram_ids:
        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        await fund_user(conn, user_row["id"], Decimal("10.00"))

    session.sent.clear()
    latencies_ms: list[float] = []

    async def one_balance_call(telegram_id: int) -> None:
        start = time.perf_counter()
        await dp.feed_update(bot, make_text_update(telegram_id, "/balance"))
        latencies_ms.append((time.perf_counter() - start) * 1000)

    started = time.monotonic()
    await asyncio.gather(*(one_balance_call(tid) for tid in telegram_ids))
    wall_clock_elapsed = time.monotonic() - started
    await _settle(messages=concurrency)

    # Every single concurrent /balance call got its own reply -- no lost
    # message, no cross-request state bleeding one user's balance into
    # another's under real concurrent dispatch.
    assert len(session.sent) == concurrency
    for message in session.sent:
        assert "10.00" in message.text

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p95 = latencies_ms[int(len(latencies_ms) * 0.95) - 1]
    p99 = latencies_ms[-1]
    print(
        f"\n[{concurrency}-concurrent /balance] wall_clock={wall_clock_elapsed:.3f}s "
        f"p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms"
    )

    # A sanity bound, not the parent directive's own FAST-class target.
    # A related but separate finding (isolated with a bare asyncpg pool,
    # no bot code involved at all -- see docs/TELEGRAM_PERFORMANCE.md)
    # measured that this kind of p50/p99 can be dominated by cold
    # Postgres *connection establishment*, not query execution or
    # handler logic: a pre-warmed pool already sized to 50 connections
    # answered 50 concurrent queries in ~22-25ms, while the same pool
    # cold needed ~1.8s to open that many new physical connections at
    # once. That finding is what drove raising the bot's own real pool
    # (services/bot/app.py) from min_size=2 to min_size=5. This specific
    # test's own 15-way concurrency intentionally stays well under any
    # single session-scoped pool's warm capacity (see this test's own
    # docstring for why a higher figure caused a real, self-inflicted
    # regression), so it is not itself a connection-establishment
    # benchmark -- just a sanity bound confirming no crash or pathological
    # slowdown under real concurrent dispatch. Not a network round trip
    # to Telegram either way (FakeSession has none), so this number isn't
    # directly comparable to a real deployment's own webhook-to-reply
    # figure.
    assert p99 < 5000, f"a /balance call took {p99:.0f}ms under {concurrency}-way concurrency"

    assert (
        metrics.telegram_commands_total.labels(handler="cmd_balance")._value.get() >= concurrency
    )


async def test_disabling_a_command_makes_a_real_dispatcher_call_degrade_gracefully(pool, bot_ctx):
    """Telegram Command Center: an admin-disabled command must return a
    controlled, translated reply through the real dispatcher -- never a
    crash, never silence, and never the real handler's own effect (a
    disabled /balance must not leak a real balance). Uses this file's own
    bot_ctx/bot_setup rather than building a second dispatcher elsewhere:
    aiogram permanently attaches services/bot/handlers.py's module-level
    `router` singleton to whichever Dispatcher calls build_dispatcher()
    first and refuses a second attachment for the rest of the process --
    confirmed the hard way when an earlier draft of this test lived in
    its own file and imported bot_ctx across modules, which made pytest
    instantiate the session-scoped bot_setup fixture a second time.
    """
    from packages.core import metrics
    from services.admin import command_registry_queries
    from services.bot import command_registry
    from tests.integration.test_admin_auth import create_test_admin

    dp, bot, session = bot_ctx
    admin_id, *_ = await create_test_admin(pool, role="superadmin")

    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_balance", changes={"enabled": False},
        reason="test", ip_address=None,
    )
    try:
        await command_registry.refresh_once(pool)

        telegram_id = next_telegram_id()
        await dp.feed_update(
            bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
        )
        await _settle()
        session.sent.clear()

        blocked_before = metrics.telegram_command_blocked_total.labels(handler="cmd_balance")._value.get()
        await dp.feed_update(bot, make_text_update(telegram_id, "/balance"))
        await _settle()

        assert len(session.sent) == 1
        assert "temporarily unavailable" in session.sent[0].text or "ለጊዜው" in session.sent[0].text
        assert (
            metrics.telegram_command_blocked_total.labels(handler="cmd_balance")._value.get()
            == blocked_before + 1
        )
    finally:
        # This table is real, shared, persistent config -- restore it for
        # every other test/process sharing this database, the same
        # discipline as clean_payout_stream/clean_notifications_stream in
        # conftest.py.
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_balance", changes={"enabled": True},
            reason="test cleanup", ip_address=None,
        )
        command_registry.set_cache({})


async def test_disabling_a_command_also_blocks_it_via_the_menu_button_path(pool, bot_ctx):
    """command_gate_middleware (services/bot/app.py) only wraps aiogram's
    own routed dispatch -- on_menu_text's own dispatch to e.g. cmd_balance
    is a plain, direct Python function call, invisible to that
    middleware. A real architecture-review finding while wiring the
    registry: without handlers.py::on_menu_text's own explicit
    command_registry.is_enabled() check, disabling /balance would still
    leave it fully working for anyone who presses the "Balance" reply-
    keyboard button instead of typing the slash command -- this is the
    regression test for that gap, exercised via the real localized button
    text on_menu_text actually matches against, not the slash command.
    """
    from packages.core import metrics
    from services.admin import command_registry_queries
    from services.bot import command_registry
    from services.bot.i18n import t
    from tests.integration.test_admin_auth import create_test_admin

    dp, bot, session = bot_ctx
    admin_id, *_ = await create_test_admin(pool, role="superadmin")

    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_balance", changes={"enabled": False},
        reason="test", ip_address=None,
    )
    try:
        await command_registry.refresh_once(pool)

        telegram_id = next_telegram_id()
        await dp.feed_update(
            bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
        )
        await _settle()
        session.sent.clear()

        blocked_before = metrics.telegram_command_blocked_total.labels(handler="cmd_balance")._value.get()
        # "am" is the language make_contact_update's fresh registration
        # resolves to by default (no language_code on the fake sender) --
        # the real localized menu button label, not the slash command.
        button_text = t("menu.balance", "am")
        await dp.feed_update(bot, make_text_update(telegram_id, button_text))
        await _settle()

        assert len(session.sent) == 1
        assert "ለጊዜው" in session.sent[0].text or "temporarily unavailable" in session.sent[0].text
        assert (
            metrics.telegram_command_blocked_total.labels(handler="cmd_balance")._value.get()
            == blocked_before + 1
        )
    finally:
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_balance", changes={"enabled": True},
            reason="test cleanup", ip_address=None,
        )
        command_registry.set_cache({})


async def test_a_configured_cooldown_is_enforced_through_the_real_dispatcher(pool, bot_ctx):
    """Telegram Command Center Phase 3: cooldown_seconds/rate_limit_per_
    minute were real, stored, admin-editable fields with no enforcement
    wired up as of Phase 2's own report. Fixed by services/bot/
    command_registry.py::command_gate_middleware reusing the same Redis
    token-bucket packages/core/rate_limit.py already uses for deposits/
    gateway/admin-login -- this test proves it end to end through the
    real dispatcher, not just against the bucket primitive directly
    (tests/integration/test_command_registry_admin.py covers that).
    """
    from packages.core import metrics
    from services.admin import command_registry_queries
    from services.bot import command_registry
    from tests.integration.test_admin_auth import create_test_admin

    dp, bot, session = bot_ctx
    admin_id, *_ = await create_test_admin(pool, role="superadmin")

    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_rules", changes={"cooldown_seconds": 30},
        reason="test", ip_address=None,
    )
    try:
        await command_registry.refresh_once(pool)

        telegram_id = next_telegram_id()
        await dp.feed_update(
            bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
        )
        await _settle()
        session.sent.clear()

        rate_limited_before = metrics.telegram_command_rate_limited_total.labels(
            handler="cmd_rules", limit_type="cooldown"
        )._value.get()

        # First /rules goes through normally -- the real handler's own
        # rules text, not a cooldown message.
        await dp.feed_update(bot, make_text_update(telegram_id, "/rules"))
        await _settle()
        assert len(session.sent) == 1
        first_reply = session.sent[0].text

        # Immediately again -- must be blocked by the 30s cooldown just
        # configured, with a real, computed "try again in ~30s" reply,
        # not the earlier rules text repeated and not a bare "too many
        # requests".
        session.sent.clear()
        await dp.feed_update(bot, make_text_update(telegram_id, "/rules"))
        await _settle()
        assert len(session.sent) == 1
        assert session.sent[0].text != first_reply
        assert "30" in session.sent[0].text or "ለ" in session.sent[0].text  # am: "በ{seconds}"
        assert (
            metrics.telegram_command_rate_limited_total.labels(
                handler="cmd_rules", limit_type="cooldown"
            )._value.get()
            == rate_limited_before + 1
        )
    finally:
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_rules", changes={"cooldown_seconds": 0},
            reason="test cleanup", ip_address=None,
        )
        command_registry.set_cache({})


async def test_a_configured_analytics_key_relabels_every_command_metric(pool, bot_ctx):
    """Phase 3's "one canonical command identity": an admin-set
    analytics_key must relabel *every* metric this codebase attributes to
    a command -- count/success/error/latency (services/bot/perf.py) and
    DB/Redis time (also perf.py, via the same resolved label feeding
    _current_command) -- never leave some metrics keyed by the real
    handler_name while others use the override.
    """
    from packages.core import metrics
    from services.admin import command_registry_queries
    from services.bot import command_registry
    from tests.integration.test_admin_auth import create_test_admin

    dp, bot, session = bot_ctx
    admin_id, *_ = await create_test_admin(pool, role="superadmin")

    await command_registry_queries.update_command_admin(
        pool, admin_id=admin_id, handler_name="cmd_support", changes={"analytics_key": "support_v2"},
        reason="test", ip_address=None,
    )
    try:
        await command_registry.refresh_once(pool)

        telegram_id = next_telegram_id()
        await dp.feed_update(
            bot, make_contact_update(telegram_id, contact_user_id=telegram_id, phone=unique_phone())
        )
        await _settle()
        session.sent.clear()

        real_name_before = metrics.telegram_commands_total.labels(handler="cmd_support")._value.get()
        override_before = metrics.telegram_commands_total.labels(handler="support_v2")._value.get()
        override_latency_before = metrics.telegram_command_latency_seconds.labels(
            handler="support_v2"
        )._sum.get()

        await dp.feed_update(bot, make_text_update(telegram_id, "/support"))
        await _settle()

        assert len(session.sent) == 1
        # The real handler ran normally -- this is a relabeling, not a
        # behavior change.
        assert metrics.telegram_commands_total.labels(handler="cmd_support")._value.get() == real_name_before
        assert (
            metrics.telegram_commands_total.labels(handler="support_v2")._value.get()
            == override_before + 1
        )
        assert metrics.telegram_command_success_total.labels(handler="support_v2")._value.get() >= 1
        assert (
            metrics.telegram_command_latency_seconds.labels(handler="support_v2")._sum.get()
            > override_latency_before
        )
        # DB/Redis time attribution (telegram_db_duration_seconds/
        # telegram_redis_duration_seconds) is driven by the exact same
        # resolved label via perf.py's _current_command contextvar --
        # verified directly in tests/integration/test_perf_db_redis.py
        # against a properly instrumented pool/redis client (this file's
        # own bot_ctx fixture intentionally uses the *plain*, uninstrumented
        # test pool/redis, matching every other test here, so it cannot
        # itself observe DB/Redis timing regardless of labeling).
    finally:
        await command_registry_queries.update_command_admin(
            pool, admin_id=admin_id, handler_name="cmd_support", changes={"analytics_key": None},
            reason="test cleanup", ip_address=None,
        )
        command_registry.set_cache({})


# --- self-service Telebirr redemption by pasting the SMS in chat, no
# command needed (services/bot/handlers.py's on_customer_telebirr_paste)
# -- the same redeem_evidence() the Mini App's own wallet screen already
# offers (services/payments/telebirr_redemption.py), now reachable by a
# registered player just pasting their confirmation SMS as an ordinary
# chat message. ---------------------------------------------------------


def _transferred_sms(reference: str, *, amount: str = "10.00") -> str:
    # Template 2 from services/payments/telebirr_parser.py's own
    # docstring (the PAYER's own phone, not the recipient's) -- this is
    # the message a real customer actually has on their own clipboard
    # after paying, which is the whole point of this feature.
    return (
        f"Dear Nebyu You have transferred ETB {amount} to SURAFEL DESALEGNE (2519****0917) "
        f"on 02/09/2026 07:32:00. Your transaction number is {reference}. The service fee is "
        "ETB 0.87 and 15% VAT on the service fee is ETB 0.13. Your current E-Money Account "
        "balance is ETB 385.12. To download your payment information please click this link: "
        f"https://transactioninfo.ethiotelecom.et/receipt/{reference}. Thank you for using "
        "telebirr Ethio telecom"
    )


async def _make_available_evidence(pool, *, amount: str = "10.00") -> str:
    reference = f"DI{next_telegram_id() % 10**8:08d}"
    recipient = f"Recipient{reference}"
    await pool.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', '0911000000', $1, true)",
        recipient,
    )
    outcome = await ingest_sms_evidence(
        pool,
        raw_sms=(
            f"Dear {recipient} \n"
            f"You have received ETB {amount} from DAWIT WERKALEMAHU(2519****6294)  on "
            f"04/09/2026 10:27:23. Your transaction number is {reference}. Your current "
            "E-Money Account balance is ETB 252.12.\nThank you for using telebirr\nEthio telecom"
        ),
        source="macrodroid", source_ref="test-device",
    )
    assert outcome.status == "ingested_available"
    return reference


async def _set_telebirr_sms_availability(pool, *, enabled: bool) -> None:
    admin_id, *_ = await create_test_admin(pool)
    await admin_queries.set_payment_provider_availability_admin(
        pool, admin_id=admin_id, provider="telebirr_sms", direction="in", enabled=enabled,
        reason="test toggle", ip_address=None,
    )


async def test_pasting_your_own_telebirr_sms_in_chat_credits_your_wallet_no_command(pool, bot_ctx):
    dp, bot, session = bot_ctx
    await _set_telebirr_sms_availability(pool, enabled=True)
    try:
        reference = await _make_available_evidence(pool, amount="25.00")
        telegram_id = await _register(dp, bot, session)

        await dp.feed_update(bot, make_text_update(telegram_id, _transferred_sms(reference, amount="25.00")))
        await _settle()

        assert len(session.sent) == 1
        assert "25.00" in session.sent[0].text

        user_row = await pool.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
        evidence_row = await pool.fetchrow(
            "SELECT status, redeemed_by_user_id FROM payment_evidence WHERE external_reference = $1",
            reference,
        )
        assert evidence_row["status"] == "redeemed"
        assert evidence_row["redeemed_by_user_id"] == user_row["id"]

        async with pool.acquire() as conn:
            cash_account = await ledger.get_or_create_account(conn, user_row["id"], "user_cash")
            assert await ledger.balance(conn, cash_account.id) == Decimal("25.00")
    finally:
        await _set_telebirr_sms_availability(pool, enabled=False)


async def test_pasting_an_sms_with_an_unknown_reference_gets_a_clear_reply(pool, bot_ctx):
    dp, bot, session = bot_ctx
    await _set_telebirr_sms_availability(pool, enabled=True)
    try:
        telegram_id = await _register(dp, bot, session)
        never_ingested_reference = f"DI{next_telegram_id() % 10**8:08d}"

        await dp.feed_update(
            bot, make_text_update(telegram_id, _transferred_sms(never_ingested_reference))
        )
        await _settle()

        assert len(session.sent) == 1
        assert "couldn't find" in session.sent[0].text.lower() or "አላገኘንም" in session.sent[0].text
    finally:
        await _set_telebirr_sms_availability(pool, enabled=False)


async def test_pasting_an_sms_when_telebirr_sms_is_disabled_credits_nothing(pool, bot_ctx):
    # telebirr_sms ships disabled by default (migration 9c1f4d7a2b3e) --
    # explicitly left off here (no enable/restore) to exercise that real
    # default rather than assuming it.
    dp, bot, session = bot_ctx
    reference = await _make_available_evidence(pool)
    telegram_id = await _register(dp, bot, session)

    await dp.feed_update(bot, make_text_update(telegram_id, _transferred_sms(reference)))
    await _settle()

    assert len(session.sent) == 1
    assert "launching soon" in session.sent[0].text or "በቅርቡ" in session.sent[0].text

    evidence_row = await pool.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert evidence_row["status"] == "available"  # untouched -- never redeemed


async def test_an_unregistered_stranger_pasting_a_telebirr_sms_gets_the_registration_prompt(pool, bot_ctx):
    # Mirrors test_unauthorized_sender_sms_text_is_never_ingested's own
    # agent-SMS case, but for this handler: an SMS-shaped message is still
    # just unrecognized text from someone who isn't a player yet, so it
    # gets the same registration nudge as anything else would (not a bare
    # "please register" reply, which this handler used to send here
    # before matching on_menu_text's own existing behavior).
    dp, bot, session = bot_ctx
    await _set_telebirr_sms_availability(pool, enabled=True)
    try:
        stranger_telegram_id = next_telegram_id()
        reference = await _make_available_evidence(pool)

        await dp.feed_update(bot, make_text_update(stranger_telegram_id, _transferred_sms(reference)))
        await _settle()

        assert len(session.sent) == 1
        assert "Share Phone Number" in session.sent[0].text or "ስልክ ቁጥር አጋራ" in session.sent[0].text

        evidence_row = await pool.fetchrow(
            "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
        )
        assert evidence_row["status"] == "available"  # a stranger can never redeem anything
    finally:
        await _set_telebirr_sms_availability(pool, enabled=False)
