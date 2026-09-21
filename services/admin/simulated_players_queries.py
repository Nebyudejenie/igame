"""Simulated Players: up to MAX_SIMULATED_PLAYERS admin-controlled bot accounts that join
real rooms and play through the real game engine (services/engine/
round_engine.py, via services/engine/commands.py -- the same channel a
real player's WebSocket action travels) so a room doesn't feel empty
during early launch.

Bots stake/win through the exact same user_cash -> pot_escrow ->
user_cash/house_revenue ledger flow real players use -- zero changes to
round_engine.py's money logic, so settlement math for a room mixing bots
and real players is byte-for-byte the same as real-players-only.
pot_escrow/house_revenue are singleton, platform-wide accounts (see
migrations/versions/81d041ff4513_ledger_foundation.py); a separate
sim_pot_escrow-style kind would mean splitting one shared pot across two
accounts while still needing one correct total for derash math -- a real
double-accounting/leak risk in exactly the mixed-room scenario this
feature exists to create.

Isolation from genuine customer money instead comes from three layers:
(1) a bot's balance is funded from house_float via the exact pattern
services/admin/queries.py::adjust_balance() already uses -- a real,
audited operational cost, never a fabricated deposit; (2) users.
is_simulated lets every report already joined to `users` filter bots out
with one clause (see queries.py's dashboard_summary()/retention_
cohorts()); (3) services/payments/withdrawals.py::request_withdrawal()
refuses a simulated user outright.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core import ledger
from services.admin import audit

INITIAL_SIMULATED_BALANCE = Decimal("5000.00")
MAX_SIMULATED_PLAYERS = 200

STATUSES = ("disabled", "idle", "joining", "playing", "paused")
STRATEGIES = ("conservative", "normal", "active", "randomized")
SCHEDULE_MODES = ("always_on", "scheduled_window", "room_specific")

# Fixed key for a single advisory lock serializing roster-cap checks --
# COUNT(*) can't be combined with FOR UPDATE (Postgres rejects FOR UPDATE
# alongside an aggregate), and this is a human-paced admin action, not a
# hot path, so a single global advisory lock (same primitive round_engine
# .py's own per-user stake path already uses at join()) is simpler than
# reshaping the count into a lockable row set.
_ROSTER_LOCK_KEY = 927341001

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "disabled": {"idle"},
    "idle": {"paused", "disabled"},
    "joining": {"paused", "disabled"},
    "playing": {"paused", "disabled"},
    "paused": {"idle", "disabled"},
}


class InvalidSimulatedPlayerTransition(ValueError):
    pass


class SimulatedPlayerRosterFull(ValueError):
    pass


class SimulatedPlayerNotFound(ValueError):
    pass


async def _fund_simulated_player_in_transaction(
    conn: ledger.AsyncpgConnection,
    *,
    user_id: int,
    amount: Decimal,
    admin_id: int,
    action: str,
    ip_address: str | None,
) -> dict[str, str]:
    """Exact shape of services/admin/queries.py::adjust_balance():
    house_float-backed, kind='adjustment', never a raw UPDATE. Always
    called from inside a caller's own already-open transaction (create,
    and reset), so the audit entry can never be separated from the money
    movement -- same reasoning services/engine/refunds.py::
    refund_round_in_transaction() already documents for itself. A signed
    `amount` (negative to debit the bot back toward house_float) is what
    lets reset_simulated_player_admin reuse this for a bot sitting above
    the target balance too.
    """
    idempotency_key = f"simplayer-fund-{user_id}-{uuid.uuid4().hex}"
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    before = await ledger.balance(conn, cash.id)
    txn = await ledger.post(
        conn,
        "adjustment",
        [ledger.Entry(house_float.id, -amount), ledger.Entry(cash.id, amount)],
        idempotency_key=idempotency_key,
        created_by=f"admin:{admin_id}",
        memo=f"simulated_player_seed:user_{user_id}",
    )
    after = await ledger.balance(conn, cash.id)
    await audit.record(
        conn,
        admin_id=admin_id,
        action=action,
        target_type="simulated_player",
        target_id=str(user_id),
        before={"cash_balance": str(before)},
        after={"cash_balance": str(after), "ledger_transaction_id": txn.id},
        ip_address=ip_address,
    )
    return {"before": str(before), "after": str(after)}


async def active_simulated_player_count(pool: asyncpg.Pool) -> int:
    """How many bots should currently read as "present" on the platform --
    the same signal a real player's own open WebSocket gives the Rooms
    screen's "online" headcount (services/gateway/connection.py), which a
    bot has no other way to contribute to (this module's own docstring:
    bots act via services.engine.commands.send_command(), never a
    WebSocket of their own). 'idle'/'joining'/'playing' all count -- an
    idle bot between rounds is still meant to look like a real user
    browsing rooms, exactly like a real player who's connected but not in
    a round yet; 'disabled'/'paused' don't, since an admin explicitly
    stopped those. Gated on the global settings.enabled switch too, belt
    and suspenders: if the whole feature is off, nothing here should
    count regardless of whatever an individual row's status still says.
    """
    enabled = await pool.fetchval("SELECT enabled FROM simulated_players_settings WHERE id = 1")
    if not enabled:
        return 0
    count = await pool.fetchval(
        "SELECT count(*) FROM simulated_players WHERE status IN ('idle', 'joining', 'playing')"
    )
    return int(count)


async def list_simulated_players(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT u.id AS user_id, u.display_name, u.created_at,
               sp.status, sp.strategy, sp.join_probability_pct, sp.max_cards_per_join,
               sp.schedule_mode, sp.schedule_window_start_minute, sp.schedule_window_end_minute,
               sp.pinned_room_id, sp.current_room_id, sp.last_activity_at, sp.activated_at
        FROM simulated_players sp
        JOIN users u ON u.id = sp.user_id
        ORDER BY sp.created_at
        """
    )
    out: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        for row in rows:
            cash = await ledger.get_or_create_account(conn, row["user_id"], "user_cash")
            stats = await simulated_player_stats(pool, row["user_id"])
            out.append(
                {
                    "user_id": row["user_id"],
                    "display_name": row["display_name"],
                    "created_at": row["created_at"].isoformat(),
                    "status": row["status"],
                    "strategy": row["strategy"],
                    "join_probability_pct": row["join_probability_pct"],
                    "max_cards_per_join": row["max_cards_per_join"],
                    "schedule_mode": row["schedule_mode"],
                    "schedule_window_start_minute": row["schedule_window_start_minute"],
                    "schedule_window_end_minute": row["schedule_window_end_minute"],
                    "pinned_room_id": row["pinned_room_id"],
                    "current_room_id": row["current_room_id"],
                    "last_activity_at": row["last_activity_at"].isoformat() if row["last_activity_at"] else None,
                    "activated_at": row["activated_at"].isoformat() if row["activated_at"] else None,
                    "balance": str(await ledger.balance(conn, cash.id)),
                    **stats,
                }
            )
    return out


