"""Shared "what's currently effective" readers for Keno's insert-only
-versioned config tables (keno_configs / keno_risk_tiers / keno_paytables).

Used by both packages/core/keno_tickets.py (ticket placement) and
services/engine/keno_round_engine.py (the round lifecycle) -- promoted
here rather than duplicated in each, the same "one real implementation"
discipline DECISIONS.md documents for packages/core/phone.py.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg

from packages.core import ledger


async def load_active_config(conn: ledger.AsyncpgConnection) -> asyncpg.Record:
    row = await conn.fetchrow(
        "SELECT * FROM keno_configs WHERE effective_from <= now() ORDER BY effective_from DESC LIMIT 1"
    )
    if row is None:
        raise RuntimeError("no active keno_configs row -- Keno has never been configured on this deployment")
    return row


async def load_current_tier(conn: ledger.AsyncpgConnection) -> asyncpg.Record:
    """The tier keno_tier_state currently points at (Part 7.2's
    promotion/demotion state machine), not just "the latest version of
    some tier_number" -- tier changes are a deliberate, audited decision
    (see services/engine/keno_round_engine.py's tier-evaluation sweep),
    not automatically the newest row."""
    row = await conn.fetchrow(
        """
        SELECT tiers.* FROM keno_tier_state
        JOIN keno_risk_tiers tiers ON tiers.id = keno_tier_state.current_tier_id
        WHERE keno_tier_state.id = 1
        """
    )
    if row is None:
        raise RuntimeError("keno_tier_state has no row -- Keno's risk tier has never been initialized")
    return row


async def is_user_allowed_to_play(conn: ledger.AsyncpgConnection, user_id: int, config: asyncpg.Record) -> bool:
    """Whether this specific user may see live round state or place a
    ticket right now -- both keno_enabled AND, while a staged launch is
    still restricting access, allowlist membership. Both
    packages.core.keno_queries.game_center_state (visibility) and
    packages.core.keno_tickets.place_ticket (betting) must call this
    exact function rather than reimplementing the check -- a 2026-09-23
    CTO review found the visibility side had drifted from the betting
    side (the state endpoint never checked keno_enabled at all), and a
    single shared check is how that class of drift gets structurally
    prevented going forward, not just fixed once."""
    if not config["keno_enabled"]:
        return False
    if not config["beta_restricted"]:
        return True
    row = await conn.fetchrow("SELECT 1 FROM keno_beta_allowlist WHERE user_id = $1", user_id)
    return row is not None


async def load_active_paytable(conn: ledger.AsyncpgConnection, pick_count: int, profile: str) -> asyncpg.Record:
    row = await conn.fetchrow(
        "SELECT * FROM keno_paytables WHERE pick_count = $1 AND profile = $2 "
        "AND effective_from <= now() ORDER BY effective_from DESC LIMIT 1",
        pick_count,
        profile,
    )
    if row is None:
        raise RuntimeError(f"no active keno_paytable for pick_count={pick_count} profile={profile}")
    return row


def paytable_multipliers(paytable_row: asyncpg.Record) -> dict[int, Decimal]:
    """asyncpg returns jsonb as a raw string (no codec registered on this
    pool) -- every caller needs this same json.loads(), so it lives here
    once rather than being repeated at each call site."""
    return {int(k): Decimal(v) for k, v in json.loads(paytable_row["multipliers"]).items()}
