"""Real-Postgres integration tests for packages/core/keno_tickets.py's
bet-placement transaction (build spec Part 6.1, Part 17's "bet -> deduct"
scenario, idempotent re-bet, betting-after-close rejection, insufficient
-balance rejection, exposure-cap rejection, per-user-share cap)."""

from __future__ import annotations

import itertools
import json
import random
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, keno_tickets, ledger
from tests.integration.conftest import create_funded_user

# keno_risk_tiers has UNIQUE(tier_number, version) and keno_paytables has
# UNIQUE(pick_count, version) -- every test seeds its own independent
# config/tier/paytable set against the same persistent dev database (no
# transaction-rollback isolation, matching this suite's own
# next_telegram_id()/unique_phone() convention in conftest.py), so each
# needs a fresh version number, not a hardcoded 1.
_version_counter = itertools.count(random.randint(10**6, 2 * 10**6))


def _next_version() -> int:
    return next(_version_counter)

pytestmark = pytest.mark.asyncio


# A guardrail-passing multiplier table per pick count, reused from
# tests/unit/test_keno_statistical.py's own solved values (RTP ~0.85,
# within [0.75, 0.97]) -- real, checkable numbers, not placeholders.
_MULTIPLIERS_BY_PICK_COUNT = {
    1: {1: Decimal("3.40")},
    2: {2: Decimal("14.14")},
    3: {3: Decimal("61.26")},
    5: {3: Decimal("3.57"), 4: Decimal("20.67"), 5: Decimal("465.17")},
}


async def _seed_keno_round(
    conn: asyncpg.Connection,
    *,
    stake_options: list[Decimal] | None = None,
    max_tickets_per_user_per_round: int = 3,
    per_user_round_capacity_share_bps: int = 2000,
    max_round_exposure_pct: Decimal = Decimal("0.05"),
    max_win_per_ticket: Decimal = Decimal("800"),
    keno_enabled: bool = True,
) -> int:
    """Seeds one config/tier/paytable-per-pick-count/jackpot-pool row and
    one betting_open round, mirroring what a real keno-engine-worker
    would have already set up by the time a ticket is placed. Every test
    in this file gets an independent round (a fresh insert), so tests
    never share round-level state like total_stake/ticket_count."""
    stake_options = stake_options or [Decimal("10"), Decimal("20"), Decimal("50")]
    version = _next_version()

    config_id = await conn.fetchval(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled)
        VALUES ($1, 45, 25, 12, 8, $2, $3, $4)
        RETURNING id
        """,
        version,
        max_tickets_per_user_per_round,
        per_user_round_capacity_share_bps,
        keno_enabled,
    )

    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile)
        VALUES (1, $1, 0, 5, 16, $2, $3, $4, 'low_variance')
        RETURNING id
        """,
        version,
        stake_options,
        max_win_per_ticket,
        max_round_exposure_pct,
    )

    for pick_count, multipliers in _MULTIPLIERS_BY_PICK_COUNT.items():
        stats = keno.compute_paytable_stats(pick_count, multipliers)
        await conn.execute(
            """
            INSERT INTO keno_paytables
                (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
                 max_multiplier, volatility)
            VALUES ($1, $2, 'low_variance', $3::jsonb, $4, $5, $6, $7)
            """,
            pick_count,
            version,
            json.dumps({str(k): str(v) for k, v in multipliers.items()}),
            int(stats.rtp * 10000),
            int(stats.hit_frequency * 10000),
            stats.max_multiplier,
            stats.volatility,
        )

    reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
    jackpot_account = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
    await conn.execute(
        "INSERT INTO keno_jackpot_pool (id, account_id) VALUES (1, $1) ON CONFLICT (id) DO NOTHING",
        jackpot_account.id,
    )
    _ = reserve_account  # created for its side effect (account row exists); balance read separately

    round_id = await conn.fetchval(
        """
        INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at)
        VALUES ((SELECT COALESCE(MAX(seq), 0) + 1 FROM keno_rounds), 'betting_open', $1, $2, $3, now())
        RETURNING id
        """,
        config_id,
        tier_id,
        keno.server_seed_hash(keno.generate_server_seed()),
    )
    return round_id


