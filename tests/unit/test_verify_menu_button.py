"""Tests for services/bot/verify_menu_button.py -- a real fake Bot
(unittest.mock.AsyncMock, same pattern test_notifier.py already
establishes for aiogram) rather than hitting the real Telegram API,
which no test in this repo does or should.
"""

from unittest.mock import AsyncMock

from aiogram.types import MenuButtonDefault, MenuButtonWebApp, User, WebAppInfo

from packages.core.config import Settings
from services.bot.verify_menu_button import _run

MINIAPP_URL = "https://arada.click"


def _settings(**overrides: object) -> Settings:
    kwargs: dict[str, object] = dict(
        telegram_bot_token="fake-token", telegram_bot_username="zemengamebot", miniapp_url=MINIAPP_URL
    )
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


def _fake_bot(*, username: str = "zemengamebot", menu_button: object) -> AsyncMock:
    bot = AsyncMock()
    bot.get_me.return_value = User(id=8988277728, is_bot=True, first_name="Zemen Game", username=username)
    bot.get_chat_menu_button.return_value = menu_button
    return bot


async def test_reports_token_username_mismatch_and_refuses_to_proceed():
    bot = _fake_bot(username="some_other_bot", menu_button=MenuButtonDefault())
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 1
    bot.set_chat_menu_button.assert_not_awaited()


async def test_already_correct_web_app_button_is_a_clean_no_op():
    bot = _fake_bot(menu_button=MenuButtonWebApp(text="Play", web_app=WebAppInfo(url=MINIAPP_URL)))
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 0
    bot.set_chat_menu_button.assert_not_awaited()


async def test_wrong_button_without_fix_reports_but_does_not_change_anything():
    bot = _fake_bot(menu_button=MenuButtonDefault())
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 1
    bot.set_chat_menu_button.assert_not_awaited()


async def test_wrong_button_pointing_elsewhere_without_fix_is_also_reported():
    bot = _fake_bot(
        menu_button=MenuButtonWebApp(text="Play", web_app=WebAppInfo(url="https://wrong.example.com"))
    )
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 1
    bot.set_chat_menu_button.assert_not_awaited()


async def test_a_trailing_slash_telegram_added_itself_is_not_reported_as_wrong():
    # A real false negative caught against production: setChatMenuButton
    # with a bare-domain URL round-trips through getChatMenuButton with a
    # "/" Telegram appended itself -- functionally identical (the WebView
    # opens the same page), and must not be treated as "still broken" or
    # a --fix run would just re-set the exact same effective URL forever.
    bot = _fake_bot(menu_button=MenuButtonWebApp(text="Play", web_app=WebAppInfo(url=MINIAPP_URL + "/")))
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 0
    bot.set_chat_menu_button.assert_not_awaited()


async def test_fix_corrects_a_wrong_menu_button():
    bot = _fake_bot(menu_button=MenuButtonDefault())
    code = await _run(True, settings=_settings(), bot=bot)
    assert code == 0
    bot.set_chat_menu_button.assert_awaited_once()
    _, kwargs = bot.set_chat_menu_button.call_args
    sent = kwargs["menu_button"]
    assert isinstance(sent, MenuButtonWebApp)
    assert sent.web_app.url == MINIAPP_URL


async def test_fix_refuses_when_miniapp_url_is_not_configured():
    bot = _fake_bot(menu_button=MenuButtonDefault())
    code = await _run(True, settings=_settings(miniapp_url=""), bot=bot)
    assert code == 1
    bot.set_chat_menu_button.assert_not_awaited()


async def test_missing_bot_token_is_refused_before_any_api_call():
    bot = _fake_bot(menu_button=MenuButtonDefault())
    code = await _run(False, settings=_settings(telegram_bot_token=""), bot=bot)
    assert code == 1
    bot.get_me.assert_not_awaited()
