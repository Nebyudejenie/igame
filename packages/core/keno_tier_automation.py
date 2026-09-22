"""Keno tier promotion/demotion and the daily payout circuit breaker
(keno.md Part 7.2/7.3) -- the two remaining pieces of the tiered risk
ladder that existed only as schema columns until now
(keno_tier_state.candidate_tier_id/candidate_since, keno_configs.
daily_payout_circuit_breaker_multiple, all previously written only by a
manual admin override, never read by anything automated).

Both functions are meant to be called once per round, from
KenoRoundEngine._create_round() -- before a round's own tier_id is
pinned, so a transition here only ever affects rounds not yet created
(keno.md's own "never mid-round" rule), and at low enough frequency
(once per ~45s round, not per-ticket) that neither's own queries
(a per-tier reserve comparison, a 24h ledger/ticket aggregate) add
meaningful load.

Promotion: min_reserve held continuously for PROMOTION_HOLD_DAYS
("prevents flapping on a lucky day," keno.md's own words) -- tracked via
keno_tier_state.candidate_tier_id/candidate_since, reset (not just left
alone) the moment the reserve drops back below the candidate tier's own
threshold, so a dip that later recovers must restart the full window
rather than resuming a partial one.

Demotion: immediate the moment the reserve falls below the *current*
tier's own min_reserve -- no waiting period, by explicit spec contrast
with promotion's own 7-day rule.

Circuit breaker: if trailing-24h actual payouts (keno_payout +
keno_jackpot_payout ledger credits) exceed
daily_payout_circuit_breaker_multiple times trailing-24h *expected*
payouts (the same expected_payout_contribution each ticket already
carries for the exposure system), demote one tier immediately and
record it distinctly (trigger='circuit_breaker') from an ordinary
reserve-based demotion -- an operator reading keno_tier_changes needs to
tell "the reserve genuinely shrank" apart from "payouts are running hot
relative to what the paytable predicts," two different problems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import structlog

from packages.core import ledger, metrics

logger = structlog.get_logger()

PROMOTION_HOLD = timedelta(days=7)


@dataclass(frozen=True)
class TierChangeResult:
    changed: bool
    from_tier_number: int | None = None
    to_tier_number: int | None = None
    trigger: str | None = None


async def _apply_tier_change(
    conn: asyncpg.pool.PoolConnectionProxy,
    *,
    from_tier_id: int | None,
    to_tier_id: int,
    trigger: str,
    reason: str,
    reserve_balance: Decimal | None,
) -> None:
    await conn.execute(
        "INSERT INTO keno_tier_state (id, current_tier_id, candidate_tier_id, candidate_since) "
        "VALUES (1, $1, NULL, NULL) "
        "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1, candidate_tier_id = NULL, "
        "candidate_since = NULL, updated_at = now()",
        to_tier_id,
    )
    await conn.execute(
        "INSERT INTO keno_tier_changes (from_tier_id, to_tier_id, trigger, reason, reserve_balance_at_change) "
        "VALUES ($1, $2, $3, $4, $5)",
        from_tier_id,
        to_tier_id,
        trigger,
        reason,
        reserve_balance,
    )
    metrics.keno_tier_changes_total.labels(trigger=trigger).inc()


async def evaluate_tier_transition(pool: asyncpg.Pool, *, reserve_balance: Decimal) -> TierChangeResult:
    """Promotion (7-day hold) / immediate demotion by reserve threshold.
    A no-op (returns changed=False) if Keno has never been configured at
    all (no keno_tier_state row) -- nothing to evaluate on a genuinely
    fresh deployment."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1 FOR UPDATE")
            if state is None:
                return TierChangeResult(changed=False)

            current_tier = await conn.fetchrow(
                "SELECT * FROM keno_risk_tiers WHERE id = $1", state["current_tier_id"]
            )
            assert current_tier is not None  # FK guarantees this

            # The latest effective row for EVERY tier_number, not "every
            # row sharing the current tier's own version" -- version is
            # scoped per tier_number independently (create_tier_admin's
            # own version = MAX(version) WHERE tier_number = $1), so an
            # admin editing just Tier 3 bumps only Tier 3's version,
            # leaving 1/2/4 at whatever they were. Same DISTINCT ON
            # -latest-effective-row-per-key pattern keno_paytables' own
            # paytable_grid() already uses for the identical reason.
            all_tiers = await conn.fetch(
                """
                SELECT DISTINCT ON (tier_number) *
                FROM keno_risk_tiers
                WHERE effective_from <= now()
                ORDER BY tier_number, effective_from DESC
                """
            )
            # The highest tier_number whose min_reserve the current balance actually meets.
            eligible = max(
                (t for t in all_tiers if t["min_reserve"] <= reserve_balance),
                key=lambda t: t["tier_number"],
                default=current_tier,
            )

            if eligible["tier_number"] > current_tier["tier_number"]:
                if state["candidate_tier_id"] == eligible["id"] and state["candidate_since"] is not None:
                    held_for = _now_utc() - state["candidate_since"]
                    if held_for >= PROMOTION_HOLD:
                        await _apply_tier_change(
                            conn,
                            from_tier_id=current_tier["id"],
                            to_tier_id=eligible["id"],
                            trigger="automated_promotion",
                            reason=(
                                f"reserve ({reserve_balance}) held above tier {eligible['tier_number']}'s "
                                f"min_reserve ({eligible['min_reserve']}) for {held_for.days}+ days"
                            ),
                            reserve_balance=reserve_balance,
                        )
                        logger.info(
                            "keno_tier_promoted",
                            from_tier=current_tier["tier_number"],
                            to_tier=eligible["tier_number"],
                            reserve_balance=str(reserve_balance),
                        )
                        return TierChangeResult(
                            changed=True,
                            from_tier_number=current_tier["tier_number"],
                            to_tier_number=eligible["tier_number"],
                            trigger="automated_promotion",
                        )
                    return TierChangeResult(changed=False)
                # New candidate, or the reserve dipped and came back --
                # either way this starts (or restarts) the hold window.
                await conn.execute(
                    "UPDATE keno_tier_state SET candidate_tier_id = $1, candidate_since = now(), "
                    "updated_at = now() WHERE id = 1",
                    eligible["id"],
                )
                return TierChangeResult(changed=False)

            if eligible["tier_number"] < current_tier["tier_number"]:
                await _apply_tier_change(
                    conn,
                    from_tier_id=current_tier["id"],
                    to_tier_id=eligible["id"],
                    trigger="automated_demotion",
                    reason=(
                        f"reserve ({reserve_balance}) fell below tier {current_tier['tier_number']}'s "
                        f"min_reserve ({current_tier['min_reserve']})"
                    ),
                    reserve_balance=reserve_balance,
                )
                logger.warning(
                    "keno_tier_demoted",
                    from_tier=current_tier["tier_number"],
                    to_tier=eligible["tier_number"],
                    reserve_balance=str(reserve_balance),
                )
                return TierChangeResult(
                    changed=True,
                    from_tier_number=current_tier["tier_number"],
                    to_tier_number=eligible["tier_number"],
                    trigger="automated_demotion",
                )

            # Sitting exactly at the current tier -- clear any stale
            # candidate tracking from a reserve dip that never actually
            # crossed back down into a demotion (e.g. it hovered right at
            # the boundary): the 7-day clock must not silently keep
            # running for a tier the reserve isn't consistently above.
            if state["candidate_tier_id"] is not None:
                await conn.execute(
                    "UPDATE keno_tier_state SET candidate_tier_id = NULL, candidate_since = NULL, "
                    "updated_at = now() WHERE id = 1"
                )
            return TierChangeResult(changed=False)