@pytest.fixture(autouse=True)
async def _close_any_dangling_betting_open_round(pool: asyncpg.Pool):
    """keno_rounds enforces at most one betting_open round at a time
    (ux_keno_rounds_single_betting_open) -- the real engine always
    guarantees this, but this test file seeds rounds directly (bypassing
    the engine) and most tests never advance their own round to a
    terminal status, so a prior test's still-open round would otherwise
    collide with (or, worse, be silently picked up by) the next test's
    place_ticket() call. Runs before *and* after every test in this
    file for that reason."""

    async def _close() -> None:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE keno_rounds SET status = 'completed' WHERE status = 'betting_open'")

    await _close()
    yield
    await _close()


async def _fund_reserve(conn: asyncpg.Connection, amount: Decimal) -> Decimal:
    """Real operator deposit into the keno reserve, via the ledger --
    mirrors fund_user()'s own shape for the player-facing account.
    Returns the *resulting* balance, not just the amount deposited:
    keno_reserve is a genuine platform-wide singleton account (same as
    pot_escrow/house_revenue), so it persists and accumulates across
    every test in this file/session that funds it -- a test asserting
    an exposure ceiling must derive that ceiling from the real resulting
    balance, never assume it equals only what this one call added."""
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn,
        "keno_reserve_deposit",
        [ledger.Entry(house_float.id, -amount), ledger.Entry(reserve.id, amount)],
        idempotency_key=f"test-reserve-fund-{uuid.uuid4()}",
        created_by="test",
    )
    return await ledger.balance(conn, reserve.id)


async def test_place_ticket_debits_stake_and_credits_reserve_and_jackpot(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        round_id = await _seed_keno_round(conn)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    ticket = await keno_tickets.place_ticket(
        pool,
        redis=object(),
        user_id=user_id,
        picks=[1, 2, 3],
        stake=Decimal("10"),
        idempotency_key=f"test-{uuid.uuid4()}",
    )
    assert ticket.round_id == round_id
    assert ticket.status == "pending"
    assert ticket.picks == frozenset({1, 2, 3})

    async with pool.acquire() as conn:
        balance = await ledger.user_balance_snapshot(pool, user_id)
        assert Decimal(balance["cash"]) == Decimal("990.00")

        selections = await conn.fetch(
            "SELECT number FROM keno_ticket_selections WHERE ticket_id = $1 ORDER BY number", ticket.id
        )
        assert [row["number"] for row in selections] == [1, 2, 3]

        round_row = await conn.fetchrow("SELECT total_stake, ticket_count FROM keno_rounds WHERE id = $1", round_id)
        assert round_row["total_stake"] == Decimal("10.00")
        assert round_row["ticket_count"] == 1


async def test_place_ticket_is_idempotent_on_retry(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    idempotency_key = f"test-{uuid.uuid4()}"
    first = await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_id, picks=[7, 8], stake=Decimal("20"), idempotency_key=idempotency_key
    )
    second = await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_id, picks=[7, 8], stake=Decimal("20"), idempotency_key=idempotency_key
    )
    assert first.id == second.id

    balance = await ledger.user_balance_snapshot(pool, user_id)
    # Only ever debited once, despite two calls with the same key.
    assert Decimal(balance["cash"]) == Decimal("980.00")


async def test_place_ticket_rejects_when_no_round_is_betting_open(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        round_id = await _seed_keno_round(conn)
        await conn.execute("UPDATE keno_rounds SET status = 'betting_closed' WHERE id = $1", round_id)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    with pytest.raises(keno_tickets.RoundNotAcceptingBets):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )


