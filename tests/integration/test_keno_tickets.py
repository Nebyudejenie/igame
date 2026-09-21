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

from packages.core import keno, keno_exposure, keno_tickets, ledger, responsible_gaming
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


def _min_stake_to_exceed_exposure(
    ceiling: Decimal, *, pick_count: int, multipliers: dict[int, Decimal], safety_factor: Decimal = Decimal("1.5")
) -> Decimal:
    """Exact, scale-independent minimum stake whose own
    estimate_percentile_exposure() would exceed `ceiling` -- computed
    from the *real* production formula (expected + Z*sqrt(variance)),
    not a hand-tuned constant.

    Both expected_payout and payout_variance scale with stake (linear,
    quadratic respectively -- keno_exposure.compute_ticket_risk_
    contribution()'s own docstring), so per-unit-stake exposure is a
    single fixed number for a given (pick_count, multipliers) pair;
    solving stake*(unit_expected + Z*sqrt(unit_variance)) >= ceiling for
    stake gives an exact answer, not an approximation.

    Exists because two tests in this file (round-exposure-cap and
    per-user-share) used to hand-pick a max_round_exposure_pct meant to
    yield a small, known-magnitude ceiling regardless of the shared,
    ever-growing keno_reserve balance (see _fund_reserve's own
    docstring) -- but keno_risk_tiers.max_round_exposure_pct is
    numeric(5,4) (max 4 decimal places), and both tests' own pct
    derivation needed *more* precision than that once this session's own
    unusually heavy real-worker/e2e testing pushed the shared reserve
    balance past ~20,000,000 ETB, silently rounding pct up to the
    column's minimum representable value (0.0001) and producing a
    ceiling roughly 1,500-3,000x larger than either test's own hardcoded
    stake was ever chosen to clear. Both now pick a safely-representable,
    *fixed* pct instead and derive the stake needed from the real
    resulting ceiling via this function, so neither is fragile to
    whatever the shared reserve happens to hold by the time it runs.
    """
    unit = keno_exposure.compute_ticket_risk_contribution(pick_count, multipliers, stake=Decimal("1"))
    per_unit_exposure = unit.expected_payout + keno_exposure.Z_SCORE_99_9 * keno_exposure._decimal_sqrt(unit.payout_variance)
    return ((ceiling / per_unit_exposure) * safety_factor).quantize(Decimal("0.01"))


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

    with pytest.raises(keno_tickets.InsufficientBalance) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    # A real pre-existing bug this session's own keno_autoplay test
    # caught: raising the bare TicketRejected base class with a message
    # only ever set __str__, never .code -- every caller reading
    # exc.code (services/gateway/app.py's own HTTPException(detail=
    # exc.code), keno_autoplay.py's own stop_reason) got the generic
    # "ticket_rejected" fallback instead of this specific, more useful
    # one. This assertion used to lock the bug in as "expected."
    assert exc_info.value.code == "insufficient_balance"


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
        # fund it first, read the REAL resulting balance, then pick a
        # *fixed*, safely-representable max_round_exposure_pct (the
        # column is numeric(5,4) -- 0.0001 is its own minimum nonzero
        # value, deliberately used at exactly that floor rather than
        # something derived that could silently round up to it) and
        # derive the stake needed to exceed the real resulting ceiling
        # via _min_stake_to_exceed_exposure()'s own exact math, rather
        # than a hand-picked constant that only worked while the shared
        # reserve stayed small.
        balance = await _fund_reserve(conn, Decimal("100"))
        pct = Decimal("0.0001")
        ceiling = (balance * pct).quantize(Decimal("0.01"))
        big_stake = _min_stake_to_exceed_exposure(ceiling, pick_count=5, multipliers=_MULTIPLIERS_BY_PICK_COUNT[5])
        await _seed_keno_round(conn, max_round_exposure_pct=pct, stake_options=[Decimal("10"), big_stake])
        user_id = await create_funded_user(conn, big_stake * 2)

    with pytest.raises(keno_tickets.RoundCapacityReached):
        await keno_tickets.place_ticket(
            pool,
            redis=object(),
            user_id=user_id,
            picks=[1, 2, 3, 4, 5],
            stake=big_stake,
            idempotency_key=f"test-{uuid.uuid4()}",
        )