async def simulated_player_stats(pool: asyncpg.Pool, user_id: int) -> dict[str, int]:
    """Computed on demand from round_entries/round_winners, the same way
    services/gateway/queries.py::user_history() and services/admin/
    queries.py::player_ltv() already compute real-player stats -- no
    stored counter columns to drift out of sync, and with at most 10
    bots this is cheap.
    """
    row = await pool.fetchrow(
        """
        SELECT
          count(DISTINCT re.round_id) AS games_played,
          count(DISTINCT rw.round_id) AS games_won
        FROM round_entries re
        LEFT JOIN round_winners rw ON rw.round_id = re.round_id AND rw.user_id = re.user_id
        WHERE re.user_id = $1
        """,
        user_id,
    )
    assert row is not None
    games_played = int(row["games_played"])
    games_won = int(row["games_won"])
    return {
        "games_played": games_played,
        "games_won": games_won,
        "games_lost": games_played - games_won,
    }


async def get_settings_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    row = await pool.fetchrow("SELECT * FROM simulated_players_settings WHERE id = 1")
    assert row is not None
    return {
        "enabled": row["enabled"],
        "max_concurrent_bots": row["max_concurrent_bots"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def update_settings_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    enabled: bool,
    max_concurrent_bots: int,
    reason: str,
    ip_address: str | None,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("reason is required")
    if not 0 <= max_concurrent_bots <= MAX_SIMULATED_PLAYERS:
        raise ValueError(f"max_concurrent_bots must be between 0 and {MAX_SIMULATED_PLAYERS}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT enabled, max_concurrent_bots FROM simulated_players_settings WHERE id = 1 FOR UPDATE"
            )
            assert before is not None
            await conn.execute(
                "UPDATE simulated_players_settings SET enabled = $1, max_concurrent_bots = $2, "
                "updated_by_admin_id = $3, updated_at = now() WHERE id = 1",
                enabled,
                max_concurrent_bots,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="simulated_players.settings_update",
                target_type="simulated_players_settings",
                target_id="1",
                before=dict(before),
                after={"enabled": enabled, "max_concurrent_bots": max_concurrent_bots},
                reason=reason,
                ip_address=ip_address,
            )
    return await get_settings_admin(pool)


async def create_simulated_player(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    display_name: str,
    strategy: str,
    ip_address: str | None,
) -> int:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy!r}")
    if not display_name.strip():
        raise ValueError("display_name is required")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _ROSTER_LOCK_KEY)
            roster_size = await conn.fetchval("SELECT count(*) FROM simulated_players")
            if roster_size >= MAX_SIMULATED_PLAYERS:
                raise SimulatedPlayerRosterFull(
                    f"the simulated-player roster is already at its {MAX_SIMULATED_PLAYERS}-bot cap"
                )

            # Real Telegram user ids are always positive -- any negative
            # value can never collide with one, now or ever, so this is a
            # permanent, collision-proof placeholder rather than a
            # reserved range that could theoretically run out. Scoped to
            # telegram_id < 0 specifically (not MIN() over the whole
            # table): the smallest *real* telegram_id already in the
            # table could itself be some large positive number, so
            # `MIN(all rows) - 1` is not guaranteed negative on a fresh
            # roster -- only decrementing from the most negative existing
            # value (or 0, if this is the very first bot ever) is.
            next_telegram_id = await conn.fetchval(
                "SELECT COALESCE(MIN(telegram_id), 0) - 1 FROM users WHERE telegram_id < 0"
            )
            user_row = await conn.fetchrow(
                """
                INSERT INTO users (telegram_id, display_name, is_simulated, status, language, kyc_level)
                VALUES ($1, $2, true, 'active', 'am', 0)
                RETURNING id
                """,
                next_telegram_id,
                display_name.strip(),
            )
            assert user_row is not None
            user_id: int = user_row["id"]

            await conn.execute(
                """
                INSERT INTO simulated_players (user_id, strategy, created_by_admin_id)
                VALUES ($1, $2, $3)
                """,
                user_id,
                strategy,
                admin_id,
            )

            await _fund_simulated_player_in_transaction(
                conn,
                user_id=user_id,
                amount=INITIAL_SIMULATED_BALANCE,
                admin_id=admin_id,
                action="simulated_players.fund",
                ip_address=ip_address,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="simulated_players.create",
                target_type="simulated_player",
                target_id=str(user_id),
                after={"display_name": display_name.strip(), "strategy": strategy},
                ip_address=ip_address,
            )
    return user_id


