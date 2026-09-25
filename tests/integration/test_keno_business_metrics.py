"""Part 8 revenue/retention metrics (packages/core/keno_business_metrics.py)
against real Postgres. Every test seeds its own tickets at a randomized
far-future synthetic time and computes at that time, so nothing else in
the shared dev database (or a previous run of this same file) can land
inside its window."""

from __future__ import annotations

import itertools
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, keno_business_metrics, metrics
from tests.integration.conftest import create_user

pytestmark = pytest.mark.asyncio

_version_counter = itertools.count(random.randint(20 * 10**6, 21 * 10**6))
_MULTIPLIERS = {1: Decimal("3.40")}


def _synthetic_now() -> datetime:
    # A random day far in the future: unique per test, per run.
    return datetime(2100, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=random.randint(0, 200_000))


async def _seed_keno_refs(conn: asyncpg.Connection) -> tuple[int, int, int]:
    version = next(_version_counter)
    config_id = await conn.fetchval(
        "INSERT INTO keno_configs (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds, "
        "max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled, beta_restricted) "
        "VALUES ($1, 45, 25, 12, 8, 5, 5000, false, true) RETURNING id",
        version,
    )
    tier_id = await conn.fetchval(
        "INSERT INTO keno_risk_tiers (tier_number, version, min_reserve, max_pick_count, max_top_multiplier, "
        "stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile) "
        "VALUES (1, $1, 0, 5, 16, ARRAY[10.00,20.00,50.00]::numeric(18,2)[], 800, 0.10, 'low_variance') RETURNING id",
        version,
    )
    stats = keno.compute_paytable_stats(1, _MULTIPLIERS)
    paytable_id = await conn.fetchval(
        "INSERT INTO keno_paytables (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps, "
        "max_multiplier, volatility) VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6) RETURNING id",
        version, json.dumps({"1": "3.40"}), int(stats.rtp * 10000), int(stats.hit_frequency * 10000),
        stats.max_multiplier, stats.volatility,
    )
    return config_id, tier_id, paytable_id


async def _round(conn: asyncpg.Connection, refs: tuple[int, int, int]) -> int:
    config_id, tier_id, _ = refs
    return await conn.fetchval(
        "INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash) "
        "VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'completed', $1, $2, 'x') RETURNING id",
        config_id, tier_id,
    )


async def _ticket(conn, refs, *, user_id: int, round_id: int, at: datetime, stake: str, payout: str,
                  jackpot: str | None = None, status: str | None = None) -> None:
    status = status or ("won" if Decimal(payout) > 0 else "lost")
    await conn.execute(
        "INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, status, payout, jackpot_payout, "
        "idempotency_key, created_at) VALUES ($1, $2, $3, 1, $4, $5, $6, $7, $8, $9)",
        round_id, user_id, refs[2], Decimal(stake), status, Decimal(payout),
        Decimal(jackpot) if jackpot else None, f"test-biz-{uuid.uuid4()}", at,
    )


async def test_session_grouping_splits_on_a_gap_longer_than_30_minutes() -> None:
    t0 = datetime(2100, 1, 1, tzinfo=timezone.utc)
    rows = [
        (1, t0, 10),
        (1, t0 + timedelta(minutes=29), 11),   # same session: gap 29m
        (1, t0 + timedelta(minutes=29 + 31), 12),  # new session: gap 31m
        (2, t0, 10),
    ]
    sessions = sorted(keno_business_metrics.group_sessions(rows), key=lambda s: (s.user_id, s.start))
    assert [(s.user_id, s.ticket_count, s.round_count) for s in sessions] == [(1, 2, 2), (1, 1, 1), (2, 1, 1)]
    assert sessions[0].end - sessions[0].start == timedelta(minutes=29)


