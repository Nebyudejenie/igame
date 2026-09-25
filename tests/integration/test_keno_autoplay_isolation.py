"""One autoplay session's failure must never skip the sessions queued
behind it or take down the Keno round loop (operator decision,
2026-09-25). Covers both per-player loops the engine runs for autoplay:
placement at betting open (keno_autoplay.place_for_active_sessions) and
the post-settlement update of each session's net position
(keno_round_engine's to_publish loop -> record_settlement_safely)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno_autoplay, keno_tickets
from services.engine.keno_lock import LOCK_KEY as KENO_LOCK_KEY
from services.engine.keno_round_engine import KenoRoundEngine
from tests.integration.conftest import create_funded_user
from tests.integration.test_keno_autoplay import _seed_keno_round
from tests.integration.test_keno_round_engine import _seed_fast_config_and_tier, _wait_until

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean(pool: asyncpg.Pool, redis):
    async def clean() -> None:
        await redis.delete(KENO_LOCK_KEY)
        await pool.execute("UPDATE keno_rounds SET status = 'completed' WHERE status NOT IN ('completed','failed','voided')")
        await pool.execute(
            "UPDATE keno_autoplay_sessions SET status = 'stopped', stop_reason = 'test_cleanup', updated_at = now() "
            "WHERE status = 'active'"
        )

    await clean()
    yield
    await clean()


async def _three_sessions(pool: asyncpg.Pool, conn: asyncpg.Connection) -> list[keno_autoplay.AutoplaySession]:
    sessions = []
    for _ in range(3):
        user_id = await create_funded_user(conn, Decimal("1000.00"))
        sessions.append(
            await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)
        )
    return sessions  # ascending id: the first one is walked first


async def _state(conn: asyncpg.Connection, session_id: int) -> tuple[str, str | None, int]:
    row = await conn.fetchrow(
        "SELECT s.status, s.stop_reason, (SELECT count(*) FROM keno_tickets t WHERE t.autoplay_session_id = s.id) AS n "
        "FROM keno_autoplay_sessions s WHERE s.id = $1",
        session_id,
    )
    return row["status"], row["stop_reason"], row["n"]


@pytest.mark.parametrize("failure", [RuntimeError("simulated bug"), asyncpg.exceptions.DeadlockDetectedError("deadlock detected")])
async def test_one_sessions_unexpected_placement_failure_does_not_skip_the_sessions_behind_it(
    pool: asyncpg.Pool, conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    round_id = await _seed_keno_round(conn)
    first, second, third = await _three_sessions(pool, conn)
    real_place = keno_tickets.place_ticket

    async def fail_for_the_first_player(*args, **kwargs):
        if kwargs["user_id"] == first.user_id:
            raise failure
        return await real_place(*args, **kwargs)

    monkeypatch.setattr(keno_tickets, "place_ticket", fail_for_the_first_player)

    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)  # type: ignore[arg-type]

    assert await _state(conn, first.id) == ("stopped", "placement_error", 0)
    assert await _state(conn, second.id) == ("active", None, 1)
    assert await _state(conn, third.id) == ("active", None, 1)


async def test_even_failing_to_stop_the_failed_session_does_not_skip_the_rest(
    pool: asyncpg.Pool, conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the database itself is what failed, stopping the session can
    fail too. The loop still moves on; the session stays active and is
    retried next round."""
    round_id = await _seed_keno_round(conn)
    first, second, third = await _three_sessions(pool, conn)
    real_place = keno_tickets.place_ticket
    real_stop = keno_autoplay._stop_internal

    async def fail_for_the_first_player(*args, **kwargs):
        if kwargs["user_id"] == first.user_id:
            raise ConnectionResetError("simulated dropped connection")
        return await real_place(*args, **kwargs)

    async def stop_fails_for_the_first_session(pool_, session_id, **kwargs):
        if session_id == first.id:
            raise ConnectionResetError("simulated dropped connection")
        return await real_stop(pool_, session_id, **kwargs)

    monkeypatch.setattr(keno_tickets, "place_ticket", fail_for_the_first_player)
    monkeypatch.setattr(keno_autoplay, "_stop_internal", stop_fails_for_the_first_session)

    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)  # type: ignore[arg-type]

    assert await _state(conn, first.id) == ("active", None, 0)
    assert await _state(conn, second.id) == ("active", None, 1)
    assert await _state(conn, third.id) == ("active", None, 1)


async def test_a_failed_net_position_update_for_one_player_does_not_skip_the_others(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real engine, end to end: two autoplay sessions, and recording
    the first one's settled result raises. The round must still complete,
    the second session's net position must still reflect every one of its
    settled tickets, and the first session must be stopped (its
    stop-on-win/loss total can no longer be trusted)."""
    await _seed_fast_config_and_tier(conn, betting_seconds=1, draw_seconds=1, result_seconds=1)
    first_user = await create_funded_user(conn, Decimal("1000.00"))
    second_user = await create_funded_user(conn, Decimal("1000.00"))
    first = await keno_autoplay.start_session(pool, user_id=first_user, picks=[7], stake=Decimal("10"), rounds_total=10)
    second = await keno_autoplay.start_session(pool, user_id=second_user, picks=[7], stake=Decimal("10"), rounds_total=10)
    real_record = keno_autoplay.record_settlement

    async def fail_for_the_first_session(pool_, **kwargs):
        if kwargs["autoplay_session_id"] == first.id:
            raise RuntimeError("simulated failure recording the first player's result")
        return await real_record(pool_, **kwargs)

    monkeypatch.setattr(keno_autoplay, "record_settlement", fail_for_the_first_session)

    engine = KenoRoundEngine(pool, redis)
    engine_task = asyncio.create_task(engine.run_forever())
    try:
        async def first_autoplay_round_completed() -> bool:
            return bool(await pool.fetchval(
                "SELECT count(*) FROM keno_tickets t JOIN keno_rounds r ON r.id = t.round_id "
                "WHERE t.autoplay_session_id = $1 AND r.status = 'completed'",
                second.id,
            ))

        await _wait_until(first_autoplay_round_completed, timeout=25.0)
    finally:
        engine.stop()
        await asyncio.wait_for(engine_task, timeout=15.0)

    first_row = await conn.fetchrow("SELECT status, stop_reason FROM keno_autoplay_sessions WHERE id = $1", first.id)
    assert (first_row["status"], first_row["stop_reason"]) == ("stopped", "settlement_error")

    expected = await conn.fetchval(
        "SELECT COALESCE(SUM(payout + COALESCE(jackpot_payout, 0) - stake), 0) FROM keno_tickets "
        "WHERE autoplay_session_id = $1 AND status IN ('won', 'lost')",
        second.id,
    )
    assert await conn.fetchval("SELECT net_position FROM keno_autoplay_sessions WHERE id = $1", second.id) == expected
    assert expected != 0  # 1 pick: a loss is -10, a win +24 -- never zero, so a skipped update can't hide