async def set_strategy_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    strategy: str,
    join_probability_pct: int,
    max_cards_per_join: int,
    schedule_mode: str,
    schedule_window_start_minute: int | None,
    schedule_window_end_minute: int | None,
    pinned_room_id: int | None,
    reason: str,
    ip_address: str | None,
) -> None:
    if not reason.strip():
        raise ValueError("reason is required")
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy!r}")
    if schedule_mode not in SCHEDULE_MODES:
        raise ValueError(f"unknown schedule_mode: {schedule_mode!r}")
    if not 0 <= join_probability_pct <= 100:
        raise ValueError("join_probability_pct must be between 0 and 100")
    if not 1 <= max_cards_per_join <= 4:
        raise ValueError("max_cards_per_join must be between 1 and 4")

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT * FROM simulated_players WHERE user_id = $1 FOR UPDATE", user_id
            )
            if before is None:
                raise SimulatedPlayerNotFound(f"no such simulated player: {user_id}")
            await conn.execute(
                """
                UPDATE simulated_players
                SET strategy = $2, join_probability_pct = $3, max_cards_per_join = $4,
                    schedule_mode = $5, schedule_window_start_minute = $6,
                    schedule_window_end_minute = $7, pinned_room_id = $8, updated_at = now()
                WHERE user_id = $1
                """,
                user_id,
                strategy,
                join_probability_pct,
                max_cards_per_join,
                schedule_mode,
                schedule_window_start_minute,
                schedule_window_end_minute,
                pinned_room_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="simulated_players.set_strategy",
                target_type="simulated_player",
                target_id=str(user_id),
                before={"strategy": before["strategy"], "join_probability_pct": before["join_probability_pct"]},
                after={"strategy": strategy, "join_probability_pct": join_probability_pct},
                reason=reason,
                ip_address=ip_address,
            )


