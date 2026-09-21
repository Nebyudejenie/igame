"""Real-Postgres integration tests for packages/core/keno_autoplay.py --
session lifecycle (start/stop/query), config validation, and the two
round-engine integration points (place_for_active_sessions,
record_settlement) exercised directly, independent of a live
KenoRoundEngine (see test_keno_round_engine.py for the full real-worker
proof)."""

from __future__ import annotations

import itertools
import json
import random
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno, keno_autoplay, ledger
from tests.integration.conftest import create_funded_user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_autoplay_sessions(pool: asyncpg.Pool):
    """place_for_active_sessions() correctly operates on every active
    session platform-wide, not scoped to one test's own users -- a
    session an earlier test in this file left 'active' would otherwise
    leak into a later test's own _seed_keno_round()/place_for_active_
    sessions() call and get an unexpected extra ticket placed, exactly
    the kind of cross-test bleed the shared dev database already
    requires this discipline for elsewhere (see test_keno_round_engine
    .py's own _clean_keno_lock_and_rounds)."""

    async def _clean() -> None:
        await pool.execute(
            "UPDATE keno_autoplay_sessions SET status = 'stopped', stop_reason = 'test_cleanup', updated_at = now() "
            "WHERE status = 'active'"
        )

    await _clean()
    yield
    await _clean()


_version_counter = itertools.count(random.randint(4 * 10**6, 5 * 10**6))


def _next_version() -> int:
    return next(_version_counter)


_MULTIPLIERS = {1: Decimal("3.40")}


async def _seed_keno_round(conn: asyncpg.Connection, *, max_round_exposure_pct: Decimal = Decimal("0.90")) -> int:
    version = _next_version()
    config_id = await conn.fetchval(
        """
        INSERT INTO keno_configs
            (version, round_cycle_seconds, betting_seconds, draw_seconds, result_seconds,
             max_tickets_per_user_per_round, per_user_round_capacity_share_bps, keno_enabled)
        VALUES ($1, 45, 25, 12, 8, 5, 5000, true)
        RETURNING id
        """,
        version,
    )
    tier_id = await conn.fetchval(
        """
        INSERT INTO keno_risk_tiers
            (tier_number, version, min_reserve, max_pick_count, max_top_multiplier,
             stake_options, max_win_per_ticket, max_round_exposure_pct, paytable_profile)
        VALUES (1, $1, 0, 5, 16, ARRAY[10.00,20.00,50.00]::numeric(18,2)[], 800, $2, 'low_variance')
        RETURNING id
        """,
        version, max_round_exposure_pct,
    )
    stats = keno.compute_paytable_stats(1, _MULTIPLIERS)
    await conn.execute(
        """
        INSERT INTO keno_paytables (pick_count, version, profile, multipliers, computed_rtp_bps, hit_frequency_bps,
                                     max_multiplier, volatility)
        VALUES (1, $1, 'low_variance', $2::jsonb, $3, $4, $5, $6)
        """,
        version,
        json.dumps({str(k): str(v) for k, v in _MULTIPLIERS.items()}),
        int(stats.rtp * 10000),
        int(stats.hit_frequency * 10000),
        stats.max_multiplier,
        stats.volatility,
    )
    await conn.execute("UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed','failed','voided')")
    reserve = await ledger.get_or_create_account(conn, None, "keno_reserve")
    house_float = await ledger.get_or_create_account(conn, None, "house_float")
    await ledger.post(
        conn, "keno_reserve_deposit",
        [ledger.Entry(house_float.id, Decimal("-100000")), ledger.Entry(reserve.id, Decimal("100000"))],
        idempotency_key=f"test-autoplay-reserve-{uuid.uuid4()}", created_by="test",
    )
    round_id = await conn.fetchval(
        """
        INSERT INTO keno_rounds (seq, status, config_id, tier_id, server_seed_hash, betting_opened_at)
        VALUES ((SELECT COALESCE(MAX(seq),0)+1 FROM keno_rounds), 'betting_open', $1, $2, $3, now())
        RETURNING id
        """,
        config_id, tier_id, keno.server_seed_hash(keno.generate_server_seed()),
    )
    return round_id


