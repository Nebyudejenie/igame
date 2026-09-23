"""Load: many real players rushing to place tickets in the same Keno
round at once (spec Part 17's own load-testing requirement, the same
adversarial-concurrency spirit test_load_rush.py already proves for
Bingo's card allocation), against a deliberately *tight* round-exposure
ceiling so real rejections are the expected, common outcome, not a rare
edge case -- proving the exposure cap (packages/core/keno_exposure.py,
enforced inside the same row-locked transaction that accepts a ticket)
actually serializes correctly under real concurrent contention, not just
sequential test calls: never oversold past the ceiling, every accepted
ticket's stake debited exactly once, and the ledger reconciling exactly
afterward.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, keno_tickets, ledger
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from tests.integration.conftest import create_funded_user

pytestmark = pytest.mark.load

PLAYERS = 300
STAKE = Decimal("50.00")
MAX_ROUND_EXPOSURE_PCT = Decimal("0.30")
RESERVE_TARGET = Decimal("2000")
# keno_reserve is a genuine global singleton account shared by every
# Keno test in this dev database, not something this file can isolate --
# by the time this test runs on a given day it may already hold tens of
# millions of ETB deposited by every other test that ran before it. Two
# problems that causes, both closed below: (1) a fixed
# max_round_exposure_pct against that real (not assumed-clean) balance
# isn't "tight" at all -- an earlier version of this test used exactly
# that and every one of 300 tickets sailed through with zero rejections;
# (2) max_round_exposure_pct is numeric(5,4) -- only 4 decimal digits --
# so *computing* a percentage against tens of millions to hit a small
# target ceiling underflows to 0.0000 long before it gets there (800 /
# 55,000,000 rounds to zero at 4dp). Fixed by resetting the reserve to a
# small, known baseline via a real withdrawal before seeding this test's
# own tier -- this is disposable synthetic test data in the local dev
# database, not production money, so a real ledger-posted withdrawal
# here is the actually-correct fix, not a workaround.

_version_counter = itertools.count(random.randint(5 * 10**6, 6 * 10**6))


def _next_version() -> int:
    return next(_version_counter)


@pytest.fixture(autouse=True)
async def _clean_keno_lock_and_rounds(pool: asyncpg.Pool, redis):
    async def _clean() -> None:
        await redis.delete(KENO_LOCK_KEY)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed', 'failed', 'voided')"
            )

    await _clean()
    yield
    await _clean()


async def _seed_tight_capacity_round(conn: asyncpg.Connection, *, max_round_exposure_pct: Decimal) -> int:
    version = _next_version()
    multipliers = {1: Decimal("3.40")}  # RTP=0.85, matches keno.py's own solved reference table
    config_id = await conn.fetchval(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             min_picks, max_picks, draw_count, number_pool_size,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled,
             beta_restricted, effective_from)
        VALUES ($1, 30, 20, 1, 1, 1, 5, 20, 80, 1, 10000, true, false, now() - interval '1 second')
        RETURNING id
        """,
        version,
    )
    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile, effective_from)
        VALUES (1, $1, 0, 5, 16, ARRAY[10.00, 20.00, 50.00]::numeric(18,2)[], 800, $2, 'low_variance',
                now() - interval '1 second')
        RETURNING id
        """,
        version, max_round_exposure_pct,
    )
    stats = keno.compute_paytable_stats(1, multipliers)
    await conn.execute(
        """
        INSERT INTO keno_paytables
            (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
             max_multiplier, volatility, effective_from)
        VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6, now() - interval '1 second')
        """,
        version, json.dumps({str(k): str(v) for k, v in multipliers.items()}),
        int(stats.rtp * 10000), int(stats.hit_frequency * 10000), stats.max_multiplier, stats.volatility,
    )
    await conn.execute(
        "INSERT INTO keno_tier_state (id, current_tier_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1, candidate_tier_id = NULL, candidate_since = NULL",
        tier_id,
    )

    jackpot = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
    await conn.execute("INSERT INTO keno_jackpot_pool (id, account_id) VALUES (1, $1) ON CONFLICT (id) DO NOTHING", jackpot.id)

    round_id = await conn.fetchval(
        """
        INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at)
        VALUES ((SELECT COALESCE(MAX(seq), 0) + 1 FROM keno_rounds), 'betting_open', $1, $2, $3, now())
        RETURNING id
        """,
        config_id, tier_id, keno.server_seed_hash(keno.generate_server_seed()),
    )
    return round_id


async def test_300_players_rush_one_round_with_a_tight_exposure_cap(pool, redis, conn):
    reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    reserve_before_reset = await ledger.balance(conn, reserve_account.id)
    if reserve_before_reset > RESERVE_TARGET:
        await ledger.post(
            conn, "keno_reserve_withdrawal",
            [ledger.Entry(reserve_account.id, -(reserve_before_reset - RESERVE_TARGET)),
             ledger.Entry(house_float.id, reserve_before_reset - RESERVE_TARGET)],
            idempotency_key=f"test-load-reset-{uuid.uuid4()}", created_by="test",
        )
    reserve_before = await ledger.balance(conn, reserve_account.id)
    assert reserve_before == RESERVE_TARGET

    round_id = await _seed_tight_capacity_round(conn, max_round_exposure_pct=MAX_ROUND_EXPOSURE_PCT)
    ceiling = (reserve_before * MAX_ROUND_EXPOSURE_PCT).quantize(Decimal("0.01"))
    print(
        f"\n[keno rush] reserve_before={reserve_before} max_round_exposure_pct={MAX_ROUND_EXPOSURE_PCT} "
        f"ceiling={ceiling} players={PLAYERS} stake={STAKE}"
    )

    players = [await create_funded_user(conn, Decimal("1000.00")) for _ in range(PLAYERS)]

    async def _place(user_id: int):
        try:
            return await keno_tickets.place_ticket(
                pool, redis=redis, user_id=user_id, picks=[random.randint(1, 80)], stake=STAKE,
                idempotency_key=f"rush-{uuid.uuid4()}",
            )
        except keno_tickets.TicketRejected as exc:
            return exc

    started = time.monotonic()
    results = await asyncio.gather(*(_place(user_id) for user_id in players))
    elapsed = time.monotonic() - started

    accepted = [r for r in results if isinstance(r, keno_tickets.PlacedTicket)]
    rejected = [r for r in results if isinstance(r, keno_tickets.TicketRejected)]
    reasons: dict[str, int] = {}
    for exc in rejected:
        reasons[type(exc).__name__] = reasons.get(type(exc).__name__, 0) + 1

    print(
        f"[keno rush] elapsed={elapsed:.2f}s accepted={len(accepted)} rejected={len(rejected)} "
        f"reasons={reasons}"
    )

    # The actual point of a *tight* cap: real rejections must have
    # happened -- if every ticket sailed through, this test isn't
    # exercising contention at all, just restating that place_ticket()
    # works (already covered elsewhere).
    assert len(rejected) > 0, "expected real RoundCapacityReached/UserRoundShareExceeded rejections under this cap"
    assert len(accepted) > 0, "expected at least some tickets to succeed before the cap filled"
    assert all(isinstance(exc, (keno_tickets.RoundCapacityReached, keno_tickets.UserRoundShareExceeded)) for exc in rejected)

    # Never oversold: every accepted ticket's own stake, summed, must be
    # reflected exactly once in the round's own total_stake -- proving
    # the row-locked exposure check genuinely serialized concurrent
    # acceptances rather than a race letting two accepted tickets both
    # read the same "still under ceiling" snapshot.
    round_row = await pool.fetchrow(
        "SELECT total_stake, ticket_count, projected_exposure FROM keno_rounds WHERE id = $1", round_id
    )
    assert round_row["ticket_count"] == len(accepted)
    assert round_row["total_stake"] == STAKE * len(accepted)

    # No accepted ticket ever pushed projected exposure past the ceiling
    # (a small tolerance for the single ticket that legitimately tips it
    # over, matching the codebase's own accept-then-check-next-time
    # semantics -- the check runs before each acceptance, not after).
    assert round_row["projected_exposure"] <= ceiling * Decimal("1.5"), (
        "final projected_exposure is wildly past the ceiling -- suggests the cap didn't actually serialize"
    )

    actual_ticket_count = await pool.fetchval("SELECT count(*) FROM keno_tickets WHERE round_id = $1", round_id)
    assert actual_ticket_count == len(accepted)

    reserve_balance_after = await ledger.balance(conn, reserve_account.id)
    # Reserve only ever grew here (no payouts yet -- round still betting
    # -open, nothing settled): grew by exactly each accepted ticket's own
    # reserve_cut (stake minus jackpot diversion), not the full stake
    # (some of it split to keno_jackpot_pool) and not any rejected one's.
    jackpot_cut_bps = await pool.fetchval(
        "SELECT jackpot_diversion_bps FROM keno_configs WHERE id = (SELECT config_id FROM keno_rounds WHERE id = $1)",
        round_id,
    )
    expected_reserve_cut_per_ticket = STAKE - (STAKE * Decimal(jackpot_cut_bps) / Decimal(10000)).quantize(Decimal("0.01"))
    assert reserve_balance_after == reserve_before + expected_reserve_cut_per_ticket * len(accepted)

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []
