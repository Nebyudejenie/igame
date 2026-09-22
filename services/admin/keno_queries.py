"""Keno admin queries (build spec Part 15): config/paytable/tier
management and the kill switch, all audited. Mirrors
services/admin/bonus_queries.py's own layering -- domain logic
(packages.core.keno's guardrails, keno_config's "what's active" readers)
stays audit-agnostic; this module is what adds services/admin/audit.py's
audit trail on top, inside the same transaction as the mutation itself.

Every write here is insert-only-versioned (keno_configs/keno_paytables/
keno_risk_tiers rows are never updated in place -- a "change" is a new
row with a later effective_from), which is what makes "effective from
the next round only" (Part 15) true by construction: the engine only
re-reads "what's currently active" at round-creation time.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core import keno, keno_config, ledger, metrics
from services.admin import audit


class InvalidKenoConfig(Exception):
    pass


def _json_safe(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    """services/admin/audit.py's record() does a plain json.dumps() on
    before/after with no custom encoder (the established convention
    throughout this codebase -- every existing admin-query module hand
    -curates its own audit dict rather than passing a raw row for
    exactly this reason). keno_configs/keno_paytables/keno_risk_tiers
    rows carry Decimal, datetime, list[Decimal], and (multipliers) raw
    jsonb-as-string columns that plain json.dumps can't serialize --
    this converts them once, consistently, rather than hand-curating a
    field list per call site and risking one silently drifting out of
    sync with the schema."""
    safe: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            safe[key] = str(value)
        elif isinstance(value, list):
            safe[key] = [str(v) if isinstance(v, Decimal) else v for v in value]
        elif isinstance(value, (bytes, bytearray)):
            safe[key] = value.hex()
        elif hasattr(value, "isoformat"):  # date/datetime/time
            safe[key] = value.isoformat()
        else:
            safe[key] = value
    return safe


async def list_configs_admin(pool: asyncpg.Pool, *, limit: int = 20) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM keno_configs ORDER BY id DESC LIMIT $1", limit)
    return [dict(row) for row in rows]


async def get_active_config_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await keno_config.load_active_config(conn)
    return dict(row)


async def create_config_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    round_cycle_seconds: int,
    betting_seconds: int,
    draw_seconds: int,
    result_seconds: int,
    min_picks: int,
    max_picks: int,
    max_tickets_per_user_per_round: int,
    per_user_round_capacity_share_bps: int,
    jackpot_diversion_bps: int,
    rtp_floor_bps: int = 7500,
    rtp_ceiling_bps: int = 9700,
    keno_enabled: bool,
    reason: str,
    effective_from: Any = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise InvalidKenoConfig("reason is required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchval("SELECT COALESCE(MAX(version), 0) + 1 FROM keno_configs")
            row = await conn.fetchrow(
                """
                INSERT INTO keno_configs
                    (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
                     min_picks, max_picks, max_tickets_per_user_per_round,
                     per_user_round_capacity_share_bps, jackpot_diversion_bps,
                     rtp_floor_bps, rtp_ceiling_bps, keno_enabled, created_by_admin_id,
                     effective_from)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        COALESCE($15, now()))
                RETURNING *
                """,
                version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
                min_picks, max_picks, max_tickets_per_user_per_round,
                per_user_round_capacity_share_bps, jackpot_diversion_bps,
                rtp_floor_bps, rtp_ceiling_bps, keno_enabled, admin_id, effective_from,
            )
            assert row is not None  # INSERT ... RETURNING * always yields exactly one row
            await audit.record(
                conn, admin_id=admin_id, action="keno.config.create", target_type="keno_config",
                target_id=str(row["id"]), before=None, after=_json_safe(row), reason=reason,
                ip_address=ip_address,
            )
    return dict(row)


async def set_keno_enabled_admin(
    pool: asyncpg.Pool, *, admin_id: int, enabled: bool, reason: str, ip_address: str | None = None
) -> dict[str, Any]:
    """The kill switch (Part 0: "one admin toggle... that instantly stops
    new rounds and blocks new tickets while still settling in-flight
    tickets"). A new config row (not an UPDATE, matching every other
    keno_configs write) copying the current active config's every other
    field, flipping only keno_enabled -- so the audit trail shows exactly
    what changed and nothing else silently reset to a default. Turning it
    off never touches a round already past betting_open (Part 0's own
    "still settling in-flight tickets" requirement falls out of this for
    free: place_ticket() checks keno_enabled, the round engine's own
    lifecycle does not)."""
    if not reason.strip():
        raise InvalidKenoConfig("reason is required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await keno_config.load_active_config(conn)
            version = await conn.fetchval("SELECT COALESCE(MAX(version), 0) + 1 FROM keno_configs")
            row = await conn.fetchrow(
                """
                INSERT INTO keno_configs
                    (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
                     min_picks, max_picks, max_tickets_per_user_per_round,
                     per_user_round_capacity_share_bps, jackpot_diversion_bps,
                     rtp_floor_bps, rtp_ceiling_bps, keno_enabled, created_by_admin_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                RETURNING *
                """,
                version, current["round_cycle_seconds"], current["betting_seconds"],
                current["draw_seconds"], current["result_seconds"], current["min_picks"],
                current["max_picks"], current["max_tickets_per_user_per_round"],
                current["per_user_round_capacity_share_bps"], current["jackpot_diversion_bps"],
                current["rtp_floor_bps"], current["rtp_ceiling_bps"], enabled, admin_id,
            )
            assert row is not None  # INSERT ... RETURNING * always yields exactly one row
            await audit.record(
                conn, admin_id=admin_id, action="keno.kill_switch",
                target_type="keno_config", target_id=str(row["id"]),
                before={"keno_enabled": current["keno_enabled"]}, after={"keno_enabled": enabled},
                reason=reason, ip_address=ip_address,
            )
    return dict(row)


async def list_paytables_admin(pool: asyncpg.Pool, *, pick_count: int | None = None) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        if pick_count is not None:
            rows = await conn.fetch(
                "SELECT * FROM keno_paytables WHERE pick_count = $1 ORDER BY id DESC", pick_count
            )
        else:
            rows = await conn.fetch("SELECT * FROM keno_paytables ORDER BY pick_count, id DESC")
    return [_paytable_row_to_dict(row) for row in rows]


def _paytable_row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    data["multipliers"] = json.loads(row["multipliers"])
    return data


def preview_paytable_stats(pick_count: int, multipliers: dict[str, str]) -> dict[str, Any]:
    """Part 15's "live RTP, hit frequency, volatility" preview -- pure,
    no DB, callable on every keystroke of the admin paytable editor
    before anything is saved."""
    decimal_multipliers = {int(k): Decimal(v) for k, v in multipliers.items()}
    stats = keno.compute_paytable_stats(pick_count, decimal_multipliers)
    within_guardrail = keno.RTP_FLOOR <= stats.rtp <= keno.RTP_CEILING
    return {
        "rtp": str(stats.rtp),
        "hit_frequency": str(stats.hit_frequency),
        "max_multiplier": str(stats.max_multiplier),
        "volatility": str(stats.volatility),
        "within_guardrail": within_guardrail,
        "rtp_floor": str(keno.RTP_FLOOR),
        "rtp_ceiling": str(keno.RTP_CEILING),
    }


async def create_paytable_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    pick_count: int,
    profile: str,
    multipliers: dict[str, str],
    reason: str,
    effective_from: Any = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    """Validates the RTP guardrail server-side (Part 3.2: "refuse to
    activate any paytable where RTP exceeds/falls below" the configured
    ceiling/floor) -- this is the actual enforcement point, never just a
    UI warning the admin console could be bypassed by calling the API
    directly."""
    if not reason.strip():
        raise InvalidKenoConfig("reason is required")
    if profile not in ("low_variance", "standard"):
        raise InvalidKenoConfig(f"unknown profile: {profile!r}")

    decimal_multipliers = {int(k): Decimal(v) for k, v in multipliers.items()}
    try:
        rtp = keno.validate_paytable_rtp(pick_count, decimal_multipliers)
    except keno.PaytableRTPOutOfRange as exc:
        raise InvalidKenoConfig(str(exc)) from exc
    stats = keno.compute_paytable_stats(pick_count, decimal_multipliers)

    async with pool.acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM keno_paytables WHERE pick_count = $1", pick_count
            )
            row = await conn.fetchrow(
                """
                INSERT INTO keno_paytables
                    (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
                     max_multiplier, volatility, created_by_admin_id, effective_from)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, COALESCE($10, now()))
                RETURNING *
                """,
                pick_count, version, profile, json.dumps(multipliers),
                int(rtp * 10000), int(stats.hit_frequency * 10000), stats.max_multiplier,
                stats.volatility, admin_id, effective_from,
            )
            assert row is not None  # INSERT ... RETURNING * always yields exactly one row
            await audit.record(
                conn, admin_id=admin_id, action="keno.paytable.create", target_type="keno_paytable",
                target_id=str(row["id"]), before=None, after=_json_safe(_paytable_row_to_dict(row)), reason=reason,
                ip_address=ip_address,
            )
    return _paytable_row_to_dict(row)


async def list_tiers_admin(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM keno_risk_tiers ORDER BY tier_number, id DESC")
        current = await conn.fetchrow(
            "SELECT current_tier_id, candidate_tier_id, candidate_since FROM keno_tier_state WHERE id = 1"
        )
    tiers = [dict(row) for row in rows]
    current_tier_id = current["current_tier_id"] if current else None
    for tier in tiers:
        tier["is_current"] = tier["id"] == current_tier_id
    return tiers


async def create_tier_admin(
    pool: asyncpg.Pool,
    *,
    admin_id: int,
    tier_number: int,
    min_reserve: Decimal,
    max_pick_count: int,
    max_top_multiplier: Decimal,
    stake_options: list[Decimal],
    max_win_per_ticket: Decimal,
    max_round_exposure_pct: Decimal,
    paytable_profile: str,
    reason: str,
    effective_from: Any = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise InvalidKenoConfig("reason is required")
    if paytable_profile not in ("low_variance", "standard"):
        raise InvalidKenoConfig(f"unknown profile: {paytable_profile!r}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM keno_risk_tiers WHERE tier_number = $1", tier_number
            )
            row = await conn.fetchrow(
                """
                INSERT INTO keno_risk_tiers
                    (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
                     stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile,
                     created_by_admin_id, effective_from)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, COALESCE($11, now()))
                RETURNING *
                """,
                tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
                stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile,
                admin_id, effective_from,
            )
            assert row is not None  # INSERT ... RETURNING * always yields exactly one row
            await audit.record(
                conn, admin_id=admin_id, action="keno.tier.create", target_type="keno_risk_tier",
                target_id=str(row["id"]), before=None, after=_json_safe(row), reason=reason,
                ip_address=ip_address,
            )
    return dict(row)


async def set_current_tier_admin(
    pool: asyncpg.Pool, *, admin_id: int, tier_id: int, reason: str, ip_address: str | None = None
) -> dict[str, Any]:
    """Manual override of keno_tier_state (Part 7.2's promotion/demotion
    target) -- always available to a superadmin regardless of what the
    automated reserve-threshold evaluation (packages/core/
    keno_tier_automation.py, wired into KenoRoundEngine._create_round())
    would otherwise choose, the same "admin can always directly act"
    precedent services/admin/queries.py's void_round_admin/stop_room_admin
    already set for Bingo -- the next automated evaluation simply resumes
    from wherever this override left current_tier_id, same as it would
    after any other change. Never affects an in-flight round (Part 7.2:
    "never mid-round") -- rounds pin their own tier_id at creation and
    never re-read this table again. Recorded in keno_tier_changes
    (trigger='admin_override') alongside the existing admin_audit_log
    entry -- the former is the one place with a full tier history
    regardless of actor, the latter is admin_audit_log's own "which admin
    did this" record specifically."""
    if not reason.strip():
        raise InvalidKenoConfig("reason is required")
    async with pool.acquire() as conn:
        async with conn.transaction():
            tier = await conn.fetchrow("SELECT * FROM keno_risk_tiers WHERE id = $1", tier_id)
            if tier is None:
                raise InvalidKenoConfig(f"no such tier: {tier_id}")
            before = await conn.fetchrow("SELECT current_tier_id FROM keno_tier_state WHERE id = 1")
            await conn.execute(
                "INSERT INTO keno_tier_state (id, current_tier_id, candidate_tier_id, candidate_since) "
                "VALUES (1, $1, NULL, NULL) "
                "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1, candidate_tier_id = NULL, "
                "candidate_since = NULL, updated_at = now()",
                tier_id,
            )
            reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
            reserve_balance = await ledger.balance(conn, reserve_account.id)
            await conn.execute(
                "INSERT INTO keno_tier_changes "
                "(from_tier_id, to_tier_id, trigger, reason, reserve_balance_at_change, admin_id) "
                "VALUES ($1, $2, 'admin_override', $3, $4, $5)",
                before["current_tier_id"] if before else None,
                tier_id,
                reason,
                reserve_balance,
                admin_id,
            )
            metrics.keno_tier_changes_total.labels(trigger="admin_override").inc()
            await audit.record(
                conn, admin_id=admin_id, action="keno.tier.set_current", target_type="keno_risk_tier",
                target_id=str(tier_id),
                before={"current_tier_id": before["current_tier_id"] if before else None},
                after={"current_tier_id": tier_id}, reason=reason, ip_address=ip_address,
            )
    return dict(tier)


