"""One-shot CLI to diagnose and, optionally, fix the Telegram bot's own
chat menu button -- the persistent icon next to the message box, a
launch surface for the Mini App entirely separate from and independent
of the in-chat "|>| Play" keyboard button main_menu_keyboard() already
builds correctly (services/bot/keyboards.py). Nothing in this codebase
has ever called setChatMenuButton, so whatever the menu button currently
does was set (or never set) entirely outside this repo -- via BotFather,
by hand, at some point in the past.

Exists because a real production incident (the Mini App opening to a
permanently blank/rejected-session screen) needed exactly this fact
checked and could not be checked any other way: no code path in this
repo has ever read or written this specific piece of Telegram-side bot
configuration.

Run: `python -m services.bot.verify_menu_button` to report the current
state only (never writes anything). Add `--fix` to correct the menu
button to a real web_app launch pointed at MINIAPP_URL, but only if it's
currently something other than that already -- never touches it if it's
already correct, and refuses to run at all if MINIAPP_URL isn't
configured (nothing safe to set it to).

Never prints the bot token. get_me()'s response contains no secret --
only the bot's own public id/username/name -- so it's safe to print
in full.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo

from packages.core.config import Settings, get_settings
from services.bot.app import build_bot
from services.bot.i18n import t


async def _run(fix: bool, *, settings: Settings | None = None, bot: Bot | None = None) -> int:
    """settings/bot are overridable only so tests can inject a fake bot
    without hitting the real Telegram API -- the real CLI entrypoint
    (main(), below) always calls this with neither, using the real
    configured settings and a real Bot built from them.
    """
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not configured.", file=sys.stderr)
        return 1

    owns_bot = bot is None
    bot = bot or build_bot(settings)
    try:
        me = await bot.get_me()
        print(f"Bot identity: @{me.username} (id={me.id}, name={me.first_name!r})")
        if settings.telegram_bot_username and me.username != settings.telegram_bot_username:
            print(
                f"MISMATCH: TELEGRAM_BOT_TOKEN belongs to @{me.username}, but "
                f"TELEGRAM_BOT_USERNAME is configured as @{settings.telegram_bot_username}. "
                "This token/username pair is internally inconsistent -- fix whichever one is "
                "wrong in deploy/.env before doing anything else.",
                file=sys.stderr,
            )
            return 1
        print(f"Token/username consistency: OK (@{me.username} matches TELEGRAM_BOT_USERNAME)")

        button = await bot.get_chat_menu_button()
        print(f"Current chat menu button: {button.model_dump(exclude_none=True)}")

        # Telegram normalizes a bare-domain URL by appending "/" -- a real
        # false negative this caught against production: setChatMenuButton
        # with "https://arada.click" round-tripped as "https://arada.click/"
        # from the very next getChatMenuButton, correct in every way that
        # matters (the WebView opens the same page either way) but not
        # byte-equal, which made this check report "still broken" forever
        # after a genuinely successful --fix.
        is_correct_web_app = (
            isinstance(button, MenuButtonWebApp)
            and settings.miniapp_url
            and button.web_app.url.rstrip("/") == settings.miniapp_url.rstrip("/")
        )
        if is_correct_web_app:
            print(f"Menu button already correctly launches the Mini App at {settings.miniapp_url}.")
            return 0

        if not settings.miniapp_url:
            print(
                "MINIAPP_URL is not configured -- nothing safe to point the menu button at. "
                "Set MINIAPP_URL in deploy/.env first.",
                file=sys.stderr,
            )
            return 1

        print(
            f"Menu button does NOT currently launch the Mini App at {settings.miniapp_url} -- "
            "this is consistent with the reported blank-screen/rejected-session symptom if this "
            "is the launch surface players are actually using."
        )
        if not fix:
            print("Re-run with --fix to correct it.")
            return 1

        play_text = t("menu.play", "am")
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=play_text, web_app=WebAppInfo(url=settings.miniapp_url))
        )
        print(f"Fixed: menu button now launches {settings.miniapp_url} (text={play_text!r}).")
        return 0
    finally:
        if owns_bot:
            await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose (and optionally fix) the Telegram bot's chat menu button."
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Correct the menu button to a real web_app launch if it isn't one already.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.fix))


if __name__ == "__main__":
    sys.exit(main())