async def _transition_bot(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    to_status: str,
    action: str,
    reason: str | None,
    ip_address: str | None,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, activated_at FROM simulated_players WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if row is None:
                raise SimulatedPlayerNotFound(f"no such simulated player: {user_id}")
            current = row["status"]
            allowed = _ALLOWED_TRANSITIONS.get(current, set())
            if to_status not in allowed:
                raise InvalidSimulatedPlayerTransition(
                    f"cannot move a {current!r} simulated player to {to_status!r}"
                )
            clear_room = to_status in ("disabled", "paused", "idle")
            set_activated = row["activated_at"] is None and to_status != "disabled"
            await conn.execute(
                f"""
                UPDATE simulated_players
                SET status = $2, updated_at = now()
                    {", current_room_id = NULL" if clear_room else ""}
                    {", activated_at = now()" if set_activated else ""}
                WHERE user_id = $1
                """,
                user_id,
                to_status,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action=action,
                target_type="simulated_player",
                target_id=str(user_id),
                before={"status": current},
                after={"status": to_status},
                reason=reason,
                ip_address=ip_address,
            )


async def start_simulated_player_admin(
    pool: asyncpg.Pool, *, admin_id: int, user_id: int, reason: str | None, ip_address: str | None
) -> None:
    await _transition_bot(
        pool, admin_id=admin_id, user_id=user_id, to_status="idle",
        action="simulated_players.start", reason=reason, ip_address=ip_address,
    )


async def pause_simulated_player_admin(
    pool: asyncpg.Pool, *, admin_id: int, user_id: int, reason: str | None, ip_address: str | None
) -> None:
    await _transition_bot(
        pool, admin_id=admin_id, user_id=user_id, to_status="paused",
        action="simulated_players.pause", reason=reason, ip_address=ip_address,
    )


async def stop_simulated_player_admin(
    pool: asyncpg.Pool, *, admin_id: int, user_id: int, reason: str | None, ip_address: str | None
) -> None:
    await _transition_bot(
        pool, admin_id=admin_id, user_id=user_id, to_status="disabled",
        action="simulated_players.stop", reason=reason, ip_address=ip_address,
    )


async def reset_simulated_player_admin(
    pool: asyncpg.Pool, *, admin_id: int, user_id: int, reason: str, ip_address: str | None
) -> dict[str, str]:
    if not reason.strip():
        raise ValueError("reason is required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM simulated_players WHERE user_id = $1 FOR UPDATE", user_id
            )
            if row is None:
                raise SimulatedPlayerNotFound(f"no such simulated player: {user_id}")
            await conn.execute(
                "UPDATE simulated_players SET status = 'disabled', current_room_id = NULL, "
                "updated_at = now() WHERE user_id = $1",
                user_id,
            )
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            current_balance = await ledger.balance(conn, cash.id)
            delta = INITIAL_SIMULATED_BALANCE - current_balance
            result = await _fund_simulated_player_in_transaction(
                conn,
                user_id=user_id,
                amount=delta,
                admin_id=admin_id,
                action="simulated_players.reset",
                ip_address=ip_address,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="simulated_players.reset",
                target_type="simulated_player",
                target_id=str(user_id),
                before={"status": row["status"]},
                after={"status": "disabled"},
                reason=reason,
                ip_address=ip_address,
            )
    return result


async def stop_all_simulated_players_admin(
    pool: asyncpg.Pool, *, admin_id: int, reason: str, ip_address: str | None
) -> dict[str, int]:
    """No bulk-action precedent exists anywhere in this codebase, so this
    is modeled directly on services/admin/queries.py::stop_room_admin()'s
    own "FOR UPDATE -> mutate -> audit inside the transaction" shape.
    Flipping every bot's status to 'disabled' plus the global switch to
    false is itself the complete, sufficient stop -- there is no live
    in-process engine reference to reach into (the bot-runner is a
    separate process that only ever reads this same DB state), so the
    real-world stop latency is bounded by the runner's own poll interval,
    the same latency EngineWorker's own claim loop already tolerates for
    picking up newly-active rooms.
    """
    if not reason.strip():
        raise ValueError("reason is required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE simulated_players_settings SET enabled = false, "
                "updated_by_admin_id = $1, updated_at = now() WHERE id = 1",
                admin_id,
            )
            rows = await conn.fetch(
                "SELECT user_id, status FROM simulated_players WHERE status != 'disabled' FOR UPDATE"
            )
            for row in rows:
                await conn.execute(
                    "UPDATE simulated_players SET status = 'disabled', current_room_id = NULL, "
                    "updated_at = now() WHERE user_id = $1",
                    row["user_id"],
                )
                await audit.record(
                    conn,
                    admin_id=admin_id,
                    action="simulated_players.stop",
                    target_type="simulated_player",
                    target_id=str(row["user_id"]),
                    before={"status": row["status"]},
                    after={"status": "disabled"},
                    reason=reason,
                    ip_address=ip_address,
                )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="simulated_players.stop_all",
                target_type="simulated_players_settings",
                target_id="1",
                before={"enabled": True, "stopped_count": len(rows)},
                after={"enabled": False},
                reason=reason,
                ip_address=ip_address,
            )
    return {"stopped_count": len(rows)}


