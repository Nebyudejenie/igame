"""Admin console resource operations: users, rounds, rooms, dashboard,
reports. Every mutation here writes an audit_log entry in the same
transaction as the mutation itself (never as an afterthought) and manual
balance adjustments go through the ledger like any other money movement --
there is no code path anywhere that writes a balance directly, admin
console included (spec section 26/34: "no hidden god mode").
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

import asyncpg
from redis.asyncio import Redis

from packages.core import bingo, ledger, metrics
from packages.core.device_auth import generate_device_token, hash_device_token
from packages.core.notifications import notify_user
from packages.core.phone import normalize_ethiopian_phone
from packages.core.phone_crypto import decrypt_phone, phone_lookup_hash
from packages.core.referrals import maybe_grant_referral_bonus, maybe_grant_welcome_bonus
from services.admin import audit, auth, rbac
from services.engine.refunds import TERMINAL_STATUSES, refund_round_in_transaction
from services.payments.withdrawals import enqueue_payout


class AdminUsernameTaken(ValueError):
    pass


class CannotModifyOwnAccount(ValueError):
    pass


# A code review pass caught dashboard_summary()/daily_ggr() computing
# "today" with Python's date.today() (whatever the admin process's own
# host/container timezone happens to be) while the SQL cast `created_at
# ::date` converts using the *Postgres session's* own timezone setting --
# two independent, unconfigured ambient defaults that were never
# guaranteed to agree, and even if they happened to (both defaulting to
# UTC, say), neither would match the Ethiopian calendar day these reports
# are actually meant to describe. A transaction between 21:00-24:00 UTC
# (00:00-03:00 EAT) is a real, everyday occurrence, not an edge case, so
# this isn't a theoretical risk -- it's routinely wrong by up to 3 hours
# on both sides of midnight. Both sides now compute the boundary the same
# explicit way instead of trusting ambient defaults.
ETHIOPIA_TZ = ZoneInfo("Africa/Addis_Ababa")


def _with_decrypted_phone(row: dict[str, Any]) -> dict[str, Any]:
    blob = row.pop("phone_e164_encrypted")
    row["phone_e164"] = decrypt_phone(bytes(blob)) if blob is not None else None
    return row


def _parse_jsonb(value: Any) -> Any:
    """asyncpg returns a jsonb column as a raw JSON string unless a codec
    is registered for it (none is, anywhere this pool is created) -- a
    code review pass caught three independent, inconsistent copies of
    this same guard in this file (list_rooms(), _room_audit_value(), and
    shared_payout_account_clusters()), one of them missing the
    isinstance guard entirely. One shared helper instead: safe to call
    on a value that's already been parsed (returns it unchanged), so
    every call site can use it unconditionally.
    """
    return json.loads(value) if isinstance(value, str) else value


async def search_users(pool: asyncpg.Pool, query: str, limit: int = 20) -> list[dict[str, Any]]:
    # Phone matching is exact only (spec section 9.2: numbers are
    # encrypted at rest) -- a random-nonce ciphertext can't support
    # substring search, and phone_lookup_hash is only ever computed from a
    # genuinely complete, correctly-formatted E.164 number, so a partial
    # digit string simply matches nothing rather than silently degrading
    # to name/telegram_id-only search. A real product tradeoff, confirmed
    # with the user rather than picked unilaterally -- see DECISIONS.md.
    normalized = normalize_ethiopian_phone(query)
    phone_hash = phone_lookup_hash(normalized) if normalized else None

    rows = await pool.fetch(
        """
        SELECT id, telegram_id, display_name, phone_e164_encrypted, status, kyc_level, created_at,
               is_simulated
        FROM users
        WHERE phone_lookup_hash = $1
           OR display_name ILIKE '%' || $2 || '%'
           OR telegram_id::text = $2
        ORDER BY created_at DESC
        LIMIT $3
        """,
        phone_hash,
        query,
        limit,
    )
    return [_with_decrypted_phone(dict(r)) for r in rows]


async def get_user_detail(pool: asyncpg.Pool, user_id: int) -> dict[str, Any] | None:
    user_row = await pool.fetchrow(
        "SELECT id, telegram_id, display_name, phone_e164_encrypted, status, kyc_level, language, "
        "created_at, last_seen_at, is_simulated FROM users WHERE id = $1",
        user_id,
    )
    if user_row is None:
        return None
    user_dict = _with_decrypted_phone(dict(user_row))

    # A code review pass caught this reimplementing user_balance_snapshot
    # -- three get_or_create_account() + balance() round trips, the same
    # shape ledger.py's own shared helper already replaced with a single
    # query when it was consolidated out of services/gateway/queries.py
    # (packages/core/ledger.py's own docstring covers why it lives there,
    # not here or gateway/queries.py). Purely a reuse/efficiency fix --
    # same three balance figures, just not reimplemented a third time.
    balances = await ledger.user_balance_snapshot(pool, user_id)

    return {**user_dict, "balances": balances, "ltv": await player_ltv(pool, user_id)}


async def player_ltv(pool: asyncpg.Pool, user_id: int) -> dict[str, Any]:
    """Net cash this player has contributed to the platform over their
    lifetime -- total succeeded deposits minus total succeeded
    withdrawals, the standard "player value" metric a real-money operator
    tracks (spec section 11's Reports screen: "player LTV"). Computed
    directly from payments, not house_revenue, since house_revenue is one
    shared account not itemized per player -- this is the metric that
    actually is.
    """
    row = await pool.fetchrow(
        """
        SELECT
          COALESCE(SUM(amount) FILTER (WHERE direction = 'in'), 0) AS total_deposited,
          COALESCE(SUM(amount) FILTER (WHERE direction = 'out'), 0) AS total_withdrawn
        FROM payments
        WHERE user_id = $1 AND status = 'succeeded'
        """,
        user_id,
    )
    assert row is not None
    total_deposited: Decimal = row["total_deposited"]
    total_withdrawn: Decimal = row["total_withdrawn"]
    return {
        "total_deposited": str(total_deposited),
        "total_withdrawn": str(total_withdrawn),
        "net_ltv": str(total_deposited - total_withdrawn),
    }


async def top_players_by_ltv(pool: asyncpg.Pool, limit: int = 20) -> list[dict[str, Any]]:
    """The Reports-screen leaderboard version of player_ltv() -- ranked,
    not per-user, and computed in one aggregate query rather than N calls.
    """
    rows = await pool.fetch(
        """
        SELECT
          p.user_id,
          u.display_name,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'in'), 0) AS total_deposited,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'out'), 0) AS total_withdrawn,
          COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'in'), 0)
            - COALESCE(SUM(p.amount) FILTER (WHERE p.direction = 'out'), 0) AS net_ltv
        FROM payments p
        JOIN users u ON u.id = p.user_id
        WHERE p.status = 'succeeded'
        GROUP BY p.user_id, u.display_name
        ORDER BY net_ltv DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "user_id": r["user_id"],
            "display_name": r["display_name"],
            "total_deposited": str(r["total_deposited"]),
            "total_withdrawn": str(r["total_withdrawn"]),
            "net_ltv": str(r["net_ltv"]),
        }
        for r in rows
    ]


async def retention_cohorts(pool: asyncpg.Pool, weeks: int = 8) -> list[dict[str, Any]]:
    """Weekly signup-cohort retention (spec section 11's Reports screen:
    "retention cohorts") -- for each week's new signups, what fraction
    played at least one round in each of the following `weeks` weeks.
    "Active" means entered a round that actually started (round_entries
    joined to a round with a real started_at), not just opened the app.

    One set-based SQL query, not a per-user Python loop -- this report
    scans every user and every round entry ever recorded, so it has to
    scale with real data volume, not just look right against a handful of
    test rows.

    Every (cohort, week_offset) pair is returned even at zero activity --
    including offsets for a cohort that signed up recently, where that
    week hasn't actually happened yet. Those are marked `elapsed: false`
    and `retention_rate: null` rather than a bare 0.0: a code review pass
    caught that an un-elapsed week is otherwise indistinguishable from a
    cohort that genuinely churned to zero, in the same report row as
    fully-elapsed older cohorts a reader would reasonably compare it
    against. `active_users` itself is left as a real, honest count either
    way -- someone already active partway through an in-progress week is
    real information; only the *rate* implies a completed comparison.
    """
    rows = await pool.fetch(
        """
        WITH cohort AS (
          SELECT id, date_trunc('week', created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS cohort_week
          FROM users
          WHERE NOT is_simulated
        ),
        cohort_sizes AS (
          SELECT cohort_week, count(*) AS cohort_size FROM cohort GROUP BY cohort_week
        ),
        activity AS (
          SELECT DISTINCT re.user_id,
            date_trunc('week', r.started_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS active_week
          FROM round_entries re
          JOIN rounds r ON r.id = re.round_id
          WHERE r.started_at IS NOT NULL
        ),
        retention AS (
          SELECT
            c.cohort_week,
            ((a.active_week - c.cohort_week) / 7)::int AS week_offset,
            count(DISTINCT c.id) AS active_users
          FROM cohort c
          JOIN activity a ON a.user_id = c.id AND a.active_week >= c.cohort_week
          WHERE a.active_week < c.cohort_week + ($1::int * interval '1 week')
          GROUP BY c.cohort_week, week_offset
        )
        SELECT
          cs.cohort_week,
          cs.cohort_size,
          gs.week_offset,
          COALESCE(r.active_users, 0) AS active_users,
          (cs.cohort_week + ((gs.week_offset + 1) * interval '1 week')) <= now() AS elapsed
        FROM cohort_sizes cs
        CROSS JOIN generate_series(0, $1::int - 1) AS gs(week_offset)
        LEFT JOIN retention r ON r.cohort_week = cs.cohort_week AND r.week_offset = gs.week_offset
        ORDER BY cs.cohort_week, gs.week_offset
        """,
        weeks,
    )

    cohorts: dict[date, dict[str, Any]] = {}
    for row in rows:
        cohort = cohorts.setdefault(
            row["cohort_week"],
            {"cohort_week": row["cohort_week"].isoformat(), "cohort_size": row["cohort_size"], "weeks": []},
        )
        cohort_size = row["cohort_size"]
        elapsed = row["elapsed"]
        cohort["weeks"].append(
            {
                "week_offset": row["week_offset"],
                "active_users": row["active_users"],
                "elapsed": elapsed,
                "retention_rate": (
                    round(row["active_users"] / cohort_size, 4) if elapsed and cohort_size else None
                ),
            }
        )
    return list(cohorts.values())


