"""Read-only Keno query helpers for the player-facing REST API (build
spec Part 10). Kept separate from services/gateway/queries.py, which is
Bingo-specific -- mirrors the same "packages/core is framework-free
domain logic, the route layer stays thin" split every other feature in
this codebase already follows.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core import keno, keno_config, ledger


async def game_center_state(pool: asyncpg.Pool) -> dict[str, Any] | None:
    """The Game Center / current-round summary Part 10's "current round"
    endpoint needs: status, timestamps, server time, seed hash, offered
    picks/stakes (from the round's own pinned tier, never the live
    "current" tier -- Part 6.2's "an admin editing... must not alter
    in-flight" rule applies here too), and the live jackpot pool. Returns
    None only if Keno has literally never been configured/no round has
    ever been created (a genuinely fresh, un-configured deployment)."""
    async with pool.acquire() as conn:
        round_row = await conn.fetchrow(
            "SELECT * FROM keno_rounds WHERE status NOT IN ('completed','failed','voided') "
            "ORDER BY id DESC LIMIT 1"
        )
        if round_row is None:
            round_row = await conn.fetchrow("SELECT * FROM keno_rounds ORDER BY id DESC LIMIT 1")
        if round_row is None:
            return None

        tier = await conn.fetchrow("SELECT * FROM keno_risk_tiers WHERE id = $1", round_row["tier_id"])
        config = await conn.fetchrow("SELECT * FROM keno_configs WHERE id = $1", round_row["config_id"])
        jackpot_balance = await _jackpot_balance(conn)
        recent_draw = round_row["drawn_numbers"]

    assert tier is not None and config is not None
    max_picks = min(config["max_picks"], tier["max_pick_count"])
    paytable = await paytable_grid(pool, max_picks=max_picks, profile=tier["paytable_profile"])
    return {
        "round_id": round_row["id"],
        "status": round_row["status"],
        "server_seed_hash": round_row["server_seed_hash"],
        "betting_opened_at": round_row["betting_opened_at"],
        "betting_closed_at": round_row["betting_closed_at"],
        "draw_started_at": round_row["draw_started_at"],
        "reveal_index": round_row["reveal_index"],
        "drawn_numbers": list(recent_draw) if recent_draw is not None else None,
        "min_picks": config["min_picks"],
        "max_picks": max_picks,
        "stake_options": [str(s) for s in tier["stake_options"]],
        "max_win_per_ticket": str(tier["max_win_per_ticket"]),
        "tier_number": tier["tier_number"],
        "jackpot_pool": str(jackpot_balance),
        "betting_seconds": config["betting_seconds"],
        "draw_seconds": config["draw_seconds"],
        "result_seconds": config["result_seconds"],
        "paytable": paytable,
    }


async def paytable_grid(pool: asyncpg.Pool, *, max_picks: int, profile: str) -> dict[str, dict[str, str]]:
    """Every pick-count's full {matches: multiplier} table for the round's
    own pinned tier profile, in one payload -- Part 13's "potential payout
    for the current selection updates live from the server-provided
    paytable" and "paytable readable in-app" both need the whole grid up
    front (picking a stake or a pick count must never cost a round-trip),
    not a single pick_count's row the way keno_config.load_active_paytable
    already reads it for ticket placement."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (pick_count) pick_count, multipliers FROM keno_paytables "
            "WHERE pick_count BETWEEN 1 AND $1 AND profile = $2 AND effective_from <= now() "
            "ORDER BY pick_count, effective_from DESC",
            max_picks,
            profile,
        )
    return {str(row["pick_count"]): json.loads(row["multipliers"]) for row in rows}


async def _jackpot_balance(conn: ledger.AsyncpgConnection) -> Decimal:
    account = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
    return await ledger.balance(conn, account.id)


async def my_tickets(
    pool: asyncpg.Pool, user_id: int, *, round_id: int | None = None, limit: int = 20, before_id: int | None = None
) -> list[dict[str, Any]]:
    """Current-round tickets (round_id given) or paginated history
    (before_id is a cursor -- the id of the last row a prior page
    returned)."""
    limit = min(max(limit, 1), 100)
    async with pool.acquire() as conn:
        if round_id is not None:
            rows = await conn.fetch(
                "SELECT t.*, array_agg(s.number ORDER BY s.number) AS picks "
                "FROM keno_tickets t JOIN keno_ticket_selections s ON s.ticket_id = t.id "
                "WHERE t.round_id = $1 AND t.user_id = $2 GROUP BY t.id ORDER BY t.id DESC",
                round_id,
                user_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT t.*, array_agg(s.number ORDER BY s.number) AS picks "
                "FROM keno_tickets t JOIN keno_ticket_selections s ON s.ticket_id = t.id "
                "WHERE t.user_id = $1 AND ($2::bigint IS NULL OR t.id < $2) "
                "GROUP BY t.id ORDER BY t.id DESC LIMIT $3",
                user_id,
                before_id,
                limit,
            )
    return [_ticket_row_to_dict(row) for row in rows]


