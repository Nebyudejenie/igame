"""Tests for services/bot/set_bot_profile_cli.py -- same real-fake-Bot
pattern test_verify_menu_button.py already establishes (AsyncMock, no
real Telegram API call from any test in this repo).
"""

from unittest.mock import AsyncMock

from aiogram.types import BotCommand, BotDescription, BotShortDescription, User

from packages.core.config import Settings
from services.bot.set_bot_profile_cli import (
    TARGET_COMMANDS,
    TARGET_DESCRIPTION,
    TARGET_SHORT_DESCRIPTION,
    _run,
)


def _settings(**overrides: object) -> Settings:
    kwargs: dict[str, object] = dict(telegram_bot_token="fake-token")
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


def _fake_bot(*, matches_target: bool) -> AsyncMock:
    """matches_target=True builds a bot that already reports every field
    as exactly the target copy (the clean, nothing-to-do case);
    matches_target=False reports empty/wrong everywhere (the real state
    production was actually found in on 2026-09-18).
    """
    bot = AsyncMock()
    bot.get_me.return_value = User(id=8988277728, is_bot=True, first_name="Arada Bingo", username="aradabbot")

    async def get_short(language_code: str | None = None) -> BotShortDescription:
        text = TARGET_SHORT_DESCRIPTION[language_code or ""] if matches_target else ""
        return BotShortDescription(short_description=text)

    async def get_description(language_code: str | None = None) -> BotDescription:
        text = TARGET_DESCRIPTION[language_code or ""] if matches_target else ""
        return BotDescription(description=text)

    async def get_commands(language_code: str | None = None, scope: object = None) -> list[BotCommand]:
        if not matches_target:
            return [BotCommand(command="start", description="Start Jo Bingo")]
        pairs = TARGET_COMMANDS[language_code or ""]
        return [BotCommand(command=c, description=d) for c, d in pairs]

    bot.get_my_short_description.side_effect = get_short
    bot.get_my_description.side_effect = get_description
    bot.get_my_commands.side_effect = get_commands
    return bot


async def test_missing_bot_token_is_refused_before_any_api_call():
    bot = _fake_bot(matches_target=False)
    code = await _run(False, settings=_settings(telegram_bot_token=""), bot=bot)
    assert code == 1
    bot.get_me.assert_not_awaited()


async def test_everything_already_correct_is_a_clean_no_op():
    bot = _fake_bot(matches_target=True)
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 0
    bot.set_my_short_description.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()


async def test_mismatch_without_fix_reports_but_changes_nothing():
    bot = _fake_bot(matches_target=False)
    code = await _run(False, settings=_settings(), bot=bot)
    assert code == 1
    bot.set_my_short_description.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()


async def test_fix_applies_every_mismatched_field_in_every_configured_language():
    bot = _fake_bot(matches_target=False)
    code = await _run(True, settings=_settings(), bot=bot)
    assert code == 0

    # short_description + description each have 3 target entries ("", en, am).
    assert bot.set_my_short_description.await_count == 3
    assert bot.set_my_description.await_count == 3
    # commands has 2 target entries ("" and am).
    assert bot.set_my_commands.await_count == 2

    short_calls = {kwargs["language_code"]: kwargs["short_description"] for _, kwargs in bot.set_my_short_description.await_args_list}
    assert short_calls[None] == TARGET_SHORT_DESCRIPTION[""]
    assert short_calls["en"] == TARGET_SHORT_DESCRIPTION["en"]
    assert short_calls["am"] == TARGET_SHORT_DESCRIPTION["am"]

    commands_calls = {kwargs["language_code"]: kwargs["commands"] for _, kwargs in bot.set_my_commands.await_args_list}
    assert [c.command for c in commands_calls[None]] == [c for c, _ in TARGET_COMMANDS[""]]
    assert [c.description for c in commands_calls["am"]] == [d for _, d in TARGET_COMMANDS["am"]]


async def test_fix_is_idempotent_a_second_clean_run_makes_zero_write_calls():
    bot = _fake_bot(matches_target=True)
    code = await _run(True, settings=_settings(), bot=bot)
    assert code == 0
    bot.set_my_short_description.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()