# --- start_session validation ---------------------------------------------


async def test_start_session_rejects_no_stop_condition_at_all(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    with pytest.raises(keno_autoplay.InvalidAutoplayConfig):
        await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"))


async def test_start_session_rejects_rounds_total_over_the_ceiling(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    with pytest.raises(keno_autoplay.InvalidAutoplayConfig):
        await keno_autoplay.start_session(
            pool, user_id=user_id, picks=[1], stake=Decimal("10"),
            rounds_total=keno_autoplay.MAX_ROUNDS_TOTAL + 1,
        )


async def test_start_session_accepts_a_valid_multi_race_config(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(
        pool, user_id=user_id, picks=[1, 2, 3], stake=Decimal("10"), rounds_total=5
    )
    assert session.rounds_total == 5
    assert session.rounds_placed == 0
    assert session.status == "active"
    assert session.picks == frozenset({1, 2, 3})


async def test_start_session_rejects_a_second_active_session_for_the_same_user(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=5)
    with pytest.raises(keno_autoplay.AutoplaySessionAlreadyActive):
        await keno_autoplay.start_session(pool, user_id=user_id, picks=[2], stake=Decimal("20"), rounds_total=3)


async def test_stop_session_is_an_idempotent_no_op_when_nothing_is_active(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    result = await keno_autoplay.stop_session(pool, user_id=user_id)
    assert result is None


async def test_stop_session_stops_a_real_active_session(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=5)
    stopped = await keno_autoplay.stop_session(pool, user_id=user_id)
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "manual"
    assert await keno_autoplay.active_session(pool, user_id=user_id) is None
    # A second user starting their own session right after is unaffected
    # by the first user's now-stopped one -- the uniqueness constraint is
    # per-user, not global.
    other_user_id = await create_funded_user(conn, Decimal("1000.00"))
    await keno_autoplay.start_session(pool, user_id=other_user_id, picks=[1], stake=Decimal("10"), rounds_total=5)


# --- place_for_active_sessions ---------------------------------------------


async def test_place_for_active_sessions_places_one_real_ticket_per_session(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    round_id = await _seed_keno_round(conn)
    user_a = await create_funded_user(conn, Decimal("1000.00"))
    user_b = await create_funded_user(conn, Decimal("1000.00"))
    session_a = await keno_autoplay.start_session(pool, user_id=user_a, picks=[1], stake=Decimal("10"), rounds_total=3)
    session_b = await keno_autoplay.start_session(pool, user_id=user_b, picks=[2], stake=Decimal("20"), rounds_total=3)

    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)

    tickets = await pool.fetch(
        "SELECT user_id, stake, autoplay_session_id FROM keno_tickets WHERE round_id = $1 ORDER BY user_id", round_id
    )
    assert len(tickets) == 2
    by_user = {t["user_id"]: t for t in tickets}
    assert by_user[user_a]["stake"] == Decimal("10.00")
    assert by_user[user_a]["autoplay_session_id"] == session_a.id
    assert by_user[user_b]["stake"] == Decimal("20.00")
    assert by_user[user_b]["autoplay_session_id"] == session_b.id

    refreshed_a = await keno_autoplay.active_session(pool, user_id=user_a)
    assert refreshed_a is not None
    assert refreshed_a.rounds_placed == 1
    assert refreshed_a.last_round_id == round_id


async def test_place_for_active_sessions_marks_exhausted_once_rounds_total_is_reached(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=2)

    round_1 = await _seed_keno_round(conn)
    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_1)
    assert (await keno_autoplay.active_session(pool, user_id=user_id)).status == "active"  # type: ignore[union-attr]

    round_2 = await _seed_keno_round(conn)
    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_2)
    assert await keno_autoplay.active_session(pool, user_id=user_id) is None

    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT status, stop_reason, rounds_placed FROM keno_autoplay_sessions WHERE user_id = $1", user_id
        )
    assert row["status"] == "exhausted"
    assert row["stop_reason"] == "rounds_exhausted"
    assert row["rounds_placed"] == 2

    # A third round must not place a third ticket -- the session is done.
    round_3 = await _seed_keno_round(conn)
    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_3)
    count = await pool.fetchval("SELECT count(*) FROM keno_tickets WHERE round_id = $1", round_3)
    assert count == 0