def _ticket_row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "round_id": row["round_id"],
        "picks": list(row["picks"]),
        "stake": str(row["stake"]),
        "status": row["status"],
        "matches": row["matches"],
        "payout": str(row["payout"]) if row["payout"] is not None else None,
        "jackpot_payout": str(row["jackpot_payout"]) if row["jackpot_payout"] else None,
        "created_at": row["created_at"],
        "settled_at": row["settled_at"],
    }


async def round_detail(pool: asyncpg.Pool, round_id: int) -> dict[str, Any] | None:
    """Full round result + independent-verification payload -- the
    server_seed/draw_order aren't sensitive once a round is terminal
    (the whole point of commit-reveal is that they're meant to be
    published), mirroring services/admin/queries.py::get_round_fairness()'s
    own reasoning for Bingo."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM keno_rounds WHERE id = $1", round_id)
        if row is None:
            return None
        ticket_count = await conn.fetchval("SELECT count(*) FROM keno_tickets WHERE round_id = $1", round_id)

    verified: bool | None = None
    if row["status"] in ("completed", "failed") and row["server_seed"] is not None and row["drawn_numbers"] is not None:
        verified = keno.verify_draw(bytes(row["server_seed"]), row["public_seed"], list(row["drawn_numbers"]))

    return {
        "id": row["id"],
        "status": row["status"],
        "server_seed_hash": row["server_seed_hash"],
        "server_seed": row["server_seed"].hex() if row["server_seed"] is not None else None,
        "public_seed": row["public_seed"],
        "drawn_numbers": list(row["drawn_numbers"]) if row["drawn_numbers"] is not None else None,
        "verified": verified,
        "total_stake": str(row["total_stake"]),
        "total_payout": str(row["total_payout"]),
        "ticket_count": ticket_count,
        "jackpot_hit": row["jackpot_hit"],
        "betting_closed_at": row["betting_closed_at"],
        "completed_at": row["completed_at"],
    }


async def recent_results(pool: asyncpg.Pool, *, limit: int = 20) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 100)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status, drawn_numbers, total_stake, total_payout, jackpot_hit, completed_at "
            "FROM keno_rounds WHERE status IN ('completed', 'failed') "
            "ORDER BY id DESC LIMIT $1",
            limit,
        )
    return [
        {
            "id": row["id"],
            "status": row["status"],
            "drawn_numbers": list(row["drawn_numbers"]) if row["drawn_numbers"] is not None else None,
            "total_stake": str(row["total_stake"]),
            "total_payout": str(row["total_payout"]),
            "jackpot_hit": row["jackpot_hit"],
            "completed_at": row["completed_at"],
        }
        for row in rows
    ]


async def hot_cold_numbers(pool: asyncpg.Pool, *, lookback_rounds: int = 50) -> dict[str, Any]:
    """Frequency of each of the 80 numbers across the last N completed
    rounds' real draws -- a display-only curiosity stat (this codebase's
    own RNG is uniform by construction and proven so in
    tests/unit/test_keno_statistical.py; this endpoint doesn't change
    that, it just shows players the same real history they could compute
    themselves from GET /api/keno/rounds/recent)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT drawn_numbers FROM keno_rounds WHERE status = 'completed' AND drawn_numbers IS NOT NULL "
            "ORDER BY id DESC LIMIT $1",
            lookback_rounds,
        )
    counts = [0] * (keno.NUMBER_POOL_SIZE + 1)
    for row in rows:
        for number in row["drawn_numbers"]:
            counts[number] += 1
    ranked = sorted(range(1, keno.NUMBER_POOL_SIZE + 1), key=lambda n: counts[n], reverse=True)
    return {
        "rounds_considered": len(rows),
        "counts": {str(n): counts[n] for n in range(1, keno.NUMBER_POOL_SIZE + 1)},
        "hottest": ranked[:10],
        "coldest": ranked[-10:],
    }


async def my_statistics(pool: asyncpg.Pool, user_id: int) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                count(*) AS ticket_count,
                COALESCE(SUM(stake), 0) AS total_stake,
                COALESCE(SUM(payout), 0) AS total_payout,
                COALESCE(SUM(jackpot_payout), 0) AS total_jackpot_payout,
                count(*) FILTER (WHERE status = 'won') AS wins,
                count(*) FILTER (WHERE jackpot_payout > 0) AS jackpot_wins
            FROM keno_tickets WHERE user_id = $1
            """,
            user_id,
        )
    assert row is not None
    total_stake = Decimal(row["total_stake"])
    total_payout = Decimal(row["total_payout"]) + Decimal(row["total_jackpot_payout"])
    return {
        "ticket_count": row["ticket_count"],
        "total_stake": str(total_stake),
        "total_payout": str(total_payout),
        "net": str(total_payout - total_stake),
        "wins": row["wins"],
        "jackpot_wins": row["jackpot_wins"],
    }