async def get_user_ledger_history(
    pool: asyncpg.Pool, user_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT t.id AS transaction_id, t.kind, t.memo, t.created_at,
               a.kind AS account_kind, e.amount
        FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN ledger_transactions t ON t.id = e.transaction_id
        WHERE a.user_id = $1
        ORDER BY e.id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def adjust_balance(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    amount: Decimal,
    reason: str,
    ip_address: str | None,
    request_id: str,
) -> int:
    """Credits (amount > 0) or debits (amount < 0) a user's cash balance,
    offset against the house_float account -- a real ledger transaction of
    kind 'adjustment', never a direct UPDATE. `reason` is mandatory (the
    caller enforces non-empty; this function just requires the argument).

    `request_id` is a client-generated token, one per "Apply" click
    (web/admin/js/screens/users.js), that becomes the ledger idempotency
    key. An architecture audit caught that this used to be keyed on
    admin_id/user_id/datetime.now() -- unique by construction on every
    single call, which defeated ledger.post()'s own dedup entirely: a
    double-click or a retried request created two separate real-money
    ledger transactions instead of being recognized as the same one, the
    one money-moving path in this codebase without that protection.
    """
    idempotency_key = f"admin-adjust-{admin_id}-{user_id}-{request_id}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            # A genuine replay (double-click, retried request) -- the
            # ledger side would already correctly dedup via ledger.post()'s
            # own ON CONFLICT DO NOTHING, but this check is what stops
            # adjust_balance() from ALSO writing a second, duplicate
            # audit-log entry and re-incrementing the transactions-created
            # metric for a transaction that was never actually created a
            # second time.
            existing_id = await conn.fetchval(
                "SELECT id FROM ledger_transactions WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing_id is not None:
                return int(existing_id)

            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            house_float = await ledger.get_or_create_account(conn, None, "house_float")
            before_balance = await ledger.balance(conn, cash.id)

            txn = await ledger.post(
                conn,
                "adjustment",
                [
                    ledger.Entry(house_float.id, -amount),
                    ledger.Entry(cash.id, amount),
                ],
                idempotency_key=idempotency_key,
                created_by=f"admin:{admin_id}",
                memo=reason,
            )

            after_balance = await ledger.balance(conn, cash.id)

            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.adjust_balance",
                target_type="user",
                target_id=str(user_id),
                before={"cash_balance": str(before_balance)},
                after={"cash_balance": str(after_balance), "ledger_transaction_id": txn.id},
                reason=reason,
                ip_address=ip_address,
            )
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
    return txn.id


# Self-exclusion (packages.core.responsible_gaming.self_exclude()) is
# deliberately, permanently irreversible for its duration -- "there is
# deliberately no 'lift my own self-exclusion' function anywhere in this
# codebase" per that module's own docstring. This generic admin
# status-change endpoint was exactly such a function in disguise (a real
# bug a code review pass caught: any ops/finance admin holding
# users:suspend could silently reverse a legally-mandated exclusion by
# calling this with status="active"). self_excluded is excluded from both
# ends of the transition: an admin can never set it directly (that would
# also be incomplete -- it wouldn't set self_excluded_until, producing a
# broken half-exclusion), and once a user is self_excluded, this endpoint
# refuses to touch their status at all, in either direction.
_ADMIN_SETTABLE_STATUSES = frozenset({"active", "limited", "banned"})


class InvalidStatusTransition(Exception):
    pass


async def set_user_status(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    status: str,
    reason: str,
    ip_address: str | None,
) -> None:
    if status not in _ADMIN_SETTABLE_STATUSES:
        raise InvalidStatusTransition(f"admins cannot set user status to {status!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR NO KEY UPDATE (the lock the UPDATE below takes anyway):
            # FOR UPDATE also blocked every foreign-key insert for this
            # player -- a Bingo join, a Keno ticket -- for the length of
            # this transaction (test_player_row_lock_order.py).
            before = await conn.fetchval(
                "SELECT status FROM users WHERE id = $1 FOR NO KEY UPDATE", user_id
            )
            if before == "self_excluded":
                raise InvalidStatusTransition(
                    "self-exclusion cannot be changed by an admin -- it is "
                    "permanent for its duration by design"
                )
            await conn.execute("UPDATE users SET status = $1 WHERE id = $2", status, user_id)
            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.set_status",
                target_type="user",
                target_id=str(user_id),
                before={"status": before},
                after={"status": status},
                reason=reason,
                ip_address=ip_address,
            )


_VALID_KYC_LEVELS = frozenset({0, 1, 2})


class InvalidKycLevel(Exception):
    pass


async def set_kyc_level(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    user_id: int,
    kyc_level: int,
    reason: str,
    ip_address: str | None,
) -> None:
    """The manual half of KYC verification: an admin who has reviewed a
    user's identity documents (through whatever out-of-band channel this
    platform actually collects them through -- that verification method
    itself is a real, separate, not-yet-made product decision, tracked in
    DECISIONS.md, not invented here) records the outcome. Before this,
    `users.kyc_level` had a real consumer (withdrawals.py's own threshold
    gate) but no writer anywhere in the codebase -- a real, live gap a
    code review pass caught: any user who genuinely needed KYC to clear a
    large withdrawal had no path through the gate at all, not even a slow
    manual one. Promotions and demotions both go through this same
    function and the same audit trail -- a level can be revoked (fraud
    discovered, documents later found invalid) exactly the same
    accountable way it was granted.
    """
    if kyc_level not in _VALID_KYC_LEVELS:
        raise InvalidKycLevel(f"kyc_level must be one of {sorted(_VALID_KYC_LEVELS)}, got {kyc_level!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR NO KEY UPDATE, for the same reason as set_user_status().
            before = await conn.fetchval(
                "SELECT kyc_level FROM users WHERE id = $1 FOR NO KEY UPDATE", user_id
            )
            if before is None:
                raise InvalidKycLevel(f"no such user: {user_id}")
            await conn.execute("UPDATE users SET kyc_level = $1 WHERE id = $2", kyc_level, user_id)
            await audit.record(
                conn,
                admin_id=admin_id,
                action="users.set_kyc_level",
                target_type="user",
                target_id=str(user_id),
                before={"kyc_level": before},
                after={"kyc_level": kyc_level},
                reason=reason,
                ip_address=ip_address,
            )


async def list_rounds(pool: asyncpg.Pool, room_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    # rounds.player_count is incremented per *card* taken (round_engine.py's
    # join()/drop_card()), not per distinct user -- correct for the pot
    # math it also drives, but a player holding several cards would
    # otherwise inflate this admin-facing "Players" column. A
    # count(DISTINCT user_id) over round_entries, aliased back onto the
    # same player_count key, mirrors the identical fix already made for
    # the gateway's own player-facing list_rooms()/build_state_sync().
    select = (
        "SELECT id, room_id, seq, status, stake, pot, derash, "
        "(SELECT count(DISTINCT user_id) FROM round_entries WHERE round_id = rounds.id) AS player_count, "
        "started_at, ended_at FROM rounds"
    )
    if room_id is not None:
        rows = await pool.fetch(
            f"{select} WHERE room_id = $1 ORDER BY id DESC LIMIT $2",
            room_id,
            limit,
        )
    else:
        rows = await pool.fetch(f"{select} ORDER BY id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def get_round_detail(pool: asyncpg.Pool, round_id: int) -> dict[str, Any] | None:
    round_row = await pool.fetchrow("SELECT * FROM rounds WHERE id = $1", round_id)
    if round_row is None:
        return None
    entries = await pool.fetch(
        "SELECT card_no, user_id, auto_mark FROM round_entries WHERE round_id = $1", round_id
    )
    winners = await pool.fetch(
        "SELECT user_id, card_no, pattern, won_on_call, amount FROM round_winners "
        "WHERE round_id = $1",
        round_id,
    )
    return {
        "round": dict(round_row),
        "entries": [dict(e) for e in entries],
        "winners": [dict(w) for w in winners],
    }


async def get_round_fairness(pool: asyncpg.Pool, round_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT server_seed, server_seed_hash, client_seed, draw_order, status "
        "FROM rounds WHERE id = $1",
        round_id,
    )
    if row is None:
        return None
    if row["server_seed"] is None or row["status"] not in ("done", "voided"):
        return {
            "revealed": False,
            "server_seed_hash": row["server_seed_hash"],
            "reason": "server_seed is only revealed after a round finishes",
        }

    server_seed: bytes = row["server_seed"]
    client_seed: str = row["client_seed"] or ""
    draw_order = list(row["draw_order"] or [])
    verified = bingo.verify_draw(server_seed, client_seed, draw_order)

    return {
        "revealed": True,
        "server_seed": server_seed.hex(),
        "server_seed_hash": row["server_seed_hash"],
        "client_seed": client_seed,
        "draw_order": draw_order,
        "verified": verified,
    }


async def void_round_admin(
    pool: asyncpg.Pool, *, admin_id: int, round_id: int, reason: str, ip_address: str | None
) -> bool:
    # The refund and its audit-log entry commit or roll back together, in
    # one transaction -- a real bug a code review pass caught: this used
    # to call refund_round(pool, ...) (its own independent transaction,
    # already committed) and then write the audit entry afterward on a
    # separate connection, so a crash in between left real money refunded
    # with no audit trail for it at all.
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
            refunded_count = await refund_round_in_transaction(
                conn, round_id, reason=f"admin_void: {reason}"
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="rounds.void",
                target_type="round",
                target_id=str(round_id),
                before={"status": before["status"] if before else None},
                after={"status": "voided" if refunded_count else "unchanged (already terminal)"},
                reason=reason,
                ip_address=ip_address,
            )
        # Only reachable once this function's own transaction above has
        # actually committed -- safe to record here even though
        # refund_round_in_transaction() itself can't (see its own
        # comment): this call site owns a real, non-nested transaction.
        if refunded_count:
            metrics.ledger_transactions_total.labels(kind="refund").inc(refunded_count)
    return bool(refunded_count)


# The literal confirmation an admin must type/send back before this
# actually executes -- same shape as packages.core.responsible_gaming
# .SELF_EXCLUDE_CONFIRMATION_TOKEN, which this codebase already uses for
# exactly this reasoning: a real financial/game-integrity action must not
# be triggerable by a single accidental click or an automated retry with
# no human confirmation in the loop.
STOP_ROOM_CONFIRMATION_TOKEN = "STOP"


async def get_room_stop_preview_admin(pool: asyncpg.Pool, room_id: int) -> dict[str, Any] | None:
    """Everything the admin console's confirmation dialog needs to show
    before an operator commits to stopping a room -- current round,
    player count, and real staked amount, not a guess. Returns None if
    the room doesn't exist.
    """
    room = await pool.fetchrow("SELECT id, code, is_active FROM rooms WHERE id = $1", room_id)
    if room is None:
        return None

    round_row = await pool.fetchrow(
        "SELECT id, status, pot, seq FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
        room_id,
    )
    has_active_round = round_row is not None and round_row["status"] not in TERMINAL_STATUSES
    player_count = 0
    if has_active_round:
        assert round_row is not None
        player_count = await pool.fetchval(
            "SELECT count(*) FROM round_entries WHERE round_id = $1", round_row["id"]
        )

    return {
        "room_id": room["id"],
        "room_code": room["code"],
        "room_is_active": room["is_active"],
        "current_round_id": round_row["id"] if round_row else None,
        "current_round_status": round_row["status"] if round_row else None,
        "has_stoppable_round": has_active_round,
        "players": player_count,
        "staked_amount": str(round_row["pot"]) if has_active_round and round_row else "0.00",
    }


async def stop_room_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    room_id: int,
    reason: str,
    confirmation: str,
    ip_address: str | None,
) -> dict[str, Any]:
    """The emergency single-room stop. Reuses the exact same financial
    primitive void_round_admin() above already relies on
    (refund_round_in_transaction()) rather than inventing a second
    refund code path -- the only genuinely new things here are: finding
    the room's *current* round rather than requiring the caller to
    already know its id, bundling rooms.is_active=false into the same
    transaction (so the room can't be re-claimed and start a fresh round
    right after), and a stricter, explicit confirmation token on top of
    the reason (this stops an entire room, not one already-decided
    round -- a real product-owner reasoning captured in DECISIONS.md
    when this action was scoped).

    Safe against every race this needs to be safe against by
    construction, not by hoping the timing works out: refund_round_in_
    transaction() already does its own `SELECT ... FOR UPDATE` before
    touching the round, and RoundEngine._settle_with_winners() (services/
    engine/round_engine.py) now does the identical FOR UPDATE + terminal-
    status check before ever crediting a winner -- whichever transaction
    (this one, or the engine's own settlement) reaches the row first
    commits the round's real terminal outcome; the other one sees that
    and safely no-ops. A second call for an already-stopped room is
    exactly this same no-op path, not a special case.
    """
    if confirmation.strip().upper() != STOP_ROOM_CONFIRMATION_TOKEN:
        raise ValueError(
            f"confirmation must be exactly {STOP_ROOM_CONFIRMATION_TOKEN!r} to stop a room"
        )
    if not reason.strip():
        raise ValueError("reason is required")

    async with pool.acquire() as conn:
        async with conn.transaction():
            room = await conn.fetchrow(
                "SELECT id, code, is_active FROM rooms WHERE id = $1 FOR UPDATE", room_id
            )
            if room is None:
                raise ValueError(f"no such room: {room_id}")

            round_row = await conn.fetchrow(
                "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
                room_id,
            )
            refunded_user_ids: list[int] = []
            refunded_count = 0
            stopped_round_id: int | None = None
            if round_row is not None:
                stopped_round_id = round_row["id"]
                # Read who's actually in it *before* refunding -- the
                # refund doesn't delete round_entries (nothing in this
                # codebase ever deletes a player's entry, per the "no
                # deleting player entries" rule this action must respect
                # too), but reading it up front here keeps this query
                # right next to the action it's informing, not scattered.
                refunded_user_ids = [
                    r["user_id"]
                    for r in await conn.fetch(
                        "SELECT DISTINCT user_id FROM round_entries WHERE round_id = $1",
                        round_row["id"],
                    )
                ]
                refunded_count = await refund_round_in_transaction(
                    conn, round_row["id"], reason=f"admin_emergency_stop: {reason}"
                )

            await conn.execute("UPDATE rooms SET is_active = false WHERE id = $1", room_id)

            await audit.record(
                conn,
                admin_id=admin_id,
                action="rooms.emergency_stop",
                target_type="room",
                target_id=str(room_id),
                before={
                    "room_is_active": room["is_active"],
                    "round_id": stopped_round_id,
                    "round_status": round_row["status"] if round_row else None,
                },
                after={
                    "room_is_active": False,
                    "round_id": stopped_round_id,
                    "refunded_entrants": refunded_count,
                },
                reason=reason,
                ip_address=ip_address,
            )

        # Only reachable once the transaction above has actually
        # committed -- same reasoning void_round_admin() already
        # documents for its own identical metrics-after-commit placement.
        if refunded_count:
            metrics.ledger_transactions_total.labels(kind="refund").inc(refunded_count)

    # Player-facing notification, deliberately sent only after the
    # transaction above has committed for real -- "never promise a
    # refund before the ledger confirms it" applies here exactly as much
    # as it does to any other financial message this codebase sends.
    if stopped_round_id is not None and refunded_user_ids:
        room_stake = await pool.fetchval("SELECT stake FROM rooms WHERE id = $1", room_id)
        await asyncio.gather(
            *(
                ledger.publish_balance_update(pool, redis, user_id)
                for user_id in refunded_user_ids
            )
        )
        await asyncio.gather(
            *(
                notify_user(
                    pool,
                    redis,
                    user_id=user_id,
                    key="notify.room_emergency_stopped",
                    amount=str(room_stake),
                    reference=f"room-stop-{stopped_round_id}",
                )
                for user_id in refunded_user_ids
            )
        )
        await redis.publish(
            f"room:{room_id}",
            json.dumps(
                {
                    "t": "round_voided",
                    "round_id": stopped_round_id,
                    "reason": "admin_emergency_stop",
                }
            ),
        )

    return {
        "room_id": room_id,
        "stopped_round_id": stopped_round_id,
        "refunded_entrants": refunded_count,
        "room_deactivated": True,
    }


ROOMS_ADMIN_LIST_LIMIT = 200


async def list_rooms(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Phase 3 (Section 34: "do not load every dashboard dataset... use
    lazy loading, pagination") -- a real, growing gap this closes: every
    real stake tier a room represents stays active indefinitely (19 real
    rows in this environment today), while a deactivated room is
    typically retired for good, not routinely revisited. An unbounded
    `ORDER BY stake` here used to return literally every room row this
    database has ever held (6,700+ in this shared, long-lived dev
    environment, from years of this session's own test runs -- confirmed
    directly, not assumed), which made the admin Rooms screen's own
    render genuinely slow enough to fail a real-browser test's own
    timeout, not just look untidy. Every currently-active room is always
    included (there is no real scenario where an operator shouldn't see
    every room actually taking player traffic); inactive rooms are
    included newest-first up to the cap, so a room just deactivated
    (the common "I need to re-check/reactivate this one" case) still
    shows up, while years of long-abandoned rows don't have to render.
    """
    rows = await pool.fetch(
        f"""
        SELECT id, code, stake, house_cut_bps, min_players, max_players,
               max_cards_per_player, lobby_seconds, call_interval_ms, result_seconds,
               win_patterns, min_winning_lines, is_active
        FROM rooms
        ORDER BY is_active DESC, id DESC
        LIMIT {ROOMS_ADMIN_LIST_LIMIT}
        """
    )
    out = []
    for r in rows:
        d = dict(r)
        d["win_patterns"] = _parse_jsonb(d["win_patterns"])
        out.append(d)
    return out


MIN_WINNING_LINES_RANGE = range(1, 5)  # 1-4 inclusive; matches the DB CHECK constraint exactly


def _validate_min_winning_lines(value: int) -> None:
    if value not in MIN_WINNING_LINES_RANGE:
        raise ValueError(
            f"min_winning_lines must be between {MIN_WINNING_LINES_RANGE.start} and "
            f"{MIN_WINNING_LINES_RANGE.stop - 1}, got {value}"
        )


async def create_room_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    code: str,
    stake: Decimal,
    house_cut_bps: int,
    min_players: int,
    max_players: int,
    max_cards_per_player: int = 1,
    lobby_seconds: int,
    call_interval_ms: int,
    result_seconds: int,
    win_patterns: list[str],
    min_winning_lines: int = 2,
    ip_address: str | None,
) -> int:
    # Validated here, before ever reaching the database, so an admin gets
    # a clean 422 (ValueError -> HTTPException, the same mapping every
    # other admin-console validation error already uses) instead of a raw
    # constraint-violation error -- the DB CHECK stays too, as the real,
    # unconditional backstop for any caller that skips this function.
    _validate_min_winning_lines(min_winning_lines)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO rooms
                    (code, stake, house_cut_bps, min_players, max_players,
                     max_cards_per_player, lobby_seconds, call_interval_ms,
                     result_seconds, win_patterns, min_winning_lines)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
                """,
                code,
                stake,
                house_cut_bps,
                min_players,
                max_players,
                max_cards_per_player,
                lobby_seconds,
                call_interval_ms,
                result_seconds,
                json.dumps(win_patterns),
                min_winning_lines,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="rooms.create",
                target_type="room",
                target_id=str(row["id"]),
                after={"code": code, "stake": str(stake), "house_cut_bps": house_cut_bps},
                ip_address=ip_address,
            )
    return int(row["id"])


_UPDATABLE_ROOM_FIELDS = {
    "stake",
    "house_cut_bps",
    "min_players",
    "max_players",
    "max_cards_per_player",
    "lobby_seconds",
    "call_interval_ms",
    "result_seconds",
    "win_patterns",
    "min_winning_lines",
    "is_active",
}


def _room_audit_value(row: asyncpg.Record, key: str) -> Any:
    """A code review pass caught that update_room_admin()'s audit
    before/after values ran every changed field through a blanket
    str(...), including win_patterns -- asyncpg returns a jsonb column as
    a raw JSON string with no codec registered (see _parse_jsonb()
    above), so str()-ing it was a no-op that left a JSON string sitting
    as a dict value, which audit.record()'s own json.dumps(before) then
    double-encodes into an escaped string in the stored audit row --
    readable as ``"win_patterns": "[\\"row_0\\"]"`` instead of a clean
    nested array. Not a financial bug (the actual room update is
    unaffected either way), just a real audit-readability gap for
    anyone reviewing this specific field's change history.
    """
    value = row[key]
    if key == "win_patterns":
        return _parse_jsonb(value)
    return str(value)


async def update_room_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    room_id: int,
    changes: dict[str, Any],
    reason: str | None,
    ip_address: str | None,
) -> bool:
    """Edits room config -- spec section 26: this can never retroactively
    alter a round already in progress (each round snapshots its own
    stake/house_cut_bps/etc. at creation time into rounds.stake, not
    rooms.stake). More precisely than "affects rounds created after the
    change" might suggest: RoundEngine loads its own RoomConfig exactly
    once, when it first claims a room (services/engine/round_engine.py::
    RoundEngine.__init__ / load_room_config()), and keeps using that same
    snapshot for every round it runs afterward until the engine itself is
    re-claimed (a restart, or the room's lock changing hands) -- an edit
    here does not retroactively reach a round already running, but it
    also doesn't reach the *next* round in an already-live engine's own
    continuous lifecycle either. It takes effect the next time this room
    is claimed. This is the same lifecycle every other room field
    (win_patterns included) already has; min_winning_lines follows it
    identically rather than inventing a separate hot-reload path.
    """
    unknown = set(changes) - _UPDATABLE_ROOM_FIELDS
    if unknown:
        raise ValueError(f"not an editable room field: {unknown}")
    if not changes:
        return False
    if "min_winning_lines" in changes:
        _validate_min_winning_lines(changes["min_winning_lines"])
    if "stake" in changes:
        # The edit form sends this as a plain string (FormData, not a
        # typed number input) -- caught here, before it ever reaches the
        # UPDATE below, for the same reason create_room_admin's own
        # caller validates stake up front: asyncpg raises a raw
        # DataError (not a ValueError) for a non-numeric string bound to
        # a numeric column, which nothing here or in the endpoint's own
        # except ValueError catches, so a malformed edit (an empty
        # field, a stray space, a comma instead of a decimal point)
        # would otherwise surface as an opaque 500 instead of a clean,
        # actionable validation error.
        try:
            changes["stake"] = Decimal(str(changes["stake"]))
        except InvalidOperation as exc:
            raise ValueError("stake must be a decimal number") from exc

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)
            if before is None:
                return False

            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                set_clauses.append(f"{field} = ${i}")
                values.append(json.dumps(value) if field == "win_patterns" else value)
            values.append(room_id)

            await conn.execute(
                f"UPDATE rooms SET {', '.join(set_clauses)} WHERE id = ${len(values)}",
                *values,
            )
            after = await conn.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)

            await audit.record(
                conn,
                admin_id=admin_id,
                action="rooms.update",
                target_type="room",
                target_id=str(room_id),
                before={k: _room_audit_value(before, k) for k in changes},
                after={k: _room_audit_value(after, k) for k in changes} if after else None,
                reason=reason,
                ip_address=ip_address,
            )
    return True


# --- Manual payment configuration (payments:configure only -- narrower
# than payments:approve, since these two functions change behavior
# platform-wide rather than one payment at a time: which destination
# manual deposits get paid into, and which rails are live at all. The
# single highest-leverage lever a compromised/rogue admin account could
# pull, so it's scoped tighter than approving one request.) -----------


async def list_manual_payment_destinations(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, method_kind, account_ref, account_name, instructions, is_active, "
        "effective_from, effective_until, created_at, updated_at "
        "FROM manual_payment_destinations ORDER BY method_kind, id"
    )
    return [dict(r) for r in rows]


async def create_manual_payment_destination_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    method_kind: str,
    account_ref: str,
    account_name: str,
    instructions: str | None,
    ip_address: str | None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> int:
    # Same explicit-UTC rule as _coerce_destination_changes above: a
    # caller with no offset (FastAPI/Pydantic parses a plain "2026-09-05
    # T09:00:00" body value into a *naive* datetime, exactly like
    # datetime.fromisoformat() does) means UTC, deterministically, not
    # whatever OS timezone this process happens to be running under.
    if effective_from is not None and effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=timezone.utc)
    if effective_until is not None and effective_until.tzinfo is None:
        effective_until = effective_until.replace(tzinfo=timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO manual_payment_destinations
                    (method_kind, account_ref, account_name, instructions, created_by_admin_id,
                     effective_from, effective_until)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                method_kind,
                account_ref,
                account_name,
                instructions,
                admin_id,
                effective_from,
                effective_until,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_payment_destinations.create",
                target_type="manual_payment_destination",
                target_id=str(row["id"]),
                after={"method_kind": method_kind, "account_ref": account_ref, "account_name": account_name},
                ip_address=ip_address,
            )
    return int(row["id"])


