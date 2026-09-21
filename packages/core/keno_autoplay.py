"""Keno autoplay / multi-race sessions.

One server-driven mechanism for both "autoplay with stop-on-win/stop-on
-loss conditions" and "multi-race" (buy N future rounds at once) -- see
migrations/versions/d1f4b7c92a5e_keno_autoplay.py's own docstring for why
these are one table, not two competing mechanisms. "Multi-race" is this
with a fixed rounds_total and no stop-on-win/loss thresholds; "autoplay"
is the general case.

Every ticket this places goes through packages/core/keno_tickets.py::
place_ticket() -- the exact same validation, exposure-cap, tier-limit,
and responsible-gaming path a manually-placed bet already uses. This
module only decides *when* to call it and what to do with the result; it
never duplicates any of place_ticket()'s own checks, and a rejection from
any of them (insufficient balance, round capacity, a responsible-gaming
block, anything) stops the session rather than silently retrying forever
(spec-adjacent UX research, 2026-09-21: "client can't be trusted to
self-stop for real money" -- the server must be the one deciding to
stop, not just enforcing a stop the client already decided on its own).

Debited per-round as each ticket actually places, never the full
rounds_total * stake upfront -- no escrow, and an insufficient-balance
rejection partway through a sequence just stops the session cleanly
rather than needing refund logic for rounds that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import keno, keno_tickets

logger = structlog.get_logger()

# A sanity ceiling on "buy N future rounds" -- not spec-mandated, an
# operator-safety default against a fat-fingered or malicious huge
# rounds_total locking in an unbounded sequence of real-money bets.
MAX_ROUNDS_TOTAL = 100


class AutoplayError(Exception):
    code = "autoplay_error"


class AutoplaySessionAlreadyActive(AutoplayError):
    code = "autoplay_session_already_active"


class InvalidAutoplayConfig(AutoplayError):
    code = "invalid_autoplay_config"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AutoplaySession:
    id: int
    user_id: int
    picks: frozenset[int]
    stake: Decimal
    rounds_total: int | None
    rounds_placed: int
    stop_on_win_amount: Decimal | None
    stop_on_loss_amount: Decimal | None
    net_position: Decimal
    status: str
    stop_reason: str | None
    last_round_id: int | None


def _session_from_row(row: asyncpg.Record) -> AutoplaySession:
    return AutoplaySession(
        id=row["id"],
        user_id=row["user_id"],
        picks=frozenset(row["picks"]),
        stake=Decimal(row["stake"]),
        rounds_total=row["rounds_total"],
        rounds_placed=row["rounds_placed"],
        stop_on_win_amount=Decimal(row["stop_on_win_amount"]) if row["stop_on_win_amount"] is not None else None,
        stop_on_loss_amount=Decimal(row["stop_on_loss_amount"]) if row["stop_on_loss_amount"] is not None else None,
        net_position=Decimal(row["net_position"]),
        status=row["status"],
        stop_reason=row["stop_reason"],
        last_round_id=row["last_round_id"],
    )


async def start_session(
    pool: asyncpg.Pool,
    *,
    user_id: int,
    picks: list[int],
    stake: Decimal,
    rounds_total: int | None = None,
    stop_on_win_amount: Decimal | None = None,
    stop_on_loss_amount: Decimal | None = None,
) -> AutoplaySession:
    validated_picks = keno.validate_picks(picks)  # same picks validation place_ticket() itself uses
    if stake <= 0:
        raise InvalidAutoplayConfig("stake must be positive")
    if rounds_total is not None and not (0 < rounds_total <= MAX_ROUNDS_TOTAL):
        raise InvalidAutoplayConfig(f"rounds_total must be between 1 and {MAX_ROUNDS_TOTAL}")
    if stop_on_win_amount is not None and stop_on_win_amount <= 0:
        raise InvalidAutoplayConfig("stop_on_win_amount must be positive")
    if stop_on_loss_amount is not None and stop_on_loss_amount <= 0:
        raise InvalidAutoplayConfig("stop_on_loss_amount must be positive")
    # At least one real stop condition required -- a session with no
    # round cap and no win/loss threshold would run forever with nothing
    # to ever end it short of a manual stop, the exact "client can't be
    # trusted, but neither can an unbounded server default" gap this
    # module's own docstring flags.
    if rounds_total is None and stop_on_win_amount is None and stop_on_loss_amount is None:
        raise InvalidAutoplayConfig("at least one of rounds_total, stop_on_win_amount, stop_on_loss_amount is required")

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT id FROM keno_autoplay_sessions WHERE user_id = $1 AND status = 'active'", user_id
            )
            if existing is not None:
                raise AutoplaySessionAlreadyActive(str(existing))
            row = await conn.fetchrow(
                """
                INSERT INTO keno_autoplay_sessions
                    (user_id, picks, stake, rounds_total, stop_on_win_amount, stop_on_loss_amount)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                user_id, list(validated_picks), stake, rounds_total, stop_on_win_amount, stop_on_loss_amount,
            )
    assert row is not None
    return _session_from_row(row)