async def check_circuit_breaker(pool: asyncpg.Pool) -> TierChangeResult:
    """Part 7.3's daily circuit breaker: trailing-24h actual payouts vs.
    trailing-24h expected payouts, demote one tier immediately if the
    configured multiple is exceeded. A no-op if there's no meaningful
    expected-payout baseline yet (a fresh deployment, or simply a quiet
    24h with no tickets) -- a zero-denominator ratio can't mean anything."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await conn.fetchrow("SELECT * FROM keno_tier_state WHERE id = 1 FOR UPDATE")
            if state is None:
                return TierChangeResult(changed=False)
            config = await conn.fetchrow(
                "SELECT * FROM keno_configs WHERE effective_from <= now() ORDER BY effective_from DESC LIMIT 1"
            )
            if config is None:
                return TierChangeResult(changed=False)

            actual_payout = await conn.fetchval(
                """
                SELECT COALESCE(SUM(le.amount), 0)
                FROM ledger_entries le
                JOIN ledger_transactions lt ON lt.id = le.transaction_id
                WHERE lt.kind IN ('keno_payout', 'keno_jackpot_payout')
                  AND lt.created_at >= now() - interval '24 hours'
                  AND le.amount > 0
                """
            )
            expected_payout = await conn.fetchval(
                """
                SELECT COALESCE(SUM(kt.expected_payout_contribution), 0)
                FROM keno_tickets kt
                JOIN keno_rounds kr ON kr.id = kt.round_id
                WHERE kr.betting_opened_at >= now() - interval '24 hours'
                """
            )
            if expected_payout is None or Decimal(expected_payout) <= 0:
                return TierChangeResult(changed=False)

            multiple = Decimal(config["daily_payout_circuit_breaker_multiple"])
            if Decimal(actual_payout) <= Decimal(expected_payout) * multiple:
                return TierChangeResult(changed=False)

            current_tier = await conn.fetchrow(
                "SELECT * FROM keno_risk_tiers WHERE id = $1", state["current_tier_id"]
            )
            assert current_tier is not None
            lower_tier = await conn.fetchrow(
                """
                SELECT DISTINCT ON (tier_number) *
                FROM keno_risk_tiers
                WHERE effective_from <= now() AND tier_number < $1
                ORDER BY tier_number DESC, effective_from DESC
                LIMIT 1
                """,
                current_tier["tier_number"],
            )
            # Already at the floor tier -- nothing lower to demote to.
            # The breaker having tripped is still real and worth its own
            # alert; keno_round_exposure_ratio / the reserve gauges carry
            # that signal already at Tier 1, no separate action possible
            # here beyond what's already visible.
            if lower_tier is None:
                logger.warning(
                    "keno_circuit_breaker_tripped_at_floor_tier",
                    tier=current_tier["tier_number"],
                    actual_payout=str(actual_payout),
                    expected_payout=str(expected_payout),
                    multiple=str(multiple),
                )
                metrics.keno_tier_changes_total.labels(trigger="circuit_breaker").inc()
                return TierChangeResult(changed=False)

            reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
            reserve_balance = await ledger.balance(conn, reserve_account.id)
            await _apply_tier_change(
                conn,
                from_tier_id=current_tier["id"],
                to_tier_id=lower_tier["id"],
                trigger="circuit_breaker",
                reason=(
                    f"trailing 24h payouts ({actual_payout}) exceeded {multiple}x expected "
                    f"({expected_payout})"
                ),
                reserve_balance=reserve_balance,
            )
            logger.warning(
                "keno_circuit_breaker_tripped",
                from_tier=current_tier["tier_number"],
                to_tier=lower_tier["tier_number"],
                actual_payout=str(actual_payout),
                expected_payout=str(expected_payout),
                multiple=str(multiple),
            )
            return TierChangeResult(
                changed=True,
                from_tier_number=current_tier["tier_number"],
                to_tier_number=lower_tier["tier_number"],
                trigger="circuit_breaker",
            )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