# effective_from/effective_until (CTO directive section 92: an old
# recipient must stay valid for historical records, not "accidentally
# become invalid") -- added here alongside method_kind/account_ref/
# account_name/instructions/is_active, which were already dynamically
# editable through this exact same generic mechanism (the PATCH route and
# update_manual_payment_destination_admin() below needed zero other
# changes for this).
_UPDATABLE_DESTINATION_FIELDS = {
    "method_kind", "account_ref", "account_name", "instructions", "is_active",
    "effective_from", "effective_until",
}

# effective_from/effective_until arrive here as whatever a generic JSON
# body decoded them to -- a plain str, never a real datetime -- since
# `changes: dict[str, Any]` has no per-field schema to coerce them the
# way a dedicated Pydantic field (like create_manual_payment_destination_
# admin's own effective_from: datetime | None parameter) already does.
# asyncpg binds a timestamptz column strictly: it rejects a str outright
# rather than parsing it, so every edit to either date field 500'd --
# a real bug, caught live (web/admin's own "Edit destination" form
# submits exactly this shape), not a hypothetical.
_DESTINATION_DATETIME_FIELDS = {"effective_from", "effective_until"}


def _coerce_destination_changes(changes: dict[str, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for field, value in changes.items():
        if field in _DESTINATION_DATETIME_FIELDS and isinstance(value, str):
            if not value.strip():
                coerced[field] = None
                continue
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{field} is not a valid ISO datetime: {value!r}") from exc
            # A caller with no explicit offset (the admin UI's own plain
            # datetime-local input, no timezone selector) means UTC,
            # deterministically -- never whatever OS timezone the Python
            # process handling the request happens to be running under.
            # asyncpg encodes a *naive* datetime using the local system
            # timezone of the process itself, not UTC and not Postgres's
            # own session `timezone` GUC -- confirmed directly: the exact
            # same naive value round-tripped to a different stored instant
            # in this project's own dev sandbox (a non-UTC system
            # timezone) versus production (a UTC container), which is
            # exactly the silent, environment-dependent drift this
            # explicit attachment removes.
            coerced[field] = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        else:
            coerced[field] = value
    return coerced


async def update_manual_payment_destination_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    destination_id: int,
    changes: dict[str, Any],
    reason: str | None,
    ip_address: str | None,
) -> bool:
    unknown = set(changes) - _UPDATABLE_DESTINATION_FIELDS
    if unknown:
        raise ValueError(f"not an editable destination field: {unknown}")
    if not changes:
        return False
    changes = _coerce_destination_changes(changes)

    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT * FROM manual_payment_destinations WHERE id = $1", destination_id
            )
            if before is None:
                return False

            set_clauses = []
            values: list[Any] = []
            for i, (field, value) in enumerate(changes.items(), start=1):
                set_clauses.append(f"{field} = ${i}")
                values.append(value)
            values.append(destination_id)

            await conn.execute(
                f"UPDATE manual_payment_destinations SET {', '.join(set_clauses)}, updated_at = now() "
                f"WHERE id = ${len(values)}",
                *values,
            )
            after = await conn.fetchrow(
                "SELECT * FROM manual_payment_destinations WHERE id = $1", destination_id
            )

            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_payment_destinations.update",
                target_type="manual_payment_destination",
                target_id=str(destination_id),
                before={k: str(before[k]) for k in changes},
                after={k: str(after[k]) for k in changes} if after else None,
                reason=reason,
                ip_address=ip_address,
            )
    return True