async def stop_session(pool: asyncpg.Pool, *, user_id: int) -> AutoplaySession | None:
    """Manual stop -- idempotent no-op (returns None) if there's no
    active session, rather than an error; a player tapping Stop twice in
    a row (a genuine double-tap, or a retry after a dropped response)
    must never see a confusing failure on the second tap."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE keno_autoplay_sessions SET status = 'stopped', stop_reason = 'manual', updated_at = now() "
            "WHERE user_id = $1 AND status = 'active' RETURNING *",
            user_id,
        )
    return _session_from_row(row) if row is not None else None


async def active_session(pool: asyncpg.Pool, *, user_id: int) -> AutoplaySession | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM keno_autoplay_sessions WHERE user_id = $1 AND status = 'active'", user_id
        )
    return _session_from_row(row) if row is not None else None


async def _stop_internal(pool: asyncpg.Pool, session_id: int, *, status: str, reason: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE keno_autoplay_sessions SET status = $2, stop_reason = $3, updated_at = now() "
            "WHERE id = $1 AND status = 'active'",
            session_id, status, reason,
        )


async def place_for_active_sessions(pool: asyncpg.Pool, redis: Redis, *, round_id: int) -> None:
    """Called once per round from keno_round_engine.py's own
    _open_betting(), right after a round becomes real -- walks every
    currently active session and places exactly one ticket per session
    into THIS round."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM keno_autoplay_sessions WHERE status = 'active'")
    for row in rows:
        session = _session_from_row(row)
        try:
            await keno_tickets.place_ticket(
                pool, redis,
                user_id=session.user_id,
                picks=list(session.picks),
                stake=session.stake,
                idempotency_key=f"autoplay:{session.id}:{round_id}",
                autoplay_session_id=session.id,
            )
        except keno_tickets.TicketRejected as exc:
            await _stop_internal(pool, session.id, status="stopped", reason=f"ticket_rejected:{exc.code}")
            logger.info("keno_autoplay_session_stopped_on_rejection", session_id=session.id, reason=exc.code)
            continue

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE keno_autoplay_sessions SET rounds_placed = rounds_placed + 1, last_round_id = $2, "
                "updated_at = now() WHERE id = $1",
                session.id, round_id,
            )
        # Exhaustion (rounds_total reached) is checked right after
        # placing, not after settlement -- "bought N rounds" means
        # exactly N tickets placed, independent of how they each turn
        # out. +1 accounts for the ticket just placed above, since
        # session.rounds_placed was read before that UPDATE.
        if session.rounds_total is not None and session.rounds_placed + 1 >= session.rounds_total:
            await _stop_internal(pool, session.id, status="exhausted", reason="rounds_exhausted")


async def record_settlement(
    pool: asyncpg.Pool, *, autoplay_session_id: int | None, stake: Decimal, payout: Decimal, jackpot_payout: Decimal
) -> None:
    """Called from keno_round_engine.py's own _settle_tickets(), once per
    settled ticket -- a no-op if autoplay_session_id is None (true for
    the overwhelming majority of tickets, placed manually). Updates
    net_position and stops the session if either win/loss threshold is
    now crossed."""
    if autoplay_session_id is None:
        return
    delta = (payout + jackpot_payout) - stake
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE keno_autoplay_sessions SET net_position = net_position + $2, updated_at = now() "
            "WHERE id = $1 AND status = 'active' RETURNING *",
            autoplay_session_id, delta,
        )
    if row is None:
        return  # already stopped by something else (manual stop, exhaustion) -- nothing left to check
    session = _session_from_row(row)
    if session.stop_on_win_amount is not None and session.net_position >= session.stop_on_win_amount:
        await _stop_internal(pool, session.id, status="stopped", reason="stop_on_win")
    elif session.stop_on_loss_amount is not None and session.net_position <= -session.stop_on_loss_amount:
        await _stop_internal(pool, session.id, status="stopped", reason="stop_on_loss")
