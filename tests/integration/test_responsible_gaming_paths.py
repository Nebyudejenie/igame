"""Responsible-gaming audit (2026-09-25): do self-exclusion, cool-off and
the daily loss cap actually hold on every path that takes a stake, not
just the single-ticket / single-card paths the earlier tests cover?

The paths covered here, all against real Postgres:

- Keno autoplay: a limit that starts to apply *mid-session* must stop
  the session before its next ticket, with no money taken.
- Several Keno tickets (and several Bingo cards) in one round: the cap is
  cumulative, and a still-pending ticket's stake already counts.
- Concurrency: simultaneous Keno tickets for one player must never
  jointly exceed the cap.

Two tests pin down current behavior the operator should decide on rather
than engineering changing unilaterally (see their docstrings): autoplay
sessions can be *started* while blocked, and refunded stakes still count
toward today's loss.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest

from packages.core import keno_autoplay, keno_tickets, ledger, responsible_gaming
from services.engine.round_engine import RoundEngine, load_room_config
from tests.integration.conftest import create_funded_user, create_room
from tests.integration.test_keno_autoplay import _seed_keno_round

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_autoplay_sessions(pool: asyncpg.Pool):
    """Same reason as test_keno_autoplay.py's own fixture of this name:
    place_for_active_sessions() walks every active session platform-wide,
    so one left active by another test would get a ticket in ours."""

    async def _clean() -> None:
        await pool.execute(
            "UPDATE keno_autoplay_sessions SET status = 'stopped', stop_reason = 'test_cleanup', updated_at = now() "
            "WHERE status = 'active'"
        )

    await _clean()
    yield
    await _clean()


async def _cash(conn: asyncpg.Connection, user_id: int) -> Decimal:
    account = await ledger.get_or_create_account(conn, user_id, "user_cash")
    return await ledger.balance(conn, account.id)


async def _session_state(conn: asyncpg.Connection, session_id: int) -> tuple[str, str | None, int]:
    row = await conn.fetchrow(
        "SELECT s.status, s.stop_reason, (SELECT count(*) FROM keno_tickets t WHERE t.autoplay_session_id = s.id) AS n "
        "FROM keno_autoplay_sessions s WHERE s.id = $1",
        session_id,
    )
    assert row is not None
    return row["status"], row["stop_reason"], row["n"]


async def _next_round(pool: asyncpg.Pool, conn: asyncpg.Connection) -> int:
    """Opens a fresh betting round (closing the previous one) and runs the
    same autoplay hook the real engine calls from _open_betting()."""
    round_id = await _seed_keno_round(conn)
    await keno_autoplay.place_for_active_sessions(pool, redis=object(), round_id=round_id)  # type: ignore[arg-type]
    return round_id


async def _ticket(pool: asyncpg.Pool, user_id: int, stake: str = "10") -> keno_tickets.PlacedTicket:
    return await keno_tickets.place_ticket(
        pool, redis=object(), user_id=user_id, picks=[1], stake=Decimal(stake), idempotency_key=f"test-{uuid.uuid4()}"
    )


# --- autoplay: a limit that begins mid-session ------------------------------


async def test_autoplay_stops_before_the_next_ticket_once_the_player_self_excludes(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)

    await _next_round(pool, conn)
    assert (await _session_state(conn, session.id))[2] == 1
    balance_after_first = await _cash(conn, user_id)

    await responsible_gaming.self_exclude(pool, user_id)
    await _next_round(pool, conn)

    status, reason, tickets = await _session_state(conn, session.id)
    assert (status, reason, tickets) == ("stopped", "ticket_rejected:self_excluded", 1)
    assert await _cash(conn, user_id) == balance_after_first


async def test_autoplay_stops_on_cool_off_and_does_not_resume_when_it_expires(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)

    await responsible_gaming.cool_off(conn, user_id, duration_hours=24)
    await _next_round(pool, conn)
    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:cooling_off", 0)

    # The break ends. The session stays stopped: a player coming back from
    # a cool-off has to choose to play again, autoplay doesn't pick up
    # where it left off on its own.
    await conn.execute(
        "UPDATE responsible_gaming_limits SET cooloff_until = now() - interval '1 minute' WHERE user_id = $1", user_id
    )
    assert not (await responsible_gaming.check_play_allowed(conn, user_id)).blocked
    await _next_round(pool, conn)
    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:cooling_off", 0)
    assert await _cash(conn, user_id) == Decimal("1000.00")


async def test_autoplay_stops_when_the_next_ticket_would_breach_the_daily_loss_cap(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """Also proves pending tickets count: none of these tickets is ever
    settled, so the loss the cap sees is purely stakes already taken."""
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("25.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)

    for _ in range(3):  # 10, 20, then 30 > 25 is refused
        await _next_round(pool, conn)

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:loss_limit_reached", 2)
    assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("20.00")
    assert await _cash(conn, user_id) == Decimal("980.00")


async def test_lowering_the_loss_cap_mid_session_applies_to_the_very_next_autoplay_ticket(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)
    await _next_round(pool, conn)
    assert (await _session_state(conn, session.id))[2] == 1

    assert await responsible_gaming.set_loss_limit(conn, user_id, Decimal("15.00")) is True  # a decrease: immediate
    await _next_round(pool, conn)

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:loss_limit_reached", 1)


async def test_raising_the_loss_cap_mid_session_does_not_let_autoplay_continue_before_the_24h_delay(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("15.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)
    await _next_round(pool, conn)

    assert await responsible_gaming.set_loss_limit(conn, user_id, Decimal("1000.00")) is False  # an increase: deferred
    await _next_round(pool, conn)

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:loss_limit_reached", 1)


async def test_autoplay_counts_todays_bingo_loss_toward_the_shared_cap(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("60.00"))
    cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
    pot = await ledger.get_or_create_account(conn, None, "pot_escrow")
    await ledger.post(
        conn, "stake", [ledger.Entry(cash.id, Decimal("-55")), ledger.Entry(pot.id, Decimal("55"))],
        idempotency_key=f"test-bingo-stake-{uuid.uuid4()}", created_by="test",
    )
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)

    await _next_round(pool, conn)

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:loss_limit_reached", 0)


async def test_autoplay_and_manual_tickets_draw_down_one_shared_cap(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("25.00"))
    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)

    await _next_round(pool, conn)  # autoplay: 10
    await _ticket(pool, user_id)  # manual, same round: 20
    await _next_round(pool, conn)  # autoplay: 30 > 25, refused

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:loss_limit_reached", 1)
    assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("20.00")


async def test_an_autoplay_session_started_while_self_excluded_never_places_a_ticket(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    """Current behavior, for the operator to decide on: start_session()
    does not check responsible-gaming status, so a self-excluded player
    can still *create* a session. What protects them is that every ticket
    goes through place_ticket()'s own check, so the session is stopped at
    the first round with nothing staked. Rejecting at start would be a
    clearer message to the player; it would not change what they can lose."""
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.self_exclude(pool, user_id)

    session = await keno_autoplay.start_session(pool, user_id=user_id, picks=[1], stake=Decimal("10"), rounds_total=10)
    await _next_round(pool, conn)

    assert await _session_state(conn, session.id) == ("stopped", "ticket_rejected:self_excluded", 0)
    assert await _cash(conn, user_id) == Decimal("1000.00")


# --- several tickets / cards in one round -----------------------------------


async def test_several_keno_tickets_in_one_round_are_capped_cumulatively(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    round_id = await _seed_keno_round(conn)  # allows 5 tickets per user per round
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("35.00"))

    for _ in range(3):
        assert (await _ticket(pool, user_id)).round_id == round_id  # 10, 20, 30
    with pytest.raises(keno_tickets.ResponsibleGamingBlock) as exc_info:
        await _ticket(pool, user_id)  # 40 > 35, while the per-round ticket limit (5) still has room
    assert exc_info.value.code == "loss_limit_reached"
    assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("30.00")


async def test_several_bingo_cards_in_one_round_are_capped_cumulatively(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    room = await load_room_config(
        pool, await create_room(conn, stake=Decimal("20.00"), max_cards_per_player=4, lobby_seconds=60)
    )
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("50.00"))

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        assert (await engine.join(user_id, 1)).ok  # 20
        assert (await engine.join(user_id, 2)).ok  # 40
        third = await engine.join(user_id, 3)  # 60 > 50, while max_cards_per_player (4) still has room
        assert (third.ok, third.reason) == (False, "loss_limit_reached")
        assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("40.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


# --- concurrency -------------------------------------------------------------


async def test_concurrent_keno_tickets_for_one_player_cannot_jointly_exceed_the_cap(
    pool: asyncpg.Pool, conn: asyncpg.Connection
) -> None:
    await _seed_keno_round(conn)
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("25.00"))

    results = await asyncio.gather(*(_ticket(pool, user_id) for _ in range(5)), return_exceptions=True)

    placed = [r for r in results if isinstance(r, keno_tickets.PlacedTicket)]
    refused = [r for r in results if isinstance(r, keno_tickets.ResponsibleGamingBlock)]
    assert (len(placed), len(refused)) == (2, 3), results
    assert {r.code for r in refused} == {"loss_limit_reached"}
    assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("20.00")


# --- refunds: current behavior, flagged for a decision -----------------------


async def test_refunded_stakes_still_count_toward_todays_loss(
    pool: asyncpg.Pool, redis, conn: asyncpg.Connection, card_pool
) -> None:
    """Current, documented behavior (responsible_gaming.py's own comment
    on _STAKE_KINDS): refunds are not subtracted from today_net_loss().
    A player who takes a Bingo card and drops it gets every birr back but
    is still charged against their cap, so enough drops lock them out with
    no real loss. The same applies to a voided Keno round's keno_refund.
    This errs toward protecting the player, never toward letting them lose
    more than their cap; whether it should instead net refunds out is the
    operator's call. This test exists so the behavior can't change silently."""
    room = await load_room_config(
        pool, await create_room(conn, stake=Decimal("20.00"), max_cards_per_player=4, lobby_seconds=60)
    )
    user_id = await create_funded_user(conn, Decimal("1000.00"))
    await responsible_gaming.set_loss_limit(conn, user_id, Decimal("50.00"))

    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        for card_no in (1, 2):
            assert (await engine.join(user_id, card_no)).ok
            assert (await engine.drop_card(user_id, card_no)).ok
        assert await _cash(conn, user_id) == Decimal("1000.00")  # every stake came back
        assert await responsible_gaming.today_net_loss(conn, user_id) == Decimal("40.00")

        third = await engine.join(user_id, 3)
        assert (third.ok, third.reason) == (False, "loss_limit_reached")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)