async def get_payment_provider_availability(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT provider, direction, enabled, updated_at FROM payment_provider_availability "
        "ORDER BY provider, direction"
    )
    return [dict(r) for r in rows]


async def set_payment_provider_availability_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    provider: str,
    direction: str,
    enabled: bool,
    reason: str | None,
    ip_address: str | None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT enabled FROM payment_provider_availability WHERE provider = $1 AND direction = $2 "
                "FOR UPDATE",
                provider,
                direction,
            )
            if before is None:
                return False

            await conn.execute(
                "UPDATE payment_provider_availability SET enabled = $3, updated_by_admin_id = $4, "
                "updated_at = now() WHERE provider = $1 AND direction = $2",
                provider,
                direction,
                enabled,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="payment_provider_availability.set",
                target_type="payment_provider_availability",
                target_id=f"{provider}:{direction}",
                before={"enabled": before["enabled"]},
                after={"enabled": enabled},
                reason=reason,
                ip_address=ip_address,
            )
    return True


# --- Telebirr SMS-evidence admin review (CTO directive sections 91/97/99) --

# Explicit state-transition policy (section 99) -- an admin resolution can
# only move a row along one of these edges; anything else raises. redeemed
# and expired are terminal here on purpose: "REDEEMED -> AVAILABLE must
# NOT be allowed as a normal release operation" (section 98's own
# example) -- if a redeemed evidence row is ever genuinely wrong, that is
# a ledger correction handled through the existing controlled remediation
# path (section 136), never a plain status flip back to available.
_EVIDENCE_TRANSITIONS: dict[str, frozenset[str]] = {
    "available": frozenset({"blocked", "disputed"}),
    "blocked": frozenset({"disputed", "available"}),
    "disputed": frozenset({"available"}),
    # A row ships 'rejected' whenever ingestion ran before a recipient was
    # configured (the common case at launch, per the product's own
    # decision to ship the recipient list empty) -- once a real recipient
    # is added, an admin who has independently verified the evidence is
    # legitimate resolves it forward the same way a disputed row is
    # resolved.
    "rejected": frozenset({"available"}),
}