async def test_place_for_active_sessions_stops_on_a_real_rejection_not_just_logs_it(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """A stake this user can't afford -- place_for_active_sessions must
    stop the session (not retry it forever on every future round) and
    leave the specific rejection reason on the row."""
    round_id = await _seed_keno_round(conn)
    user_id = await create_funded_user(conn, Decimal("5.00"))  # can't afford a 10 ETB stake
    await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=5)

    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)

    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT status, stop_reason FROM keno_autoplay_sessions WHERE user_id = $1", user_id
        )
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "ticket_rejected:insufficient_balance"
    # (a pre-existing bug this test caught: InsufficientFunds used to
    # surface as the generic "ticket_rejected" .code, not this specific
    # one -- see packages/core/keno_tickets.py's own InsufficientBalance)


# --- record_settlement ------------------------------------------------


async def test_record_settlement_stops_the_session_once_stop_on_win_is_reached(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(
        pool, user_id=user_id, picks=[1], stake=Decimal("10"), stop_on_win_amount=Decimal("50")
    )
    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("30"), jackpot_payout=Decimal("0")
    )
    still_active = await keno_autoplay.active_session(pool, user_id=user_id)
    assert still_active is not None
    assert still_active.net_position == Decimal("20.00")  # +30 - 10, under the 50 threshold

    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("40"), jackpot_payout=Decimal("0")
    )
    assert await keno_autoplay.active_session(pool, user_id=user_id) is None
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT status, stop_reason, net_position FROM keno_autoplay_sessions WHERE id = $1", session.id
        )
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "stop_on_win"
    assert row["net_position"] == Decimal("50.00")  # 20 + (40 - 10)


async def test_record_settlement_stops_the_session_once_stop_on_loss_is_reached(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(
        pool, user_id=user_id, picks=[1], stake=Decimal("10"), stop_on_loss_amount=Decimal("25")
    )
    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("0"), jackpot_payout=Decimal("0")
    )
    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("0"), jackpot_payout=Decimal("0")
    )
    # -20 so far, still under the 25 loss threshold.
    assert (await keno_autoplay.active_session(pool, user_id=user_id)) is not None

    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("0"), jackpot_payout=Decimal("0")
    )
    assert await keno_autoplay.active_session(pool, user_id=user_id) is None
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT status, stop_reason, net_position FROM keno_autoplay_sessions WHERE id = $1", session.id
        )
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "stop_on_loss"
    assert row["net_position"] == Decimal("-30.00")


async def test_record_settlement_is_a_no_op_for_a_manually_placed_ticket(pool: asyncpg.Pool) -> None:
    """autoplay_session_id is None for the overwhelming majority of real
    tickets -- must not raise or touch anything."""
    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=None, stake=Decimal("10"), payout=Decimal("0"), jackpot_payout=Decimal("0")
    )


async def test_record_settlement_leaves_an_already_stopped_session_alone(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """A settlement arriving after the session was already stopped some
    other way (manual stop, exhaustion) must not resurrect it or
    overwrite its stop_reason."""
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(
        pool, user_id=user_id, picks=[1], stake=Decimal("10"), stop_on_win_amount=Decimal("5")
    )
    await keno_autoplay.stop_session(pool, user_id=user_id)

    await keno_autoplay.record_settlement(
        pool, autoplay_session_id=session.id, stake=Decimal("10"), payout=Decimal("100"), jackpot_payout=Decimal("0")
    )
    async with pool.acquire() as check_conn:
        row = await check_conn.fetchrow(
            "SELECT status, stop_reason, net_position FROM keno_autoplay_sessions WHERE id = $1", session.id
        )
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "manual"  # not overwritten to stop_on_win
    assert row["net_position"] == Decimal("0.00")  # not updated either -- the UPDATE's own WHERE status='active' skipped it