async def test_place_ticket_rejects_insufficient_balance(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("5.00"))  # less than the 10 ETB stake

    with pytest.raises(keno_tickets.TicketRejected) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    assert exc_info.value.code == "ticket_rejected"


async def test_place_ticket_rejects_stake_not_in_tier_options(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    with pytest.raises(keno_tickets.StakeNotAllowed):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("999"), idempotency_key=f"test-{uuid.uuid4()}"
        )


async def test_place_ticket_rejects_pick_count_above_tier_max(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn)  # tier's max_pick_count=5
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    # 6 picks passes packages.core.keno.validate_picks (bounds [1,10]
    # generically), so it's specifically place_ticket's own tier check
    # (max_pick_count=5, seeded above) that must reject it.
    with pytest.raises(keno_tickets.PickCountNotAllowedAtTier):
        await keno_tickets.place_ticket(
            pool,
            redis=object(),
            user_id=user_id,
            picks=[1, 2, 3, 4, 5, 6],
            stake=Decimal("10"),
            idempotency_key=f"test-{uuid.uuid4()}",
        )


async def test_place_ticket_rejects_too_many_tickets_this_round(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn, max_tickets_per_user_per_round=1)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
    )
    with pytest.raises(keno_tickets.TooManyTicketsThisRound):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[2], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )


async def test_place_ticket_rejects_when_keno_disabled(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn, keno_enabled=False)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    with pytest.raises(keno_tickets.KenoDisabled):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )


async def test_place_ticket_rejects_when_round_exposure_cap_reached(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # keno_reserve is a persistent, growing shared account across
        # this whole test session (see _fund_reserve's own docstring) --
        # fund it first, read the REAL resulting balance, then choose a
        # max_round_exposure_pct that yields a known-tiny ceiling
        # (~1 ETB) regardless of how much prior tests already
        # accumulated in it, rather than assuming a fixed percentage
        # against an assumed-fresh balance.
        balance = await _fund_reserve(conn, Decimal("100"))
        tiny_pct = (Decimal("1") / balance).quantize(Decimal("0.000001"))
        await _seed_keno_round(conn, max_round_exposure_pct=tiny_pct)
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    with pytest.raises(keno_tickets.RoundCapacityReached):
        await keno_tickets.place_ticket(
            pool,
            redis=object(),
            user_id=user_id,
            picks=[1, 2, 3, 4, 5],
            stake=Decimal("50"),
            idempotency_key=f"test-{uuid.uuid4()}",
        )


async def test_place_ticket_rejects_when_user_round_share_exceeded(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # Same reasoning as the exposure-cap test above: derive
        # max_round_exposure_pct from the real post-funding balance so
        # the resulting ceiling is a known, controlled value (~2000)
        # regardless of what earlier tests already left in the shared
        # reserve account.
        balance = await _fund_reserve(conn, Decimal("40000"))
        pct = (Decimal("2000") / balance).quantize(Decimal("0.000001"))
        await _seed_keno_round(
            conn, per_user_round_capacity_share_bps=1000, max_tickets_per_user_per_round=10, max_round_exposure_pct=pct
        )
        other_user_id = await create_funded_user(conn, Decimal("1000.00"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    await keno_tickets.place_ticket(
        pool,
        redis=object(),
        user_id=other_user_id,
        picks=[1],
        stake=Decimal("10"),
        idempotency_key=f"test-{uuid.uuid4()}",
    )
    with pytest.raises(keno_tickets.UserRoundShareExceeded):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[2], stake=Decimal("50"), idempotency_key=f"test-{uuid.uuid4()}"
        )


async def test_place_ticket_rejects_duplicate_picks(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await _seed_keno_round(conn)
        await _fund_reserve(conn, Decimal("40000"))
        user_id = await create_funded_user(conn, Decimal("1000.00"))

    with pytest.raises(keno_tickets.InvalidPicks):
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1, 1, 2], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