async def simulated_players_daily_activity(pool: asyncpg.Pool, on_date: Any) -> dict[str, Any]:
    """Bot-only stakes/payouts for one day, the isolated counterpart to
    queries.py::dashboard_summary() -- INNER JOIN on is_simulated (not
    LEFT, since this query only ever wants bot rows), surfaced in the
    Simulated Players screen itself rather than the main Dashboard so the
    real-money headline numbers there stay uncluttered. Deliberately does
    NOT attempt a bot-only house_revenue figure: a mixed real+bot round's
    house cut is one shared credit covering every winner together, and
    can't be honestly split by account kind alone (see dashboard_summary
    ()'s own comment).
    """
    row = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM(-e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'stake'), 0)
                AS bot_stakes_today,
            COALESCE(SUM(e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'payout'), 0)
                AS bot_payouts_today,
            count(DISTINCT t.round_id) FILTER (WHERE t.kind = 'stake') AS bot_rounds_today
        FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN ledger_transactions t ON t.id = e.transaction_id
        JOIN users u ON u.id = a.user_id AND u.is_simulated
        WHERE (e.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1
          AND t.kind IN ('stake', 'payout')
        """,
        on_date,
    )
    assert row is not None
    return {
        "bot_stakes_today": str(row["bot_stakes_today"]),
        "bot_payouts_today": str(row["bot_payouts_today"]),
        "bot_rounds_today": row["bot_rounds_today"],
    }