async def list_payment_evidence(
    pool: asyncpg.Pool, *, status: str | None, limit: int, cursor: int | None
) -> tuple[list[dict[str, Any]], int | None]:
    """Paginated, and deliberately never includes raw_sms (section 129:
    "raw evidence must be loaded deliberately, not automatically") --
    get_payment_evidence_raw_sms() below is the one, separately-permissioned,
    separately-audited way to see it. cursor is the smallest id already
    seen; returns (rows, next_cursor) where next_cursor is None once
    there's nothing more to page through.
    """
    limit = max(1, min(limit, 200))
    conditions = []
    params: list[Any] = []
    if status is not None:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    if cursor is not None:
        params.append(cursor)
        conditions.append(f"id < ${len(params)}")
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit + 1)

    rows = await pool.fetch(
        f"""
        SELECT id, source, external_reference, amount, fee, vat, direction,
               payer_name, recipient_name, recipient_phone,
               transaction_at, received_at, status, reject_reason, redeemed_by_user_id,
               redeemed_at, payment_id, created_at
        FROM payment_evidence
        {where_sql}
        ORDER BY id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1]["id"] if has_more else None
    return [dict(r) for r in page], next_cursor


async def get_payment_evidence_raw_sms(
    pool: asyncpg.Pool, *, admin_id: int, evidence_id: int, ip_address: str | None
) -> str | None:
    """The one, deliberate way to see raw SMS text (section 97: "every
    raw-SMS view should be auditable") -- unlike list_payment_evidence
    above, this always writes an audit row, whether or not the evidence
    row actually exists (a probing admin trying ids that don't exist is
    exactly the kind of thing this audit trail exists to catch).
    """
    row = await pool.fetchrow("SELECT raw_sms FROM payment_evidence WHERE id = $1", evidence_id)
    await audit.record(
        pool,
        admin_id=admin_id,
        action="payment_evidence.view_raw_sms",
        target_type="payment_evidence",
        target_id=str(evidence_id),
        ip_address=ip_address,
    )
    return row["raw_sms"] if row is not None else None


class InvalidEvidenceTransition(Exception):
    pass


async def resolve_payment_evidence_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    evidence_id: int,
    to_status: str,
    reason: str,
    ip_address: str | None,
) -> bool:
    """Returns False if the evidence row doesn't exist. Raises
    InvalidEvidenceTransition for any transition not explicitly listed in
    _EVIDENCE_TRANSITIONS above (section 99: "invalid transitions must be
    rejected") -- callers must always pass a non-empty reason, the same
    discipline _require_reason() already enforces at the route layer for
    every other admin approve/reject action.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM payment_evidence WHERE id = $1 FOR UPDATE", evidence_id
            )
            if row is None:
                return False
            current_status = row["status"]
            allowed = _EVIDENCE_TRANSITIONS.get(current_status, frozenset())
            if to_status not in allowed:
                raise InvalidEvidenceTransition(
                    f"payment_evidence {evidence_id} cannot move from {current_status!r} to {to_status!r}"
                )

            await conn.execute(
                "UPDATE payment_evidence SET status = $2, reject_reason = $3, updated_at = now() "
                "WHERE id = $1",
                evidence_id,
                to_status,
                reason if to_status != "available" else None,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="payment_evidence.resolve",
                target_type="payment_evidence",
                target_id=str(evidence_id),
                before={"status": current_status},
                after={"status": to_status},
                reason=reason,
                ip_address=ip_address,
            )
    return True


# --- Telegram payment-agent allowlist (services/bot/handlers.py's
# on_agent_sms filter reads this table directly; these are the admin-side
# CRUD functions for it) --------------------------------------------------


async def list_payment_agents(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    # Real activity, not just the allowlist: services/bot/handlers.py's
    # on_agent_sms stamps source_ref with str(message.from_user.id) for
    # every SMS an agent forwards (source='telegram_agent'), which is
    # exactly payment_agents.telegram_user_id -- joined here (aggregated
    # first, in a subquery, rather than a raw join against the full
    # evidence table) so "is this agent actually doing anything" is
    # answerable without a separate query per agent.
    rows = await pool.fetch(
        """
        SELECT pa.id, pa.telegram_user_id, pa.display_name, pa.is_active, pa.created_at,
               COALESCE(stats.submission_count, 0) AS submission_count,
               stats.last_submission_at
        FROM payment_agents pa
        LEFT JOIN (
            SELECT source_ref::bigint AS telegram_user_id,
                   count(*) AS submission_count,
                   max(received_at) AS last_submission_at
            FROM payment_evidence
            WHERE source = 'telegram_agent'
            GROUP BY source_ref::bigint
        ) stats ON stats.telegram_user_id = pa.telegram_user_id
        ORDER BY pa.id
        """
    )
    return [dict(r) for r in rows]


async def create_payment_agent_admin(
    pool: asyncpg.Pool, *, admin_id: int, telegram_user_id: int, display_name: str | None, ip_address: str | None
) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO payment_agents (telegram_user_id, display_name, created_by_admin_id) "
                "VALUES ($1, $2, $3) RETURNING id",
                telegram_user_id,
                display_name,
                admin_id,
            )
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="payment_agents.create",
                target_type="payment_agent",
                target_id=str(row["id"]),
                after={"telegram_user_id": telegram_user_id, "display_name": display_name},
                ip_address=ip_address,
            )
    return int(row["id"])


async def set_payment_agent_active_admin(
    pool: asyncpg.Pool, *, admin_id: int, agent_id: int, is_active: bool, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT is_active FROM payment_agents WHERE id = $1 FOR UPDATE", agent_id
            )
            if before is None:
                return False
            await conn.execute(
                "UPDATE payment_agents SET is_active = $2 WHERE id = $1", agent_id, is_active
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="payment_agents.set_active",
                target_type="payment_agent",
                target_id=str(agent_id),
                before={"is_active": before["is_active"]},
                after={"is_active": is_active},
                ip_address=ip_address,
            )
    return True


# --- Telebirr ingestion devices (per-device credentials for the automated
# Android/MacroDroid path, migrations/versions/e3a7c9f01b2d) -- gated at
# the same payments:view/payments:configure levels as manual payment
# destinations and payment agents just above: viewing a device's health
# is a normal payments:view concern, but minting or revoking a credential
# that can inject financial evidence into the pipeline is a
# payments:configure (superadmin-only) action, same reasoning rbac.py's
# own comment already gives for that permission. -----------------------


class DeviceIdAlreadyRegistered(ValueError):
    pass


# How long a registered, still-active device can go without a successful
# ingestion before the console calls it "degraded" rather than "healthy"
# -- a plain documented constant, the same shape this codebase's other
# "how long before we consider something possibly dead" thresholds
# already take (services/payments/payout_worker.py's CLAIM_STALE_AFTER_MS).
_DEVICE_DEGRADED_AFTER_HOURS = 6


async def list_ingestion_devices(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT id, device_id, device_name, status, created_at, last_seen_at, last_success_at,
               last_error_at, last_error_reason, success_count, duplicate_count, failure_count,
               auth_failure_count,
               CASE
                 WHEN status = 'revoked' THEN 'revoked'
                 WHEN last_success_at IS NULL THEN 'awaiting_first_ingestion'
                 WHEN last_success_at < now() - ($1 * interval '1 hour') THEN 'degraded'
                 ELSE 'healthy'
               END AS health
        FROM ingestion_devices
        ORDER BY id
        """,
        _DEVICE_DEGRADED_AFTER_HOURS,
    )
    return [dict(r) for r in rows]


async def create_ingestion_device_admin(
    pool: asyncpg.Pool, *, admin_id: int, device_id: str, device_name: str, ip_address: str | None
) -> dict[str, Any]:
    """Returns {id, device_id, token} -- the plaintext token is visible
    exactly this once (packages/core/device_auth.py's own docstring);
    only its SHA-256 hash is ever persisted. The admin must copy it into
    the phone's MacroDroid configuration immediately -- a lost token has
    no recovery path, only rotate_ingestion_device_token_admin below.
    """
    token = generate_device_token()
    token_hash = hash_device_token(token)
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
                    "INSERT INTO ingestion_devices (device_id, device_name, token_hash, created_by_admin_id) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    device_id,
                    device_name,
                    token_hash,
                    admin_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise DeviceIdAlreadyRegistered(f"device_id already registered: {device_id!r}") from exc
            assert row is not None
            await audit.record(
                conn,
                admin_id=admin_id,
                action="ingestion_devices.create",
                target_type="ingestion_device",
                target_id=str(row["id"]),
                after={"device_id": device_id, "device_name": device_name},
                ip_address=ip_address,
            )
    return {"id": row["id"], "device_id": device_id, "token": token}


async def set_ingestion_device_status_admin(
    pool: asyncpg.Pool, *, admin_id: int, device_pk: int, status: str, ip_address: str | None
) -> bool:
    if status not in ("active", "revoked"):
        raise ValueError(f"unknown device status: {status!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT status FROM ingestion_devices WHERE id = $1 FOR UPDATE", device_pk
            )
            if before is None:
                return False
            if status == "revoked":
                await conn.execute(
                    "UPDATE ingestion_devices SET status = 'revoked', revoked_by_admin_id = $2, "
                    "revoked_at = now() WHERE id = $1",
                    device_pk,
                    admin_id,
                )
            else:
                await conn.execute(
                    "UPDATE ingestion_devices SET status = 'active', revoked_by_admin_id = NULL, "
                    "revoked_at = NULL WHERE id = $1",
                    device_pk,
                )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="ingestion_devices.set_status",
                target_type="ingestion_device",
                target_id=str(device_pk),
                before={"status": before["status"]},
                after={"status": status},
                ip_address=ip_address,
            )
    return True


async def rotate_ingestion_device_token_admin(
    pool: asyncpg.Pool, *, admin_id: int, device_pk: int, ip_address: str | None
) -> dict[str, Any] | None:
    """Returns {id, device_id, token} (the new plaintext token, shown once
    -- same guarantee as create_ingestion_device_admin) or None if the
    device doesn't exist. The OLD token stops working the instant this
    commits: token_hash is overwritten, not appended to, so there is
    never a window where both the old and new token are simultaneously
    valid. Use when a device's token may have leaked, without needing to
    also re-register the device under a new device_id (which would lose
    its whole health-counter history).
    """
    token = generate_device_token()
    token_hash = hash_device_token(token)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT device_id FROM ingestion_devices WHERE id = $1 FOR UPDATE", device_pk
            )
            if row is None:
                return None
            await conn.execute(
                "UPDATE ingestion_devices SET token_hash = $2 WHERE id = $1", device_pk, token_hash
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="ingestion_devices.rotate_token",
                target_type="ingestion_device",
                target_id=str(device_pk),
                ip_address=ip_address,
            )
    return {"id": device_pk, "device_id": row["device_id"], "token": token}


# --- admin account management (services/admin/auth.py's own docstring
# used to say "provisioned out-of-band by a trusted operator" -- there
# was never a console route for it, only direct DB/script access. These
# are the console-facing wrappers around auth.create_admin_user() and
# admin_users.is_active, gated to admin_users:manage (superadmin-only,
# see rbac.py's own comment on why) -----------------------------------

_MIN_ADMIN_PASSWORD_LENGTH = 12


async def list_admin_users(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, username, role, is_active, created_at, last_login_at "
        "FROM admin_users ORDER BY id"
    )
    return [dict(r) for r in rows]


async def create_admin_user_admin(
    pool: asyncpg.Pool, *, admin_id: int, username: str, password: str, role: str, ip_address: str | None
) -> dict[str, Any]:
    """Returns {id, totp_secret, totp_provisioning_uri} -- the one and
    only time the secret is ever visible, same guarantee auth.py's own
    docstring already makes for the script-based path this replaces.
    """
    if role not in rbac.KNOWN_ADMIN_ROLES:
        raise ValueError(f"unknown role: {role!r}")
    if len(password) < _MIN_ADMIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {_MIN_ADMIN_PASSWORD_LENGTH} characters")
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                new_admin_id, totp_secret = await auth.create_admin_user(
                    conn, username=username, password=password, role=role
                )
            except asyncpg.UniqueViolationError as exc:
                raise AdminUsernameTaken(f"username already taken: {username!r}") from exc
            # Never the password or the TOTP secret -- the audit log is
            # readable by every superadmin, not just the one who ran this.
            await audit.record(
                conn,
                admin_id=admin_id,
                action="admin_users.create",
                target_type="admin_user",
                target_id=str(new_admin_id),
                after={"username": username, "role": role},
                ip_address=ip_address,
            )
    return {
        "id": new_admin_id,
        "totp_secret": totp_secret,
        "totp_provisioning_uri": auth.totp_provisioning_uri(totp_secret, username),
    }


async def set_admin_user_active_admin(
    pool: asyncpg.Pool, *, admin_id: int, target_admin_id: int, is_active: bool, ip_address: str | None
) -> bool:
    if target_admin_id == admin_id:
        raise CannotModifyOwnAccount("cannot deactivate your own account")
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT is_active FROM admin_users WHERE id = $1 FOR UPDATE", target_admin_id
            )
            if before is None:
                return False
            await conn.execute(
                "UPDATE admin_users SET is_active = $2 WHERE id = $1", target_admin_id, is_active
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="admin_users.set_active",
                target_type="admin_user",
                target_id=str(target_admin_id),
                before={"is_active": before["is_active"]},
                after={"is_active": is_active},
                ip_address=ip_address,
            )
    return True


async def set_admin_user_role_admin(
    pool: asyncpg.Pool, *, admin_id: int, target_admin_id: int, role: str, ip_address: str | None
) -> bool:
    if target_admin_id == admin_id:
        raise CannotModifyOwnAccount("cannot change your own role")
    if role not in rbac.KNOWN_ADMIN_ROLES:
        raise ValueError(f"unknown role: {role!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT role FROM admin_users WHERE id = $1 FOR UPDATE", target_admin_id
            )
            if before is None:
                return False
            await conn.execute(
                "UPDATE admin_users SET role = $2 WHERE id = $1", target_admin_id, role
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="admin_users.set_role",
                target_type="admin_user",
                target_id=str(target_admin_id),
                before={"role": before["role"]},
                after={"role": role},
                ip_address=ip_address,
            )
    return True


async def reset_admin_user_password_admin(
    pool: asyncpg.Pool, *, admin_id: int, target_admin_id: int, new_password: str, ip_address: str | None
) -> bool:
    if len(new_password) < _MIN_ADMIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {_MIN_ADMIN_PASSWORD_LENGTH} characters")
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT id FROM admin_users WHERE id = $1 FOR UPDATE", target_admin_id
            )
            if exists is None:
                return False
            await conn.execute(
                "UPDATE admin_users SET password_hash = $2 WHERE id = $1",
                target_admin_id,
                auth.hash_password(new_password),
            )
            # Never the password itself, before or after -- resetting is
            # the one admin_users action with nothing safe to log beyond
            # that it happened.
            await audit.record(
                conn,
                admin_id=admin_id,
                action="admin_users.reset_password",
                target_type="admin_user",
                target_id=str(target_admin_id),
                ip_address=ip_address,
            )
    return True


async def dashboard_summary(pool: asyncpg.Pool) -> dict[str, Any]:
    active_rounds = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status IN ('lobby', 'running', 'settling')"
    )
    active_rooms = await pool.fetchval("SELECT count(*) FROM rooms WHERE is_active = true")
    today = datetime.now(ETHIOPIA_TZ).date()
    # A code review pass caught these as three near-identical scans over
    # the same table for the same day -- one FILTER-based query, bucketing
    # by kind in a single pass, produces the exact same three figures
    # (verified against the original per-query WHERE clauses one for one,
    # including that house_revenue_today has no t.kind restriction of its
    # own, matching the original) instead of three separate round trips.
    # LEFT JOIN users (not INNER): house_revenue's own account row has
    # a.user_id IS NULL (it's a singleton system account), so an INNER
    # join would silently drop every house_revenue_today entry entirely.
    # The is_simulated exclusion only ever actually filters stakes_today/
    # payouts_today, which key off the per-user user_cash account --
    # house_revenue_today is deliberately left untouched by it (see
    # simulated_players_daily_activity() below for why a mixed real+bot
    # round's shared house cut can't be honestly split further than that).
    row = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM(-e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'stake'), 0)
                AS stakes_today,
            COALESCE(SUM(e.amount) FILTER (WHERE a.kind = 'user_cash' AND t.kind = 'payout'), 0)
                AS payouts_today,
            COALESCE(SUM(e.amount) FILTER (WHERE a.kind = 'house_revenue'), 0)
                AS house_revenue_today
        FROM ledger_entries e
        JOIN accounts a ON a.id = e.account_id
        JOIN ledger_transactions t ON t.id = e.transaction_id
        LEFT JOIN users u ON u.id = a.user_id
        WHERE (e.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1
          AND (t.kind IN ('stake', 'payout') OR a.kind = 'house_revenue')
          AND (a.user_id IS NULL OR NOT u.is_simulated)
        """,
        today,
    )
    assert row is not None
    # spec 6226's own Dashboard row: "Live players, active rooms, today's
    # GGR, deposits/withdrawals, alerts" -- an admin opening the
    # dashboard had no way to see anything needs attention without
    # already knowing to click into the Payments screen first. Same
    # WHERE clause as list_pending_withdrawals().
    pending_withdrawals_count = await pool.fetchval(
        "SELECT count(*) FROM payments WHERE direction = 'out' AND status = 'review'"
    )
    return {
        "active_rounds": active_rounds,
        "active_rooms": active_rooms,
        "stakes_today": str(row["stakes_today"]),
        "payouts_today": str(row["payouts_today"]),
        "house_revenue_today": str(row["house_revenue_today"]),
        "pending_withdrawals_count": pending_withdrawals_count,
    }


async def daily_ggr(pool: asyncpg.Pool, on_date: date) -> dict[str, Any]:
    """Gross Gaming Revenue: what the house actually kept that day --
    house_revenue credits from settlements, which already nets stakes
    against payouts (see round_engine.py's _settle_with_winners).
    """
    revenue = await pool.fetchval(
        "SELECT COALESCE(SUM(e.amount), 0) FROM ledger_entries e "
        "JOIN accounts a ON a.id = e.account_id "
        "WHERE a.kind = 'house_revenue' "
        "AND (e.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1",
        on_date,
    )
    rounds_settled = await pool.fetchval(
        "SELECT count(*) FROM rounds WHERE status = 'done' "
        "AND (ended_at AT TIME ZONE 'Africa/Addis_Ababa')::date = $1",
        on_date,
    )
    return {"date": on_date.isoformat(), "ggr": str(revenue), "rounds_settled": rounds_settled}


async def list_pending_withdrawals(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    # provider != 'manual': manual withdrawals always land in 'review'
    # too (see request_withdrawal's force_review), but they need a human
    # to actually send the transfer, not the automatic approve_
    # withdrawal_admin -> enqueue_payout -> payout_worker path this
    # screen's Approve button drives. They get their own queue,
    # list_pending_manual_withdrawals, below.
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.status, p.created_at,
               p.review_reason, pm.kind AS method_kind, pm.account_ref, pm.holder_name
        FROM payments p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN payment_methods pm ON pm.id = p.method_id
        WHERE p.direction = 'out' AND p.status = 'review' AND p.provider != 'manual'
        ORDER BY p.created_at
        """
    )
    return [dict(r) for r in rows]


async def list_stuck_processing_payouts(
    pool: asyncpg.Pool, *, older_than_seconds: int = 3600
) -> list[dict[str, Any]]:
    """Withdrawals still sitting at status='processing' longer than
    expected -- Chapa accepted the transfer request but this codebase has
    no payout webhook route or status-polling fallback to ever learn what
    actually happened to it (see services/payments/payout_worker.py's own
    module docstring). A code review pass caught the worker previously
    treating "processing" as fully settled, a real silent-money-loss risk
    if a transfer Chapa accepted was later actually rejected on their
    side; the fix leaves it genuinely unresolved instead of guessing, but
    "genuinely unresolved with no way to ever find out" is only an
    improvement if something actually surfaces it. This is that surface
    -- a real automated resolution remains blocked on confirming Chapa's
    transfer-status response vocabulary (see DECISIONS.md), so for now
    this is read-only: an admin who sees an entry here needs to check the
    transfer's real status directly with Chapa and resolve it manually
    (payments.status is not itself constrained to only 'succeeded'/
    'failed' by anything that would block a direct correction).
    """
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.provider_ref, p.updated_at
        FROM payments p
        JOIN users u ON u.id = p.user_id
        WHERE p.direction = 'out' AND p.status = 'processing'
          AND p.updated_at < now() - make_interval(secs => $1)
        ORDER BY p.updated_at
        """,
        older_than_seconds,
    )
    return [dict(r) for r in rows]


async def approve_withdrawal_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    reason: str | None,
    ip_address: str | None,
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # provider != 'manual': this function's whole point past the
            # status flip is enqueue_payout() -> payout_worker.py's
            # automatic dispatch, which calls provider.create_payout()
            # on whatever single provider instance that worker was
            # started with (Chapa, in production) -- calling that on a
            # manual-marked payment would be a real cross-provider
            # dispatch bug, not just a UI mismatch. Manual withdrawals
            # go through approve_manual_withdrawal_admin instead, which
            # never enqueues anything.
            row = await conn.fetchrow(
                "SELECT our_ref, status FROM payments WHERE id = $1 AND direction = 'out' "
                "AND provider != 'manual' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "review":
                return False
            await conn.execute(
                "UPDATE payments SET status = 'approved', updated_at = now() WHERE id = $1", payment_id
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="withdrawals.approve",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review"},
                after={"status": "approved"},
                reason=reason,
                ip_address=ip_address,
            )
            our_ref = row["our_ref"]

    await enqueue_payout(redis, our_ref=our_ref, payment_id=payment_id)
    return True


async def reject_withdrawal_admin(
    pool: asyncpg.Pool, redis: Redis, *, admin_id: int, payment_id: int, reason: str, ip_address: str | None
) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, status FROM payments WHERE id = $1 AND direction = 'out' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "review":
                return False

            locked = await ledger.get_or_create_account(conn, row["user_id"], "user_locked")
            cash = await ledger.get_or_create_account(conn, row["user_id"], "user_cash")
            txn = await ledger.post(
                conn,
                "refund",
                [ledger.Entry(locked.id, -row["amount"]), ledger.Entry(cash.id, row["amount"])],
                idempotency_key=f"payout-reject-{payment_id}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'rejected', failure_reason = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                reason,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="withdrawals.reject",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review"},
                after={"status": "rejected"},
                reason=reason,
                ip_address=ip_address,
            )
            user_id = row["user_id"]
            amount = row["amount"]
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()

    await notify_user(
        pool, redis, user_id=user_id, key="notify.withdrawal_rejected", amount=str(amount), reason=reason
    )
    return True


# --- Manual deposits (P1 directive: Jo Bingo must keep taking deposits
# when Chapa is unavailable) -----------------------------------------

async def get_manual_deposit_receipt_file_id(pool: asyncpg.Pool, payment_id: int) -> str | None:
    file_id: str | None = await pool.fetchval(
        "SELECT receipt_telegram_file_id FROM payments WHERE id = $1 AND direction = 'in' "
        "AND provider = 'manual'",
        payment_id,
    )
    return file_id


async def list_pending_manual_deposits(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.status, p.created_at,
               p.provider_ref AS external_reference, p.receipt_telegram_file_id,
               md.method_kind, md.account_ref AS destination_account_ref,
               md.account_name AS destination_account_name,
               p.first_approved_by_admin_id, fa.username AS first_approver_username,
               EXISTS (
                 SELECT 1 FROM payments p2
                 WHERE p2.direction = 'in' AND p2.provider = 'manual' AND p2.id != p.id
                   AND p2.provider_ref = p.provider_ref AND p2.status != 'rejected'
               ) AS possible_duplicate_reference
        FROM payments p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN manual_payment_destinations md ON md.id = p.manual_destination_id
        LEFT JOIN admin_users fa ON fa.id = p.first_approved_by_admin_id
        WHERE p.direction = 'in' AND p.provider = 'manual' AND p.status = 'review'
        ORDER BY p.created_at
        """
    )
    return [dict(r) for r in rows]


class SameAdminCannotProvideSecondApproval(Exception):
    """Raised by approve_manual_deposit_admin/approve_manual_withdrawal_admin
    when the admin calling for a second time is the same one who already
    provided the first approval on a >= two_person_threshold request --
    real maker-checker separation, not just a role check. A genuine
    policy-violation attempt, so this is a loud exception, not a quiet
    no_op the way "someone else already finished this" already is.
    """


async def _apply_two_person_gate(
    conn: ledger.AsyncpgConnection,
    *,
    row: asyncpg.Record | None,
    admin_id: int,
    payment_id: int,
    two_person_threshold: Decimal,
    action_prefix: str,
    reason: str | None,
    ip_address: str | None,
) -> Literal["no_op", "awaiting_second_approval"] | None:
    """The maker-checker gate shared by approve_manual_deposit_admin and
    approve_manual_withdrawal_admin -- a code-review pass caught this
    ~20-line block (the single most security-sensitive logic either
    function has) copy-pasted between the two nearly verbatim, already
    showing minor drift in audit action-string naming.

    Returns a terminal outcome the caller should return immediately as
    -is ('no_op' if the row's gone or already moved past 'review',
    'awaiting_second_approval' if this is a first approval on a
    >= two_person_threshold payment -- the caller's own final action
    must NOT run yet), or None if the row cleared every two-person check
    and the caller should proceed with its own final action (credit the
    ledger, or flip status to 'approved'). Raises
    SameAdminCannotProvideSecondApproval if the same admin tries to
    provide both approvals on a gated payment -- a real policy
    violation, not a quiet no-op.

    Must be called with `row` already fetched `FOR UPDATE` inside the
    caller's own transaction -- the row shape differs between callers
    (deposits need user_id/our_ref for the credit that follows; this
    gate only ever reads amount/status/first_approved_by_admin_id), so
    the SELECT itself stays with each caller rather than moving here.
    """
    if row is None or row["status"] != "review":
        return "no_op"

    if row["amount"] >= two_person_threshold:
        if row["first_approved_by_admin_id"] is None:
            await conn.execute(
                "UPDATE payments SET first_approved_by_admin_id = $2, first_approved_at = now() "
                "WHERE id = $1",
                payment_id,
                admin_id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action=f"{action_prefix}.first_approve",
                target_type="payment",
                target_id=str(payment_id),
                after={"first_approved_by_admin_id": admin_id},
                reason=reason,
                ip_address=ip_address,
            )
            return "awaiting_second_approval"

        if row["first_approved_by_admin_id"] == admin_id:
            raise SameAdminCannotProvideSecondApproval(
                f"admin {admin_id} already provided the first approval for payment {payment_id}"
            )

    return None


ManualDepositApprovalOutcome = Literal["credited", "awaiting_second_approval", "no_op"]


async def approve_manual_deposit_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    reason: str | None,
    ip_address: str | None,
    two_person_threshold: Decimal,
) -> ManualDepositApprovalOutcome:
    # Same shape as approve_withdrawal_admin/reject_withdrawal_admin: row-
    # lock, no-op if the row already moved out of the expected status --
    # that guard IS the idempotency/concurrency mechanism against a
    # double-click, a browser retry, or two admins racing, exactly as it
    # already is for withdrawals. status stays 'review' through the whole
    # awaiting-second-approval window (no new status value), which is
    # exactly why reject_manual_deposit_admin needs zero changes to keep
    # working at any point before a second approval lands.
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, our_ref, status, first_approved_by_admin_id FROM payments "
                "WHERE id = $1 AND direction = 'in' AND provider = 'manual' FOR UPDATE",
                payment_id,
            )
            gate_outcome = await _apply_two_person_gate(
                conn, row=row, admin_id=admin_id, payment_id=payment_id,
                two_person_threshold=two_person_threshold, action_prefix="manual_deposits",
                reason=reason, ip_address=ip_address,
            )
            if gate_outcome is not None:
                return gate_outcome
            assert row is not None  # _apply_two_person_gate already returned "no_op" for None

            # The credit itself is the same shape as deposits.py's own
            # _apply_confirmed_status(): provider_settlement -amount /
            # user_cash +amount, keyed on our_ref.
            provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
            cash_account = await ledger.get_or_create_account(conn, row["user_id"], "user_cash")
            txn = await ledger.post(
                conn,
                "deposit",
                [ledger.Entry(provider_account.id, -row["amount"]), ledger.Entry(cash_account.id, row["amount"])],
                idempotency_key=row["our_ref"],
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'succeeded', ledger_txn_id = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                txn.id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_deposits.approve",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review", "first_approved_by_admin_id": row["first_approved_by_admin_id"]},
                after={"status": "succeeded"},
                reason=reason,
                ip_address=ip_address,
            )
            # Same transaction as the credit above -- see
            # packages/core/referrals.py's own docstring: silent no-ops,
            # never raises, so a fraud signal or missing rule can never
            # fail an admin's own approval action.
            await maybe_grant_referral_bonus(conn, user_id=row["user_id"], deposit_amount=row["amount"])
            await maybe_grant_welcome_bonus(conn, user_id=row["user_id"], deposit_amount=row["amount"])
            user_id = row["user_id"]
            amount = row["amount"]
        # Only reachable once the transaction above has actually
        # committed -- see ledger.post()'s own comment for why it can't
        # safely record this itself when called nested, which every real
        # call is.
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        await ledger.publish_balance_update(pool, redis, user_id)

    balance = await ledger.user_balance_snapshot(pool, user_id)
    # Same notification key an automatic Chapa credit uses -- the same
    # economic event (a deposit landed) regardless of which rail it came
    # in on, matching how ledger_transactions.kind is already shared.
    await notify_user(
        pool, redis, user_id=user_id, key="notify.deposit_confirmed",
        amount=str(amount), balance=balance["cash"],
    )
    return "credited"


async def reject_manual_deposit_admin(
    pool: asyncpg.Pool, redis: Redis, *, admin_id: int, payment_id: int, reason: str, ip_address: str | None
) -> bool:
    # No ledger call at all -- nothing was ever credited for a request
    # still sitting in 'review', so there's nothing to reverse.
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, status FROM payments "
                "WHERE id = $1 AND direction = 'in' AND provider = 'manual' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "review":
                return False

            await conn.execute(
                "UPDATE payments SET status = 'rejected', failure_reason = $2, updated_at = now() "
                "WHERE id = $1",
                payment_id,
                reason,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_deposits.reject",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review"},
                after={"status": "rejected"},
                reason=reason,
                ip_address=ip_address,
            )
            user_id = row["user_id"]
            amount = row["amount"]

    await notify_user(
        pool, redis, user_id=user_id, key="notify.manual_deposit_rejected", amount=str(amount), reason=reason
    )
    return True


# --- Manual withdrawals ------------------------------------------------
#
# A manual withdrawal reaches 'review' via request_withdrawal's own
# force_review=True (see services/payments/withdrawals.py) -- the exact
# same fund-locking/KYC/velocity/chargeback-window gates an automatic
# withdrawal runs, unmodified. From there it needs a real two-checkpoint
# admin flow, not one action: sending a real bank/Telebirr transfer is
# never instantaneous the way an API call is, so there's a genuine gap
# between "we decided to pay this" (approved) and "we have a reference
# number for the transfer we actually sent" (settled) that needs to stay
# visible, mirroring the automatic rail's own 'approved' checkpoint
# before payout_worker.py ever dispatches anything.
#
# reject_withdrawal_admin (above) is reused UNCHANGED for the
# review -> rejected path -- its guard query has no provider filter, so
# it already reverses a manual withdrawal's lock correctly today; writing
# a second, parallel reject function here would just be a fork waiting to
# drift from it.


async def list_pending_manual_withdrawals(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, u.kyc_level, p.our_ref, p.amount, p.status,
               p.created_at, p.review_reason, pm.kind AS method_kind, pm.account_ref, pm.holder_name,
               p.first_approved_by_admin_id, fa.username AS first_approver_username,
               (
                 SELECT count(*) FROM payments p2
                 WHERE p2.user_id = p.user_id AND p2.direction = 'out' AND p2.status = 'succeeded'
               ) AS prior_successful_withdrawals
        FROM payments p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN payment_methods pm ON pm.id = p.method_id
        LEFT JOIN admin_users fa ON fa.id = p.first_approved_by_admin_id
        WHERE p.direction = 'out' AND p.status = 'review' AND p.provider = 'manual'
        ORDER BY p.created_at
        """
    )
    return [dict(r) for r in rows]


async def list_manual_withdrawals_awaiting_settlement(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT p.id, p.user_id, u.display_name, p.our_ref, p.amount, p.status, p.created_at,
               pm.kind AS method_kind, pm.account_ref, pm.holder_name
        FROM payments p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN payment_methods pm ON pm.id = p.method_id
        WHERE p.direction = 'out' AND p.status = 'approved' AND p.provider = 'manual'
        ORDER BY p.created_at
        """
    )
    return [dict(r) for r in rows]


ManualWithdrawalApprovalOutcome = Literal["approved", "awaiting_second_approval", "no_op"]


async def approve_manual_withdrawal_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    reason: str | None,
    ip_address: str | None,
    two_person_threshold: Decimal,
) -> ManualWithdrawalApprovalOutcome:
    """review -> approved. No ledger call -- funds are already locked
    from request_withdrawal() itself; this just records the decision to
    actually send the transfer. Deliberately does NOT call enqueue_payout
    -- there is no automatic dispatch for the manual rail, an admin
    settles it by hand via settle_manual_withdrawal_admin once the
    transfer has genuinely gone out.

    The two-person gate sits here, at the decision to release funds, not
    at settle_manual_withdrawal_admin -- that later step only confirms
    the mechanical transfer this decision already authorized. status
    stays 'review' through the awaiting-second-approval window, same as
    the deposit side, so reject_withdrawal_admin needs no changes either.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT amount, status, first_approved_by_admin_id FROM payments "
                "WHERE id = $1 AND direction = 'out' AND provider = 'manual' FOR UPDATE",
                payment_id,
            )
            gate_outcome = await _apply_two_person_gate(
                conn, row=row, admin_id=admin_id, payment_id=payment_id,
                two_person_threshold=two_person_threshold, action_prefix="manual_withdrawals",
                reason=reason, ip_address=ip_address,
            )
            if gate_outcome is not None:
                return gate_outcome
            assert row is not None  # _apply_two_person_gate already returned "no_op" for None

            await conn.execute(
                "UPDATE payments SET status = 'approved', updated_at = now() WHERE id = $1", payment_id
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_withdrawals.approve",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "review", "first_approved_by_admin_id": row["first_approved_by_admin_id"]},
                after={"status": "approved"},
                reason=reason,
                ip_address=ip_address,
            )
    return "approved"


async def settle_manual_withdrawal_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    external_reference: str,
    reason: str | None,
    ip_address: str | None,
) -> bool:
    """approved -> succeeded, once an admin has actually sent the
    transfer externally and has a real reference for it. Same ledger
    shape as payout_worker.py's own _settle_success(): locked -amount,
    provider_settlement +amount.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, our_ref, status FROM payments "
                "WHERE id = $1 AND direction = 'out' AND provider = 'manual' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "approved":
                return False

            locked = await ledger.get_or_create_account(conn, row["user_id"], "user_locked")
            provider_account = await ledger.get_or_create_account(conn, None, "provider_settlement")
            txn = await ledger.post(
                conn,
                "payout",
                [ledger.Entry(locked.id, -row["amount"]), ledger.Entry(provider_account.id, row["amount"])],
                idempotency_key=f"manual-payout-settle-{row['our_ref']}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'succeeded', provider_ref = $2, ledger_txn_id = $3, "
                "updated_at = now() WHERE id = $1",
                payment_id,
                external_reference,
                txn.id,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_withdrawals.settle",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "approved"},
                after={"status": "succeeded", "provider_ref": external_reference},
                reason=reason,
                ip_address=ip_address,
            )
            user_id = row["user_id"]
            amount = row["amount"]
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        await ledger.publish_balance_update(pool, redis, user_id)

    await notify_user(
        pool, redis, user_id=user_id, key="notify.withdrawal_succeeded", amount=str(amount)
    )
    return True


async def fail_manual_withdrawal_admin(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    admin_id: int,
    payment_id: int,
    reason: str,
    ip_address: str | None,
) -> bool:
    """approved -> failed: the escape hatch for a transfer that turns out
    undeliverable after approval (bad destination discovered too late,
    etc). Same ledger shape as payout_worker.py's own _reverse(): reverses
    the lock back to spendable cash, exactly like an automatic payout
    that failed at the provider.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, amount, our_ref, status FROM payments "
                "WHERE id = $1 AND direction = 'out' AND provider = 'manual' FOR UPDATE",
                payment_id,
            )
            if row is None or row["status"] != "approved":
                return False

            locked = await ledger.get_or_create_account(conn, row["user_id"], "user_locked")
            cash = await ledger.get_or_create_account(conn, row["user_id"], "user_cash")
            txn = await ledger.post(
                conn,
                "refund",
                [ledger.Entry(locked.id, -row["amount"]), ledger.Entry(cash.id, row["amount"])],
                idempotency_key=f"manual-payout-fail-{payment_id}",
                payment_id=payment_id,
            )
            await conn.execute(
                "UPDATE payments SET status = 'failed', failure_reason = $2, updated_at = now() WHERE id = $1",
                payment_id,
                reason,
            )
            await audit.record(
                conn,
                admin_id=admin_id,
                action="manual_withdrawals.fail",
                target_type="payment",
                target_id=str(payment_id),
                before={"status": "approved"},
                after={"status": "failed"},
                reason=reason,
                ip_address=ip_address,
            )
            user_id = row["user_id"]
            amount = row["amount"]
        metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
        await ledger.publish_balance_update(pool, redis, user_id)

    await notify_user(
        pool, redis, user_id=user_id, key="notify.withdrawal_failed", amount=str(amount)
    )
    return True