async def test_decomposition_multiplies_out_to_real_ggr(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    now = _synthetic_now()
    refs = await _seed_keno_refs(conn)
    r1, r2, r3 = [await _round(conn, refs) for _ in range(3)]
    alice, bob = await create_user(conn), await create_user(conn)

    # Alice: session 1 = two tickets in r1 + one in r2; session 2 (2h later) = one ticket in r3.
    await _ticket(conn, refs, user_id=alice, round_id=r1, at=now - timedelta(hours=5), stake="10", payout="0")
    await _ticket(conn, refs, user_id=alice, round_id=r1, at=now - timedelta(hours=5), stake="20", payout="34")
    await _ticket(conn, refs, user_id=alice, round_id=r2, at=now - timedelta(hours=4, minutes=50), stake="10", payout="0")
    await _ticket(conn, refs, user_id=alice, round_id=r3, at=now - timedelta(hours=2), stake="50", payout="0")
    # Bob: one session, one ticket, with a jackpot win counted as payout.
    await _ticket(conn, refs, user_id=bob, round_id=r2, at=now - timedelta(hours=3), stake="10", payout="34", jackpot="6")
    # Outside the window, pending, and refunded tickets must all be ignored.
    await _ticket(conn, refs, user_id=bob, round_id=r1, at=now - timedelta(hours=30), stake="50", payout="0")
    await _ticket(conn, refs, user_id=bob, round_id=r3, at=now - timedelta(hours=1), stake="50", payout="0", status="pending")
    await _ticket(conn, refs, user_id=bob, round_id=r3, at=now - timedelta(hours=1), stake="50", payout="0", status="refunded")

    m = await keno_business_metrics.compute(pool, now=now)

    handle = Decimal("100")  # 10+20+10+50+10
    ggr = handle - Decimal("34") - Decimal("34") - Decimal("6")  # 26
    assert m.dau == 2
    assert m.sessions == 3
    assert m.ggr == ggr
    assert m.avg_stake == Decimal("20.00")
    assert m.hold_pct == pytest.approx(float(ggr / handle))
    assert m.tickets_per_round == pytest.approx(5 / 4)  # participations: alice r1,r2,r3 + bob r2
    assert m.arpdau == Decimal("13.00")
    product = (m.dau * m.sessions_per_user * m.rounds_per_session * m.tickets_per_round
               * float(m.avg_stake) * m.hold_pct)
    assert product == pytest.approx(float(ggr))


async def test_retention_cohorts(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    now = _synthetic_now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    refs = await _seed_keno_refs(conn)
    rnd = await _round(conn, refs)

    # D1 cohort = first play two days ago; retained if they played yesterday.
    d1_day = today - timedelta(days=2)
    returning, churned = await create_user(conn), await create_user(conn)
    for user_id in (returning, churned):
        await _ticket(conn, refs, user_id=user_id, round_id=rnd, at=d1_day + timedelta(hours=10), stake="10", payout="0")
    await _ticket(conn, refs, user_id=returning, round_id=rnd, at=today - timedelta(hours=5), stake="10", payout="0")

    m = await keno_business_metrics.compute(pool, now=now)
    assert m.d1_retention == pytest.approx(0.5)
    # Nobody first played 8 or 31 days ago in this synthetic window: no cohort, NaN -- not 0.
    assert math.isnan(m.d7_retention)
    assert math.isnan(m.d30_retention)


async def test_ltv_counts_lifetime_value_of_currently_active_players_only(pool, conn) -> None:
    now = _synthetic_now()
    refs = await _seed_keno_refs(conn)
    rnd = await _round(conn, refs)
    active, lapsed = await create_user(conn), await create_user(conn)
    # active: lifetime net 40 (one old loss of 30 + a recent loss of 10).
    await _ticket(conn, refs, user_id=active, round_id=rnd, at=now - timedelta(days=90), stake="30", payout="0")
    await _ticket(conn, refs, user_id=active, round_id=rnd, at=now - timedelta(days=2), stake="10", payout="0")
    # lapsed: only played 60 days ago -- excluded from the current-player LTV.
    await _ticket(conn, refs, user_id=lapsed, round_id=rnd, at=now - timedelta(days=60), stake="500", payout="0")

    m = await keno_business_metrics.compute(pool, now=now)
    assert m.player_ltv == Decimal("40.00")


async def test_deposit_conversion_ignores_non_terminal_deposits(pool, conn) -> None:
    now = _synthetic_now()
    user_id = await create_user(conn)
    for status in ("succeeded", "succeeded", "failed", "pending", "review"):
        await conn.execute(
            "INSERT INTO payments (user_id, direction, provider, our_ref, amount, status, created_at) "
            "VALUES ($1, 'in', 'manual', $2, 100, $3, $4)",
            user_id, f"test-biz-{uuid.uuid4()}", status, now - timedelta(hours=1),
        )
    m = await keno_business_metrics.compute(pool, now=now)
    assert m.deposit_conversion_rate == pytest.approx(2 / 3)


async def test_publisher_sets_gauges_and_observes_each_closed_session_once(pool, conn) -> None:
    now = _synthetic_now()
    refs = await _seed_keno_refs(conn)
    rnd = await _round(conn, refs)
    user_id = await create_user(conn)
    await _ticket(conn, refs, user_id=user_id, round_id=rnd, at=now - timedelta(hours=3), stake="10", payout="0")
    await _ticket(conn, refs, user_id=user_id, round_id=rnd, at=now - timedelta(hours=3) + timedelta(minutes=10),
                  stake="10", payout="0")

    publisher = keno_business_metrics.Publisher(started_at=now - timedelta(days=1))
    count_before = metrics.keno_session_duration_seconds._sum.get()
    await publisher.publish(pool, now=now)
    await publisher.publish(pool, now=now)  # a second refresh must not re-observe the same session

    assert metrics.keno_dau._value.get() == 1
    assert metrics.keno_hold_pct._value.get() == pytest.approx(1.0)
    assert metrics.keno_session_duration_seconds._sum.get() - count_before == pytest.approx(600)