async def test_place_ticket_rejects_when_user_round_share_exceeded(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # Same reasoning as _min_stake_to_exceed_exposure()'s own
        # docstring: a fixed, safely-representable max_round_exposure_pct
        # (numeric(5,4) can't go below 0.0001) rather than one derived to
        # hit an absolute ceiling target that used to work only while the
        # shared reserve stayed under ~20,000,000 ETB. The share
        # threshold is 10% of the real resulting ceiling; the stake
        # needed to exceed *that* (not the ceiling itself) is derived the
        # same exact way, so this remains correct regardless of how large
        # the shared reserve grows in the future.
        balance = await _fund_reserve(conn, Decimal("40000"))
        pct = Decimal("0.0100")
        ceiling = (balance * pct).quantize(Decimal("0.01"))
        share_threshold = ceiling * Decimal("0.10")
        big_stake = _min_stake_to_exceed_exposure(share_threshold, pick_count=1, multipliers=_MULTIPLIERS_BY_PICK_COUNT[1])
        await _seed_keno_round(
            conn, per_user_round_capacity_share_bps=1000, max_tickets_per_user_per_round=10,
            max_round_exposure_pct=pct, stake_options=[Decimal("10"), big_stake],
        )
        other_user_id = await create_funded_user(conn, Decimal("1000.00"))
        user_id = await create_funded_user(conn, big_stake * 2)

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
            pool, redis=object(), user_id=user_id, picks=[2], stake=big_stake, idempotency_key=f"test-{uuid.uuid4()}"
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


# --- responsible gaming (spec 3.3's own "mandatory" per-user daily/loss
# limit) -- a spec-compliance audit (2026-09-21) found Keno never checked
# packages/core/responsible_gaming.py at all, unlike Bingo's own
# round_engine.py::join(). ------------------------------------------------


async def test_place_ticket_rejects_a_self_excluded_user(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    await _seed_keno_round(conn)
    await _fund_reserve(conn, Decimal("40000"))
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.self_exclude(pool, user_id)  # spec's own 180-day minimum, via the default

    with pytest.raises(keno_tickets.ResponsibleGamingBlock) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    assert exc_info.value.code == "self_excluded"


async def test_place_ticket_rejects_a_user_cooling_off(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    await _seed_keno_round(conn)
    await _fund_reserve(conn, Decimal("40000"))
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)

    with pytest.raises(keno_tickets.ResponsibleGamingBlock) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    assert exc_info.value.code == "cooling_off"


async def test_place_ticket_rejects_a_stake_that_would_exceed_the_daily_loss_cap(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    await _fund_reserve(conn, Decimal("40000"))
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("5.00"))

    # Stake alone (10) already exceeds the 5.00 cap -- no prior loss
    # needed to trigger this, matching check_stake_allowed()'s own
    # "net_loss + stake > loss_cap" rule.
    with pytest.raises(keno_tickets.ResponsibleGamingBlock) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    assert exc_info.value.code == "loss_limit_reached"


async def test_daily_loss_cap_is_combined_across_bingo_and_keno_not_tracked_per_game(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """The operator's own design decision (2026-09-21, DECISIONS.md): one
    daily loss cap across the whole platform, not a separate one per
    game -- responsible_gaming.today_net_loss() sums both games' own
    stake/payout ledger-transaction kinds together. Proven here by
    posting a real Bingo-style loss directly against the ledger (no
    Bingo round machinery needed, just the same 'stake'/'payout' kinds
    round_engine.py itself posts) and confirming it alone is enough to
    block a Keno ticket that would push the *combined* total past the
    cap, even though this user has never played a single hand of Keno
    before this call."""
    await _seed_keno_round(conn)
    await _fund_reserve(conn, Decimal("40000"))

    # Direction 1: a real Bingo-style loss alone is enough to block a
    # Keno ticket, even though this user's own Keno activity today is
    # zero. 55 (Bingo) + 10 (the attempted Keno stake, a valid option
    # for _seed_keno_round's own default tier) = 65, over the 60 cap.
    user_a = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_a, Decimal("60.00"))
    cash_a = await ledger.get_or_create_account(conn, user_a, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "stake", [ledger.Entry(cash_a.id, Decimal("-55")), ledger.Entry(pot.id, Decimal("55"))],
        idempotency_key=f"test-bingo-stake-{uuid.uuid4()}", created_by="test",
    )
    assert await responsible_gaming.today_net_loss(conn, user_a) == Decimal("55.00")

    with pytest.raises(keno_tickets.ResponsibleGamingBlock) as exc_info:
        await keno_tickets.place_ticket(
            pool, redis=object(), user_id=user_a, picks=[1], stake=Decimal("10"), idempotency_key=f"test-{uuid.uuid4()}"
        )
    assert exc_info.value.code == "loss_limit_reached"

    # Direction 2 (a fresh user, so the arithmetic isn't constrained by
    # direction 1's own numbers): a real Keno loss must be visible to
    # Bingo's own pre-existing check too, not just the other way around.
    # A genuinely successful 20 ETB Keno ticket first (0 + 20 = 20, well
    # under the 60 cap), then confirming a hypothetical 45 ETB Bingo
    # stake would be blocked by the combined total (20 + 45 = 65 > 60).
    user_b = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_b, Decimal("60.00"))
    keno_ticket = await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_b, picks=[1], stake=Decimal("20"), idempotency_key=f"test-{uuid.uuid4()}"
    )
    assert keno_ticket.status == "pending"
    assert await responsible_gaming.today_net_loss(conn, user_b) == Decimal("20.00")
    block = await responsible_gaming.check_stake_allowed(conn, user_b, Decimal("45"))
    assert block.blocked and block.reason == "loss_limit_reached"