async def shared_payout_account_clusters(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Risk screen, spec 8.4's first anti-fraud rule: "Same payout account
    across multiple accounts -> Link accounts, flag cluster." Multiple
    distinct users registering the same withdrawal destination
    (`payment_methods.account_ref`, e.g. the same telebirr phone number)
    is a real, live signal collectible from data this codebase already
    has -- unlike device-fingerprint clustering (spec 8.4's other
    account-linking rule), which has no writer anywhere in this codebase
    at all: the Mini App never collects a device fingerprint in the first
    place, and choosing how to (a client-side JS library, what data it's
    allowed to touch under Ethiopian data-protection norms) is a real,
    separate, not-yet-made product decision, not invented here. Live
    query, same pattern as top_players_by_ltv/retention_cohorts -- no
    materialized risk_flags table, no background scan job.
    """
    rows = await pool.fetch(
        """
        SELECT
          pm.kind,
          pm.account_ref,
          count(DISTINCT pm.user_id) AS user_count,
          jsonb_agg(
            jsonb_build_object('user_id', pm.user_id, 'display_name', u.display_name)
            ORDER BY pm.user_id
          ) AS users
        FROM payment_methods pm
        JOIN users u ON u.id = pm.user_id
        GROUP BY pm.kind, pm.account_ref
        HAVING count(DISTINCT pm.user_id) > 1
        ORDER BY user_count DESC, pm.account_ref
        """
    )
    return [{**dict(r), "users": _parse_jsonb(r["users"])} for r in rows]


async def repeat_room_pairings(
    pool: asyncpg.Pool, *, min_shared_rounds: int = 3, since_days: int = 30
) -> list[dict[str, Any]]:
    """Risk screen, spec 8.4's collusion rule: "Winner and loser in the
    same room repeatedly, same pairs -> Collusion investigation." Reports
    every pair of users who have shared a round at least
    `min_shared_rounds` times in the last `since_days` days, alongside
    how many of those shared rounds each one won -- a pair where one side
    wins almost every time is the pattern worth an admin's attention.
    This is deliberately a data screen, not an automatic verdict: the
    spec's own word is "investigation," so the judgment call of what
    counts as suspicious stays with the admin looking at the numbers, the
    same way the risk score/holder-name-match gaps documented in
    README.md are deliberately NOT auto-decided by this codebase either.

    Scoped to a trailing window (not the whole history) because the
    number of pairs in a round grows quadratically with player count
    (up to ~4,950 pairs in a 100-player room) -- this runs on demand from
    the admin console, not on any hot path, but an unbounded full-history
    scan would still get slower every day the platform stays up.

    A player can hold several cards in the same round now, which is a
    second, independent source of inflation the query below has to
    guard against: joining round_entries to itself on round_id alone
    means a user_a holding 2 cards against a user_b holding 3 cards in
    one shared round produces 2*3 = 6 raw pair rows for that single
    round, not 1. `count(DISTINCT round_id)`/`array_agg(DISTINCT
    round_id)` collapse that back down to real distinct rounds shared,
    same as `round_winners` needing `count(DISTINCT round_id)` rather
    than a raw row count once a user can win on more than one of their
    own cards in the same round -- without this, a two-card mutual win
    would double-count as two "wins" of the pair instead of the one
    round it actually was.
    """
    rows = await pool.fetch(
        """
        WITH pairs AS (
            SELECT e1.round_id, e1.user_id AS user_a, e2.user_id AS user_b
            FROM round_entries e1
            JOIN round_entries e2 ON e2.round_id = e1.round_id AND e2.user_id > e1.user_id
            WHERE e1.joined_at > now() - make_interval(days => $2)
        ),
        pair_counts AS (
            SELECT user_a, user_b,
                   count(DISTINCT round_id) AS shared_rounds,
                   array_agg(DISTINCT round_id) AS round_ids
            FROM pairs
            GROUP BY user_a, user_b
            HAVING count(DISTINCT round_id) >= $1
        )
        SELECT
          pc.user_a,
          ua.display_name AS user_a_name,
          pc.user_b,
          ub.display_name AS user_b_name,
          pc.shared_rounds,
          (SELECT count(DISTINCT w.round_id) FROM round_winners w
            WHERE w.user_id = pc.user_a AND w.round_id = ANY(pc.round_ids)) AS user_a_wins,
          (SELECT count(DISTINCT w.round_id) FROM round_winners w
            WHERE w.user_id = pc.user_b AND w.round_id = ANY(pc.round_ids)) AS user_b_wins
        FROM pair_counts pc
        JOIN users ua ON ua.id = pc.user_a
        JOIN users ub ON ub.id = pc.user_b
        ORDER BY pc.shared_rounds DESC
        """,
        min_shared_rounds,
        since_days,
    )
    return [dict(r) for r in rows]
