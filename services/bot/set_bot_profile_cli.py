"""One-shot CLI to set the Telegram bot's own discoverability metadata --
short description, full description, and the /start-menu command list --
the fields Telegram itself surfaces in bot search results, the bot's
profile screen (before a user has ever started a chat), and link-share
previews. Nothing in this repo has ever called setMyShortDescription/
setMyDescription/setMyCommands: a real check against production
(2026-09-18, via getMyShortDescription/getMyDescription) found both
description fields completely empty, and getMyCommands showed /start
still described as "Start Jo Bingo" -- an earlier product name, never
updated when the product became Arada Bingo. This is the durable,
version-controlled source of truth for that metadata going forward, so a
future token rotation or fresh bot doesn't silently lose it again the
way it evidently already has once.

Bilingual by the same convention as everywhere else in this codebase
(services/bot/locales/*.json): a "" (default, shown to any user whose
Telegram client language has no dedicated variant) entry plus explicit
"en"/"am" entries, matching the two languages the product actually ships
real (non-stub) copy for today.

Run: `python -m services.bot.set_bot_profile_cli` to report the current
state only (never writes anything). Add `--fix` to apply the target copy
below, but only to whatever field(s)/language(s) actually differ from
it -- safe to re-run any time, a clean run makes zero API calls.

Never prints the bot token -- same discipline as verify_menu_button.py.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.types import BotCommand

from packages.core.config import Settings, get_settings
from services.bot.app import build_bot

TARGET_SHORT_DESCRIPTION: dict[str, str] = {
    "": "🎉 Real Bingo, real cash — play live Bingo on Telegram and win real money instantly.",
    "en": "🎉 Real Bingo, real cash — play live Bingo on Telegram and win real money instantly.",
    "am": "🎉 እውነተኛ ቢንጎ፣ እውነተኛ ገንዘብ — በቴሌግራም ቀጥታ ቢንጎ ይጫወቱ፣ ወዲያውኑ ገንዘብ ያሸንፉ።",
}

TARGET_DESCRIPTION: dict[str, str] = {
    "": (
        "Arada Bingo — Ethiopia's live multiplayer Bingo on Telegram. Join a room, grab a "
        "card, and play real-money Bingo with real players right now. Fast rounds, instant "
        "Telebirr & Chapa deposits/withdrawals, and every draw is provably fair. Tap Start "
        "— no app download needed."
    ),
    "en": (
        "Arada Bingo — Ethiopia's live multiplayer Bingo on Telegram. Join a room, grab a "
        "card, and play real-money Bingo with real players right now. Fast rounds, instant "
        "Telebirr & Chapa deposits/withdrawals, and every draw is provably fair. Tap Start "
        "— no app download needed."
    ),
    "am": (
        "አራዳ ቢንጎ — የኢትዮጵያ ቀጥታ የብዙ ተጫዋቾች ቢንጎ በቴሌግራም። ክፍል ይቀላቀሉ፣ ካርድ ይያዙ፣ በገንዘብ ከሌሎች "
        "ተጫዋቾች ጋር አሁኑኑ ይጫወቱ። ፈጣን ዙሮች፣ ፈጣን በቴሌብር እና ቻፓ ገቢ/ወጪ፣ እያንዳንዱ ዕጣ በትክክል ፍትሃዊ "
        "መሆኑ ይረጋገጣል። ለመጀመር ጀምር የሚለውን ይንኩ — መተግበሪያ ማውረድ አያስፈልግም።"
    ),
}

# The "" (default) command set doubles as the English one, same reasoning
# TARGET_SHORT_DESCRIPTION/TARGET_DESCRIPTION use "" for.
TARGET_COMMANDS_EN: list[tuple[str, str]] = [
    ("start", "Start Arada Bingo"),
    ("play", "Open game"),
    ("balance", "Check balance"),
    ("history", "Game history"),
    ("deposit", "Deposit funds"),
    ("withdraw", "Withdraw funds"),
    ("invite", "Invite friends"),
    ("rules", "Game rules"),
    ("support", "Get support"),
    ("language", "Change language"),
    ("limits", "Set play limits"),
]

TARGET_COMMANDS_AM: list[tuple[str, str]] = [
    ("start", "አራዳ ቢንጎን ጀምር"),
    ("play", "ጨዋታ ክፈት"),
    ("balance", "ቀሪ ሂሳብ ይመልከቱ"),
    ("history", "የጨዋታ ታሪክ"),
    ("deposit", "ገቢ ያድርጉ"),
    ("withdraw", "ወጪ ያድርጉ"),
    ("invite", "ጓደኞችን ይጋብዙ"),
    ("rules", "የጨዋታ ህጎች"),
    ("support", "ድጋፍ ያግኙ"),
    ("language", "ቋንቋ ይቀይሩ"),
    ("limits", "የጨዋታ ገደብ ያዘጋጁ"),
]

TARGET_COMMANDS: dict[str, list[tuple[str, str]]] = {"": TARGET_COMMANDS_EN, "am": TARGET_COMMANDS_AM}


def _lang_tag(language_code: str) -> str:
    return language_code or "default"


async def _reconcile_text_field(
    label: str,
    *,
    target_by_lang: dict[str, str],
    getter: Callable[[str], Awaitable[str]],
    setter: Callable[[str, str], Awaitable[None]],
    fix: bool,
) -> bool:
    """Shared diff-then-optionally-fix loop for the two plain-string
    fields (short_description/description) -- commands have a different
    shape (a list, not a string) and are reconciled separately below.
    Returns True if anything was (or would be) out of date.
    """
    any_mismatch = False
    for language_code, target in target_by_lang.items():
        current = await getter(language_code)
        tag = _lang_tag(language_code)
        if current == target:
            print(f"{label} [{tag}]: OK")
            continue
        any_mismatch = True
        print(f"{label} [{tag}]: MISMATCH")
        print(f"  current: {current!r}")
        print(f"  target:  {target!r}")
        if fix:
            await setter(target, language_code)
            print("  -> fixed")
    return any_mismatch


async def _run(fix: bool, *, settings: Settings | None = None, bot: Bot | None = None) -> int:
    """settings/bot are overridable only so tests can inject a fake bot
    without hitting the real Telegram API -- the real CLI entrypoint
    (main(), below) always calls this with neither.
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

        any_mismatch = False

        async def get_short(lc: str) -> str:
            result = await bot.get_my_short_description(language_code=lc or None)
            return result.short_description

        async def set_short(value: str, lc: str) -> None:
            await bot.set_my_short_description(short_description=value, language_code=lc or None)

        any_mismatch |= await _reconcile_text_field(
            "short_description", target_by_lang=TARGET_SHORT_DESCRIPTION,
            getter=get_short, setter=set_short, fix=fix,
        )

        async def get_description(lc: str) -> str:
            result = await bot.get_my_description(language_code=lc or None)
            return result.description

        async def set_description(value: str, lc: str) -> None:
            await bot.set_my_description(description=value, language_code=lc or None)

        any_mismatch |= await _reconcile_text_field(
            "description", target_by_lang=TARGET_DESCRIPTION,
            getter=get_description, setter=set_description, fix=fix,
        )

        for language_code, target_commands in TARGET_COMMANDS.items():
            tag = _lang_tag(language_code)
            current_commands = await bot.get_my_commands(language_code=language_code or None)
            current_pairs = [(c.command, c.description) for c in current_commands]
            if current_pairs == target_commands:
                print(f"commands [{tag}]: OK")
                continue
            any_mismatch = True
            print(f"commands [{tag}]: MISMATCH")
            print(f"  current: {current_pairs!r}")
            print(f"  target:  {target_commands!r}")
            if fix:
                await bot.set_my_commands(
                    commands=[BotCommand(command=c, description=d) for c, d in target_commands],
                    language_code=language_code or None,
                )
                print("  -> fixed")

        if not any_mismatch:
            print("Everything already matches the target copy -- nothing to do.")
            return 0
        if not fix:
            print("Re-run with --fix to apply the target copy above.")
            return 1
        return 0
    finally:
        if owns_bot:
            await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose (and optionally fix) the bot's search/profile metadata."
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Apply the target short_description/description/commands where they differ.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.fix))


if __name__ == "__main__":
    sys.exit(main())