async def dashboard_summary_admin(pool: asyncpg.Pool) -> dict[str, Any]:
    """Part 15's dashboard: current round, reserve vs. player liability
    shown separately (the operator's own explicit warning: conflating
    them is how operators misjudge solvency), current tier, jackpot
    pool."""
    async with pool.acquire() as conn:
        round_row = await conn.fetchrow(
            "SELECT id, status, total_stake, total_payout, projected_exposure, ticket_count "
            "FROM keno_rounds WHERE status NOT IN ('completed','failed','voided') ORDER BY id DESC LIMIT 1"
        )
        reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
        jackpot_account = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
        reserve_balance = await ledger.balance(conn, reserve_account.id)
        jackpot_balance = await ledger.balance(conn, jackpot_account.id)
        # Player liability: sum of every user's cash+bonus-locked balance
        # -- money the operator owes, structurally separate from the
        # reserve (money the operator has to pay claims from). Never the
        # same number, and conflating them is exactly the operator's own
        # named risk in Part 7.1.
        liability = await conn.fetchval(
            "SELECT COALESCE(SUM(balance), 0) FROM account_balances WHERE kind IN ('user_cash', 'user_bonus')"
        )
        current_tier = await conn.fetchrow(
            "SELECT tiers.tier_number, tiers.id FROM keno_tier_state "
            "JOIN keno_risk_tiers tiers ON tiers.id = keno_tier_state.current_tier_id WHERE keno_tier_state.id = 1"
        )
        config = await keno_config.load_active_config(conn)

    return {
        "keno_enabled": config["keno_enabled"],
        "current_round": dict(round_row) if round_row else None,
        "reserve_balance": str(reserve_balance),
        "player_liability": str(liability),
        "jackpot_pool": str(jackpot_balance),
        "current_tier_number": current_tier["tier_number"] if current_tier else None,
    }
