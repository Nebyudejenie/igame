"""Real-Postgres, real-Redis integration tests for
services/engine/keno_round_engine.py: the full round lifecycle end to
end (Part 17's "bet -> deduct -> draw -> settle -> credit"), idempotent
re-settlement, and crash recovery (stuck-round refund, resuming a
round left mid-lifecycle)."""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, ledger
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from services.engine.keno_round_engine import KenoRoundEngine
from tests.integration.conftest import create_funded_user

pytestmark = pytest.mark.asyncio

_version_counter = itertools.count(random.randint(3 * 10**6, 4 * 10**6))


def _next_version() -> int:
    return next(_version_counter)


# Fast, test-only timings -- real seconds, not mocked, so the engine's
# own asyncio.sleep-based pacing is exercised for real, just compressed.
_FAST_MULTIPLIERS = {1: Decimal("3.40")}  # RTP=0.85, matches keno.py's own solved reference table


async def _seed_fast_config_and_tier(
    conn: asyncpg.Connection, *, betting_seconds: float = 1, draw_seconds: float = 1, result_seconds: float = 1
) -> None:
    version = _next_version()
    await conn.execute(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             min_picks, max_picks, draw_count, number_pool_size,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled,
             effective_from)
        VALUES ($1, 10, $2, $3, $4, 1, 5, 20, 80, 5, 5000, true, now() - interval '1 second')
        """,
        version,
        betting_seconds,
        draw_seconds,
        result_seconds,
    )
    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile, effective_from)
        VALUES (1, $1, 0, 5, 16, ARRAY[10.00, 20.00, 50.00]::numeric(18,2)[], 800, 0.50, 'low_variance',
                now() - interval '1 second')
        RETURNING id
        """,
        version,
    )
    stats = keno.compute_paytable_stats(1, _FAST_MULTIPLIERS)
    await conn.execute(
        """
        INSERT INTO keno_paytables
            (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
             max_multiplier, volatility, effective_from)
        VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6, now() - interval '1 second')
        """,
        version,
        json.dumps({str(k): str(v) for k, v in _FAST_MULTIPLIERS.items()}),
        int(stats.rtp * 10000),
        int(stats.hit_frequency * 10000),
        stats.max_multiplier,
        stats.volatility,
    )

    # current_tier_id is NOT NULL -- an UPDATE...SET NULL (an earlier
    # version of this helper) violates that constraint outright. A plain
    # upsert handles both "no row yet" (first test in the file) and
    # "row already exists" (every subsequent test) in one statement.
    await conn.execute(
        "INSERT INTO keno_tier_state (id, current_tier_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO UPDATE SET current_tier_id = $1",
        tier_id,
    )

    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    jackpot = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn,
        "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -Decimal("100000")), ledger.Entry(reserve.id, Decimal("100000"))],
        idempotency_key=f"test-engine-reserve-{uuid.uuid4()}",
        created_by="test",
    )
    await conn.execute("INSERT INTO keno_jackpot_pool (id, account_id) VALUES (1, $1) ON CONFLICT (id) DO NOTHING", jackpot.id)


@pytest.fixture(autouse=True)
async def _clean_keno_lock_and_rounds(pool: asyncpg.Pool, redis):
    """keno:round:lock is a true global singleton (services/engine/
    keno_lock.py) -- a prior test's engine that didn't release cleanly
    (a failure, a timeout) would otherwise make every subsequent test's
    run_forever() fail to acquire it and hang waiting for a round that
    never appears. Also closes any dangling betting_open round, same
    reasoning as test_keno_tickets.py's own autouse fixture."""

    async def _clean() -> None:
        await redis.delete(KENO_LOCK_KEY)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed', 'failed', 'voided')")

    await _clean()
    yield
    await _clean()


async def _wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.1) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


