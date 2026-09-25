"""Responsible-gaming controls (spec section 12, Prompt 9's remaining
scope after Phase 7's admin console): deposit/loss limits with an instant
decrease and a 24-hour delay on any increase, cool-off, and self-exclusion.

Self-exclusion is enforced through `users.status = 'self_excluded'` (the
same column every other status check in this codebase already reads) plus
`self_excluded_until` here for record-keeping -- there is deliberately no
"lift my own self-exclusion" function anywhere in this codebase, which is
what actually makes it irreversible for the period, not a duration check.
Cool-off is purely timestamp-driven (`cooloff_until`), never a status
value, so it lifts itself the moment the timestamp passes with no
scheduled job needed to flip anything back.

Every caller that gates an action (join a round, deposit, appear in a
marketing audience) is expected to call the relevant check here directly --
per the spec's own instruction for the marketing case, "enforce ... by
filtering on users.status inside the notification query itself, not in the
caller", the same principle applies to every other gate in this module:
the check lives in one place, not copy-pasted at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

import asyncpg

from packages.core.ledger import AsyncpgConnection

SELF_EXCLUSION_MINIMUM_DAYS = 180  # spec section 12: "6 months minimum"
LIMIT_INCREASE_DELAY_HOURS = 24

# services/bot/handlers.py has a mechanically-enforced rule (see
# test_bot_no_hardcoded_strings.py) that it may contain no hardcoded string
# literal -- every user-facing string goes through i18n.t(), and every
# domain comparison goes through a named constant or enum member like the
# ones below, never a bare "deposit"/"confirm"/"24h" literal sitting in
# that file for the checker to (rightly) flag.


class LimitsAction(Enum):
    SET_DEPOSIT = "set_deposit"
    SET_LOSS = "set_loss"
    COOL_OFF = "cool_off"
    SELF_EXCLUDE = "self_exclude"


SELF_EXCLUDE_CONFIRMATION_TOKEN = "confirm"

COOLOFF_DURATIONS_HOURS: dict[str, int] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}

_LIMITS_SUBCOMMANDS: dict[str, LimitsAction] = {
    "deposit": LimitsAction.SET_DEPOSIT,
    "loss": LimitsAction.SET_LOSS,
    "cooloff": LimitsAction.COOL_OFF,
    "selfexclude": LimitsAction.SELF_EXCLUDE,
}


@dataclass(frozen=True)
class ParsedLimitsCommand:
    action: LimitsAction | None
    value: str | None


def parse_limits_command(raw_args: str) -> ParsedLimitsCommand:
    """/limits <subcommand> <value> -- e.g. '/limits deposit 500',
    '/limits cooloff 24h', '/limits selfexclude confirm'. Returns
    action=None (with value=None) for anything that doesn't parse, which
    the caller treats as "show usage".
    """
    parts = raw_args.split()
    if len(parts) != 2:
        return ParsedLimitsCommand(None, None)
    action = _LIMITS_SUBCOMMANDS.get(parts[0].lower())
    if action is None:
        return ParsedLimitsCommand(None, None)
    return ParsedLimitsCommand(action, parts[1])


class SelfExclusionTooShort(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    user_id: int
    daily_deposit_cap: Decimal | None
    pending_daily_deposit_cap: Decimal | None
    pending_daily_deposit_cap_effective_at: datetime | None
    daily_loss_cap: Decimal | None
    pending_daily_loss_cap: Decimal | None
    pending_daily_loss_cap_effective_at: datetime | None
    cooloff_until: datetime | None
    self_excluded_until: datetime | None


async def get_or_create_limits(conn: AsyncpgConnection, user_id: int) -> Limits:
    columns = (
        "user_id, daily_deposit_cap, pending_daily_deposit_cap, "
        "pending_daily_deposit_cap_effective_at, daily_loss_cap, pending_daily_loss_cap, "
        "pending_daily_loss_cap_effective_at, cooloff_until, self_excluded_until"
    )
    row = await conn.fetchrow(
        f"SELECT {columns} FROM responsible_gaming_limits WHERE user_id = $1", user_id
    )
    if row is None:
        row = await conn.fetchrow(
            f"""
            INSERT INTO responsible_gaming_limits (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING {columns}
            """,
            user_id,
        )
        if row is None:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM responsible_gaming_limits WHERE user_id = $1", user_id
            )
    assert row is not None
    return Limits(**dict(row))


def effective_deposit_cap(limits: Limits, *, now: datetime | None = None) -> Decimal | None:
    return _effective(
        limits.daily_deposit_cap,
        limits.pending_daily_deposit_cap,
        limits.pending_daily_deposit_cap_effective_at,
        now,
    )


def effective_loss_cap(limits: Limits, *, now: datetime | None = None) -> Decimal | None:
    return _effective(
        limits.daily_loss_cap, limits.pending_daily_loss_cap, limits.pending_daily_loss_cap_effective_at, now
    )


def _effective(
    current: Decimal | None, pending: Decimal | None, pending_effective_at: datetime | None, now: datetime | None
) -> Decimal | None:
    if pending is not None and pending_effective_at is not None:
        if (now or datetime.now(UTC)) >= pending_effective_at:
            return pending
    return current


async def set_deposit_limit(conn: AsyncpgConnection, user_id: int, new_cap: Decimal) -> bool:
    """Returns True if the new cap applies immediately (a decrease, or no
    cap was set before), False if it's a increase deferred 24 hours.
    """
    limits = await get_or_create_limits(conn, user_id)
    current = effective_deposit_cap(limits)
    if current is None or new_cap <= current:
        await conn.execute(
            "UPDATE responsible_gaming_limits SET daily_deposit_cap = $2, "
            "pending_daily_deposit_cap = NULL, pending_daily_deposit_cap_effective_at = NULL, "
            "updated_at = now() WHERE user_id = $1",
            user_id,
            new_cap,
        )
        return True

    effective_at = datetime.now(UTC) + timedelta(hours=LIMIT_INCREASE_DELAY_HOURS)
    await conn.execute(
        "UPDATE responsible_gaming_limits SET pending_daily_deposit_cap = $2, "
        "pending_daily_deposit_cap_effective_at = $3, updated_at = now() WHERE user_id = $1",
        user_id,
        new_cap,
        effective_at,
    )
    return False


async def set_loss_limit(conn: AsyncpgConnection, user_id: int, new_cap: Decimal) -> bool:
    """Same immediate-vs-deferred contract as set_deposit_limit()."""
    limits = await get_or_create_limits(conn, user_id)
    current = effective_loss_cap(limits)
    if current is None or new_cap <= current:
        await conn.execute(
            "UPDATE responsible_gaming_limits SET daily_loss_cap = $2, "
            "pending_daily_loss_cap = NULL, pending_daily_loss_cap_effective_at = NULL, "
            "updated_at = now() WHERE user_id = $1",
            user_id,
            new_cap,
        )
        return True

    effective_at = datetime.now(UTC) + timedelta(hours=LIMIT_INCREASE_DELAY_HOURS)
    await conn.execute(
        "UPDATE responsible_gaming_limits SET pending_daily_loss_cap = $2, "
        "pending_daily_loss_cap_effective_at = $3, updated_at = now() WHERE user_id = $1",
        user_id,
        new_cap,
        effective_at,
    )
    return False


async def cool_off(conn: AsyncpgConnection, user_id: int, duration_hours: int) -> None:
    await get_or_create_limits(conn, user_id)
    until = datetime.now(UTC) + timedelta(hours=duration_hours)
    await conn.execute(
        "UPDATE responsible_gaming_limits SET cooloff_until = $2, updated_at = now() WHERE user_id = $1",
        user_id,
        until,
    )


async def self_exclude(
    pool: asyncpg.Pool, user_id: int, *, days: int = SELF_EXCLUSION_MINIMUM_DAYS
) -> None:
    if days < SELF_EXCLUSION_MINIMUM_DAYS:
        raise SelfExclusionTooShort(
            f"self-exclusion must be at least {SELF_EXCLUSION_MINIMUM_DAYS} days, got {days}"
        )
    until = datetime.now(UTC) + timedelta(days=days)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await get_or_create_limits(conn, user_id)
            await conn.execute("UPDATE users SET status = 'self_excluded' WHERE id = $1", user_id)
            await conn.execute(
                "UPDATE responsible_gaming_limits SET self_excluded_until = $2, updated_at = now() "
                "WHERE user_id = $1",
                user_id,
                until,
            )


@dataclass(frozen=True)
class PlayBlock:
    blocked: bool
    reason: str | None  # 'self_excluded' | 'cooling_off' | 'banned' | None


async def check_play_allowed(conn: AsyncpgConnection, user_id: int) -> PlayBlock:
    row = await conn.fetchrow(
        """
        SELECT u.status, r.cooloff_until
        FROM users u
        LEFT JOIN responsible_gaming_limits r ON r.user_id = u.id
        WHERE u.id = $1
        """,
        user_id,
    )
    return _play_block_from_row(row)


def _play_block_from_row(row: asyncpg.Record | None) -> PlayBlock:
    if row is None:
        return PlayBlock(False, None)
    if row["status"] == "self_excluded":
        return PlayBlock(True, "self_excluded")
    if row["status"] == "banned":
        return PlayBlock(True, "banned")
    cooloff_until = row["cooloff_until"]
    if cooloff_until is not None and datetime.now(UTC) < cooloff_until:
        return PlayBlock(True, "cooling_off")
    return PlayBlock(False, None)


async def check_stake_allowed(conn: AsyncpgConnection, user_id: int, stake: Decimal) -> PlayBlock:
    """Same gate as check_play_allowed() plus the loss-cap check, in as few
    round-trips as the common case allows: one combined query covers
    status, cool-off, and the loss-cap fields together (a plain SELECT,
    not get_or_create_limits()'s insert-if-missing path, since a missing
    row and an all-NULL row mean the same thing for a read); a second
    query for today's realized loss only runs when a cap is actually set.
    This is round_engine.join()'s hot path -- called on every stake --
    so it's written to add as little latency as the common no-limits-set
    case allows.
    """
    row = await conn.fetchrow(
        """
        SELECT u.status, r.cooloff_until,
               r.daily_loss_cap, r.pending_daily_loss_cap, r.pending_daily_loss_cap_effective_at
        FROM users u
        LEFT JOIN responsible_gaming_limits r ON r.user_id = u.id
        WHERE u.id = $1
        """,
        user_id,
    )
    block = _play_block_from_row(row)
    if block.blocked or row is None:
        return block

    loss_cap = _effective(
        row["daily_loss_cap"], row["pending_daily_loss_cap"], row["pending_daily_loss_cap_effective_at"], None
    )
    if loss_cap is not None:
        net_loss = await today_net_loss(conn, user_id)
        if net_loss + stake > loss_cap:
            return PlayBlock(True, "loss_limit_reached")

    return PlayBlock(False, None)


# Every game's own stake/payout ledger-transaction kinds -- one combined
# daily loss cap across the whole platform, not a per-game one (operator
# decision, 2026-09-21: a player down 500 on Bingo and 500 on Keno has
# genuinely lost 1,000 today, not two separate 500s that don't count
# against each other). A future third game adds its own kinds here, not
# a second copy of today_net_loss()'s own query. keno_jackpot_payout has
# no Bingo equivalent; included on its own merits -- a real win credited
# to user_cash, same as an ordinary payout.
_STAKE_KINDS = ("stake", "keno_stake")
_PAYOUT_KINDS = ("payout", "keno_payout", "keno_jackpot_payout")

# Refunded stakes are not losses (operator decision, 2026-09-25): a
# player who drops a Bingo card, or whose Keno round we voided, got the
# money back and must not be locked out by it. Two rules keep this from
# ever letting someone lose more than their cap:
#
# 1. Only *stake* refunds count. The "refund" kind is also posted when a
#    withdrawal is rejected, fails or is reversed (user_locked back to
#    user_cash, payment_id set, no round_id). That money was never staked;
#    subtracting it would hand a player with a rejected 5,000 withdrawal
#    5,000 of extra loss headroom.
# 2. A refund offsets today's loss only if the stake it returns was also
#    placed today. A stake placed at 23:59 and refunded at 00:01 was
#    counted against yesterday; letting its refund offset today would let
#    the player lose more than their cap today in real terms.
#
# Each refund is matched to its own stake through the static idempotency
# keys both games already rely on for exactly-once posting: Bingo's
# drop-/refund-{round}-{user}-{card} returns stake-{round}-{user}-{card}
# (round_engine.join/drop_card, refunds.refund_round_in_transaction), and
# Keno's keno:refund:{round}:{ticket} returns that ticket's stake_txn_id
# (keno_round_engine._refund_one_ticket). Changing either key format
# silently turns refunds back into losses -- test_responsible_gaming_
# paths.py covers every refund path so that can't happen unnoticed.
_REFUND_KINDS = ("refund", "keno_refund")


async def today_net_loss(conn: AsyncpgConnection, user_id: int) -> Decimal:
    """Stakes debited from user_cash today, minus payouts credited to it
    today, minus refunds of stakes placed today (see _REFUND_KINDS), across
    every game -- a positive number means the player is net down for the
    day.

    "Today" means the Ethiopian calendar day, not the Postgres session's
    ambient (UTC-by-default) one -- a bare `date_trunc('day', now())`
    would reset this loss-cap window three hours early/late every night
    (see DECISIONS.md's "day-boundary timezone mismatch" entry, the same
    class of bug already fixed for the admin dashboard's daily figures).
    """
    row = await conn.fetchrow(
        """
        WITH day AS (
            SELECT date_trunc('day', now() AT TIME ZONE 'Africa/Addis_Ababa') AT TIME ZONE 'Africa/Addis_Ababa' AS start
        ),
        mine AS (
            SELECT e.account_id, e.amount, t.kind, t.round_id, t.idempotency_key
            FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            JOIN ledger_transactions t ON t.id = e.transaction_id
            CROSS JOIN day
            WHERE a.user_id = $1 AND a.kind = 'user_cash' AND t.kind = ANY($2::text[] || $3::text[] || $4::text[])
              AND e.created_at >= day.start
        ),
        classified AS (
            SELECT m.*,
                CASE
                    WHEN m.kind = 'refund' THEN m.round_id IS NOT NULL AND EXISTS (
                        SELECT 1
                        FROM ledger_transactions st
                        JOIN ledger_entries se ON se.transaction_id = st.id AND se.account_id = m.account_id
                        CROSS JOIN day
                        WHERE st.idempotency_key = regexp_replace(m.idempotency_key, '^(drop|refund)-', 'stake-')
                          AND st.kind = 'stake' AND se.created_at >= day.start
                    )
                    WHEN m.kind = 'keno_refund' THEN EXISTS (
                        SELECT 1
                        FROM keno_tickets kt
                        JOIN ledger_entries se ON se.transaction_id = kt.stake_txn_id AND se.account_id = m.account_id
                        CROSS JOIN day
                        WHERE kt.id = substring(m.idempotency_key FROM '^keno:refund:[0-9]+:([0-9]+)$')::bigint
                          AND se.created_at >= day.start
                    )
                    ELSE false
                END AS returns_a_stake_from_today
            FROM mine m
        )
        SELECT
            COALESCE(SUM(-amount) FILTER (WHERE kind = ANY($2::text[])), 0) AS staked,
            COALESCE(SUM(amount) FILTER (WHERE kind = ANY($3::text[])), 0) AS won,
            COALESCE(SUM(amount) FILTER (WHERE kind = ANY($4::text[]) AND returns_a_stake_from_today), 0) AS refunded
        FROM classified
        """,
        user_id,
        list(_STAKE_KINDS),
        list(_PAYOUT_KINDS),
        list(_REFUND_KINDS),
    )
    assert row is not None
    staked: Decimal = row["staked"]
    won: Decimal = row["won"]
    refunded: Decimal = row["refunded"]
    return staked - won - refunded


async def marketing_eligible_user_ids(pool: asyncpg.Pool) -> list[int]:
    """The one query any future marketing/promotional send must use for its
    audience -- self-excluded, banned, and currently-cooling-off users are
    filtered out here, at the query itself, so a future caller can't forget
    to check (spec section 12: "segment your notification queries by
    users.status at the query level so it can't be forgotten").
    """
    rows = await pool.fetch(
        """
        SELECT u.id FROM users u
        LEFT JOIN responsible_gaming_limits r ON r.user_id = u.id
        WHERE u.status NOT IN ('self_excluded', 'banned')
          AND (r.cooloff_until IS NULL OR r.cooloff_until <= now())
        """
    )
    return [row["id"] for row in rows]