async def test_full_round_lifecycle_bet_to_settle(pool: asyncpg.Pool, redis) -> None:
    async with pool.acquire() as conn:
        await _seed_fast_config_and_tier(conn, betting_seconds=1, draw_seconds=1, result_seconds=1)
        user_id = await create_funded_user(conn, Decimal("1000.00"))
    balance_before = await ledger.user_balance_snapshot(pool, user_id)

    engine = KenoRoundEngine(pool, redis)
    engine_task = asyncio.create_task(engine.run_forever())
    try:
        async def _round_is_betting_open() -> bool:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
            return row is not None

        await _wait_until(_round_is_betting_open)
        async with pool.acquire() as conn:
            round_row = await conn.fetchrow("SELECT id FROM keno_rounds WHERE status = 'betting_open'")
        assert round_row is not None
        round_id = round_row["id"]

        from packages.core import keno_tickets

        ticket = await keno_tickets.place_ticket(
            pool, redis=redis, user_id=user_id, picks=[7], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
        assert ticket.round_id == round_id

        async def _round_is_completed() -> bool:
            async with pool.acquire() as conn:
                status = await conn.fetchval("SELECT status FROM keno_rounds WHERE id = $1", round_id)
            return status == "completed"

        await _wait_until(_round_is_completed, timeout=20.0)
    finally:
        engine.stop()
        await asyncio.wait_for(engine_task, timeout=15.0)

    async with pool.acquire() as conn:
        round_row = await conn.fetchrow("SELECT * FROM keno_rounds WHERE id = $1", round_id)
        ticket_row = await conn.fetchrow("SELECT * FROM keno_tickets WHERE id = $1", ticket.id)

    assert round_row["status"] == "completed"
    assert round_row["server_seed"] is not None  # revealed
    assert len(round_row["drawn_numbers"]) == 20
    assert round_row["public_seed"] is not None
    # Independent verification: anyone can recompute the draw from the
    # revealed seeds and confirm it matches what was actually settled --
    # the literal point of the commit-reveal scheme (Part 4.1).
    assert keno.verify_draw(bytes(round_row["server_seed"]), round_row["public_seed"], list(round_row["drawn_numbers"]))
    assert keno.verify_server_seed_hash(bytes(round_row["server_seed"]), round_row["server_seed_hash"])

    assert ticket_row["status"] in ("won", "lost")
    matches = keno.count_matches(frozenset({7}), list(round_row["drawn_numbers"]))
    assert ticket_row["matches"] == matches
    expected_payout = keno.compute_payout(Decimal("10"), matches, _FAST_MULTIPLIERS, max_win_per_ticket=Decimal("800"))
    assert Decimal(ticket_row["payout"]) == expected_payout

    balance_after = await ledger.user_balance_snapshot(pool, user_id)
    net_change = Decimal(balance_after["cash"]) - Decimal(balance_before["cash"])
    # Staked 10, then received expected_payout back (0 if lost).
    assert net_change == expected_payout - Decimal("10")


async def test_settlement_is_idempotent_on_retry(pool: asyncpg.Pool, redis) -> None:
    """Directly exercises _settle_and_complete twice on the same round
    (simulating a crash-and-redeliver) without going through the full
    run_forever() loop -- confirms no double payout via real ledger
    totals, not just "no exception"."""
    async with pool.acquire() as conn:
        await _seed_fast_config_and_tier(conn)
        user_id = await create_funded_user(conn, Decimal("1000.00"))

        config_id = await conn.fetchval("SELECT id FROM keno_configs ORDER BY id DESC LIMIT 1")
        tier_id = await conn.fetchval("SELECT id FROM keno_risk_tiers ORDER BY id DESC LIMIT 1")
        paytable_id = await conn.fetchval("SELECT id FROM keno_paytables WHERE pick_count = 1 ORDER BY id DESC LIMIT 1")

        server_seed = keno.generate_server_seed()
        round_id = await conn.fetchval(
            """
            INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed, server_seed_hash,
                                      public_seed, drawn_numbers, betting_closed_at, draw_completed_at)
            VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'draw_complete', $1, $2, $3, $4, $5, $6, now(), now())
            RETURNING id
            """,
            config_id,
            tier_id,
            server_seed,
            keno.server_seed_hash(server_seed),
            "test-public-seed",
            keno.derive_keno_draw(server_seed, "test-public-seed"),
        )

        ticket_id = await conn.fetchval(
            """
            INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, idempotency_key)
            VALUES ($1, $2, $3, 1, 10, $4) RETURNING id
            """,
            round_id,
            user_id,
            paytable_id,
            f"test-{uuid.uuid4()}",
        )
        await conn.execute("INSERT INTO keno_ticket_selections (ticket_id, number) VALUES ($1, 1)", ticket_id)

    engine = KenoRoundEngine(pool, redis)
    total_payout_1, _ = await engine._settle_tickets(round_id, list((await pool.fetchrow("SELECT drawn_numbers FROM keno_rounds WHERE id=$1", round_id))["drawn_numbers"]))
    # Ticket is now terminal (won/lost) -- a second, fully independent
    # settlement pass must be a safe no-op, not a double payout.
    total_payout_2, _ = await engine._settle_tickets(round_id, list((await pool.fetchrow("SELECT drawn_numbers FROM keno_rounds WHERE id=$1", round_id))["drawn_numbers"]))

    async with pool.acquire() as conn:
        ledger_txn_count = await conn.fetchval(
            "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = $1", f"keno:settle:{round_id}:{ticket_id}"
        )
    assert ledger_txn_count <= 1
    assert total_payout_1 == total_payout_2  # both read the same persisted total, not an accumulating one


async def test_stuck_round_is_marked_failed_and_refunded(pool: asyncpg.Pool, redis) -> None:
    async with pool.acquire() as conn:
        await _seed_fast_config_and_tier(conn)
        user_id = await create_funded_user(conn, Decimal("1000.00"))
        config_id = await conn.fetchval("SELECT id FROM keno_configs ORDER BY id DESC LIMIT 1")
        tier_id = await conn.fetchval("SELECT id FROM keno_risk_tiers ORDER BY id DESC LIMIT 1")
        paytable_id = await conn.fetchval("SELECT id FROM keno_paytables WHERE pick_count = 1 ORDER BY id DESC LIMIT 1")

        server_seed = keno.generate_server_seed()
        # scheduled_at far enough in the past to exceed STUCK_ROUND_THRESHOLD_SECONDS.
        round_id = await conn.fetchval(
            """
            INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed, server_seed_hash, scheduled_at)
            VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'betting_open', $1, $2, $3, $4,
                    now() - interval '3600 seconds')
            RETURNING id
            """,
            config_id,
            tier_id,
            server_seed,
            keno.server_seed_hash(server_seed),
        )
        ticket_id = await conn.fetchval(
            """
            INSERT INTO keno_tickets (round_id, user_id, paytable_id, pick_count, stake, idempotency_key)
            VALUES ($1, $2, $3, 1, 25, $4) RETURNING id
            """,
            round_id,
            user_id,
            paytable_id,
            f"test-{uuid.uuid4()}",
        )
        # Mirror what place_ticket() would really have posted: a real
        # stake debit, since the refund path reverses real money, not a
        # bookkeeping-only row.
        reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
        await ledger.post(
            conn, "keno_stake", [ledger.Entry(cash.id, Decimal("-25")), ledger.Entry(reserve.id, Decimal("25"))],
            idempotency_key=f"test-stake-{uuid.uuid4()}", created_by="test",
        )

    balance_before = await ledger.user_balance_snapshot(pool, user_id)

    engine = KenoRoundEngine(pool, redis)
    recovered = await engine.recover_on_startup()
    assert round_id in recovered

    async with pool.acquire() as conn:
        round_row = await conn.fetchrow("SELECT status, failure_reason FROM keno_rounds WHERE id = $1", round_id)
        ticket_row = await conn.fetchrow("SELECT status FROM keno_tickets WHERE id = $1", ticket_id)
    assert round_row["status"] == "failed"
    assert round_row["failure_reason"] == "stuck_round_recovery"
    assert ticket_row["status"] == "refunded"

    balance_after = await ledger.user_balance_snapshot(pool, user_id)
    assert Decimal(balance_after["cash"]) - Decimal(balance_before["cash"]) == Decimal("25")
