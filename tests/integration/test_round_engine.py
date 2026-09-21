"""Integration tests for services/engine/round_engine.py -- the state
machine and money-critical paths the spec itself flags as the ones to
review line by line.
"""

import asyncio
import json
import time
from decimal import Decimal

import pytest

from packages.core import bingo, ledger
from services.engine import refunds, round_engine, settlement
from services.engine.round_engine import ClaimResult, RoundEngine, load_room_config
from services.gateway import queries as gateway_queries
from tests.integration.conftest import create_funded_user, create_room, recv_balance_update


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def make_engine(pool, redis, card_pool, room_id) -> RoundEngine:
    room = await load_room_config(pool, room_id)
    return RoundEngine(pool, redis, room, card_pool)


async def test_full_round_35_players_ledger_balances(pool, redis, card_pool, conn):
    # lobby_seconds needs real margin here, unlike this file's other tests:
    # the lobby deadline is fixed the moment the first join starts the
    # round (round_engine.py's own _lobby_deadline_monotonic), not
    # extended by later joins, and this test joins 35 users *sequentially*
    # -- each one several real DB round trips -- rather than concurrently.
    # A flaky "not_joinable" failure a code review pass caught (this ran
    # comfortably inside the old 1-second default in isolation, but failed
    # 3 of 5 runs under real host contention -- other unrelated Docker
    # containers on the same shared 4-core box) confirmed 1 second leaves
    # no real margin for 35 sequential joins once the host is under any
    # load at all.
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2,
        lobby_seconds=20, call_interval_ms=5,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        users = [await create_funded_user(conn) for _ in range(35)]
        for i, user_id in enumerate(users):
            result = await engine.join(user_id, card_no=i + 1)
            assert result.ok, result.reason

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=45)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash, status FROM rounds WHERE room_id = $1 "
            "ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["status"] == "done"
        assert round_row["pot"] == Decimal("700.00")
        assert round_row["derash"] == Decimal("560.00")
        assert round_row["pot"] - round_row["derash"] == Decimal("140.00")

        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert len(winners) >= 1
        # A real draw over 35 real cards can legitimately produce more
        # than one simultaneous winner, not just the single-winner case --
        # settlement.split_derash() rounds each share DOWN to the cent and
        # sends whatever fraction that leaves on the table to the house
        # (tested directly in tests/unit/test_settlement.py, and exercised
        # end to end by test_two_simultaneous_claims_split_derash_evenly
        # below), so summing winners' amounts only equals derash exactly
        # when it divides evenly among however many winners this draw
        # actually produced -- recompute the real expected split rather
        # than assuming a single winner always gets the whole derash.
        expected_shares, _leftover_to_house = settlement.split_derash(
            round_row["derash"], len(winners)
        )
        assert sorted(w["amount"] for w in winners) == sorted(expected_shares)

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_state_sync_derash_reflects_the_live_pot_before_settlement(pool, redis, card_pool, conn):
    # Real bug reported against the game screen's own derash stat:
    # "sometimes 0, sometimes the right value". Root cause: rounds.derash
    # is only ever written at settlement (this file's own test above
    # confirms round_row["derash"] is correct only once status == "done"),
    # so services/gateway/queries.py::build_state_sync() used to read that
    # same column directly for a round still in "lobby"/"running" and
    # always got NULL -> a hardcoded "0.00", even though the real pot was
    # already nonzero. A player live for round_engine.py's own round_start/
    # lobby_tick broadcasts (which compute derash fresh from the pot) saw
    # the correct number; anyone who reconnected, refreshed, or joined
    # mid-round instead got build_state_sync's stale zero -- exactly the
    # intermittent symptom reported. Fixed by computing derash the same
    # live way round_engine.py itself does, not by reading the column.
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=1000, min_players=5, lobby_seconds=20,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        for i, user_id in enumerate((user_a, user_b)):
            result = await engine.join(user_id, card_no=i + 1)
            assert result.ok, result.reason

        # Still in "lobby" (min_players=5, only 2 joined) -- the exact
        # in-progress, not-yet-settled state build_state_sync() must get
        # right, since rounds.derash is NULL for the entire life of a round.
        sync = await gateway_queries.build_state_sync(pool, room_id, user_a)
        assert sync["status"] == "lobby"
        assert sync["pot"] == "40.00"
        expected_derash, _ = settlement.compute_derash(Decimal("40.00"), 1000)
        assert sync["derash"] == str(expected_derash)
        assert sync["derash"] != "0.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_two_simultaneous_claims_split_derash_evenly(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, card_b, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]

        def both_ready() -> bool:
            # Reaching into the engine's own called-numbers set here is
            # deliberate: it's how the test drives two claims to land in
            # the exact same instant without needing to control the
            # (intentionally provably-fair-random) draw order itself.
            called = engine._called  # noqa: SLF001
            return bingo.has_won(grid_a, called, room.win_patterns) and bingo.has_won(
                grid_b, called, room.win_patterns
            )

        await wait_until(both_ready, timeout=10)

        results = await asyncio.gather(engine.claim(user_a, card_a), engine.claim(user_b, card_b))
        assert all(r.ok for r in results), results

        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["pot"] == Decimal("40.00")
        assert round_row["derash"] == Decimal("32.00")

        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1 ORDER BY user_id",
            round_row["id"],
        )
        assert len(winners) == 2
        assert winners[0]["amount"] == winners[1]["amount"] == Decimal("16.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_concurrent_claims_two_players_only_one_holds_a_valid_pattern(
    pool, redis, card_pool, conn
):
    """Launch-readiness audit item: two *different* players claim at the
    same instant, but only one actually holds a winning pattern right
    now. Unlike the genuinely-racy case above (two simultaneous real
    winners, both sharing the _winner_lock critical section), this case
    has no race to resolve at all -- the losing claim's own evaluation
    (bingo.winning_patterns against that player's own card) returns
    False and returns "no_pattern" *before* ever reaching the
    _winner_lock/_pending_winners section (see claim()'s own control
    flow: the `if not valid: ... return` branch is strictly earlier than
    `async with self._winner_lock`). The two claims are provably
    independent, not merely "usually fine under this test's timing" --
    this test locks that architectural guarantee in with a real
    concurrent call, not just a sequential one.

    Directly constructs engine._called (the same white-box style
    test_two_simultaneous_claims_split_derash_evenly above already uses
    to read it) rather than waiting on natural draw progression to
    produce "one card ready, the other not": cards 1 and 2 in this
    deterministic pool turn out to complete their own two-line patterns
    at nearly the same draw count (unsurprising -- it's exactly why the
    *other* test above picks this same pair to get simultaneous
    winners), so that state can take the full round to occur naturally,
    or never occur before the round exhausts and a fresh one starts --
    at which point the original joins are for a round that no longer
    exists. Setting the called set directly makes the target state
    immediate and deterministic instead of racing real background
    per-call timing.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        winner = await create_funded_user(conn)
        loser = await create_funded_user(conn)
        winning_card, losing_card = 1, 2

        assert (await engine.join(winner, winning_card, auto_mark=False)).ok
        assert (await engine.join(loser, losing_card, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        winning_grid = card_pool[winning_card]
        losing_grid = card_pool[losing_card]
        # winning_card's own first two rows, called in full -- a real,
        # valid two-line win by this file's own win-pattern rules.
        engine._called = {winning_grid[0][c] for c in range(5)} | {  # noqa: SLF001
            winning_grid[1][c] for c in range(5)
        }
        assert bingo.has_won(winning_grid, engine._called, room.win_patterns)  # noqa: SLF001
        assert not bingo.has_won(losing_grid, engine._called, room.win_patterns)  # noqa: SLF001

        winner_result, loser_result = await asyncio.gather(
            engine.claim(winner, winning_card), engine.claim(loser, losing_card)
        )
        assert winner_result.ok is True
        assert loser_result == ClaimResult(False, "no_pattern")

        await wait_until(lambda: engine.status == "idle", timeout=5)
        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        winners = await pool.fetch(
            "SELECT user_id FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert [w["user_id"] for w in winners] == [winner]
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_claim_after_the_tie_window_has_already_settled_is_rejected(
    pool, redis, card_pool, conn
):
    """Launch-readiness audit item: claim()'s own final fallback branch
    (`return ClaimResult(False, "round_already_settled")`) is reached
    once a round is still in "settling" status but its tie window (
    WINNER_TIE_WINDOW_SECONDS = 0.05s) has already closed -- a distinct
    rejection reason from both "no_pattern" (premature) and
    "round_not_running" (never started), and previously untested.

    Backdates engine._winner_window_deadline directly (the same
    established white-box style test_two_simultaneous_claims_split_
    derash_evenly above already uses via engine._called) rather than
    racing a real sleep against the background _finalize_after_window()
    task -- that task clears the deadline and moves status away from
    "settling" the instant it actually runs, making a fixed sleep either
    too short (still genuinely within the window) or too long (status
    already back to idle/lobby, which would hit the *other* rejection
    branch, "round_not_running", instead of the one under test) with no
    reliable safe middle. Backdating deterministically hits the exact
    "still settling, window closed" branch every run, then lets the real
    settlement task complete normally afterward -- nothing about the
    branch under test is faked, only the timing of reaching it.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        first_winner = await create_funded_user(conn)
        late_claimant = await create_funded_user(conn)
        winning_card, other_card = 1, 2

        assert (await engine.join(first_winner, winning_card, auto_mark=False)).ok
        assert (await engine.join(late_claimant, other_card, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        winning_grid = card_pool[winning_card]
        other_grid = card_pool[other_card]

        def both_have_a_pattern() -> bool:
            # other_card must ALSO have a genuinely valid pattern by the
            # time the late claim arrives -- claim()'s own pattern check
            # runs *before* the winner-lock section this test targets, so
            # a card with no real pattern yet would be rejected as
            # "no_pattern" first, never reaching the branch under test.
            called = engine._called  # noqa: SLF001
            return bingo.has_won(winning_grid, called, room.win_patterns) and bingo.has_won(
                other_grid, called, room.win_patterns
            )

        await wait_until(both_have_a_pattern, timeout=10)
        first_result = await engine.claim(first_winner, winning_card)
        assert first_result.ok is True
        assert engine.status == "settling"

        # No await has happened since claim() returned, so the real
        # settlement task (already scheduled, not yet run) cannot have
        # touched this state yet -- safe to backdate deterministically.
        engine._winner_window_deadline = time.monotonic() - 1  # noqa: SLF001

        late_result = await engine.claim(late_claimant, other_card)
        assert late_result == ClaimResult(False, "round_already_settled")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_room_configured_for_one_winning_line_accepts_a_single_line(
    pool, redis, card_pool, conn
):
    """Proves per-room min_winning_lines configurability reaches the real
    production claim() path end to end, not just packages/core/bingo.py's
    own pure has_won() function in isolation. A room explicitly configured
    for 1 (the classic single-line game, still a supported configuration
    per the admin-facing rule this pass adds) must accept a claim after
    exactly one completed line -- the same card would be correctly
    rejected under this room's own sibling test's default (2 lines).
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=15, min_winning_lines=1
    )
    room = await load_room_config(pool, room_id)
    assert room.min_winning_lines == 1
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        winner = await create_funded_user(conn)
        other = await create_funded_user(conn)
        assert (await engine.join(winner, 1, auto_mark=False)).ok
        assert (await engine.join(other, 2, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        # Constructed directly rather than awaited via natural draw
        # progression, the same established reasoning as this file's
        # other min_winning_lines tests: deterministic and immediate,
        # not racing a background number-caller for an exact draw count.
        winning_grid = card_pool[1]
        engine._called = {winning_grid[0][c] for c in range(5)}  # noqa: SLF001
        assert len(bingo.winning_patterns(winning_grid, engine._called, room.win_patterns)) == 1  # noqa: SLF001

        result = await engine.claim(winner, 1)
        assert result.ok is True  # rejected under the default 2-line rule; accepted here
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_room_configured_for_three_winning_lines_rejects_two_lines(
    pool, redis, card_pool, conn
):
    """The other direction: a room requiring *more* than the default must
    reject a claim that would have won under the default 2-line rule.

    Constructs engine._called directly (the same established white-box
    technique test_concurrent_claims_two_players_only_one_holds_a_valid_
    pattern above already switched to, for the identical reason): waiting
    on natural draw progression for "exactly 2 lines, no more" risks the
    round exhausting all 75 calls (nobody can win under this room's own
    3-line rule from just joining) before that exact state is ever
    observed, leaving the original entries stale once a fresh round
    starts -- reproduced directly (a real not_in_round instead of
    no_pattern) before this fix.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=15, min_winning_lines=3
    )
    room = await load_room_config(pool, room_id)
    assert room.min_winning_lines == 3
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        winner = await create_funded_user(conn)
        other = await create_funded_user(conn)
        assert (await engine.join(winner, 1, auto_mark=False)).ok
        assert (await engine.join(other, 2, auto_mark=False)).ok
        await wait_until(lambda: engine.status == "running", timeout=5)

        winning_grid = card_pool[1]
        engine._called = {winning_grid[0][c] for c in range(5)} | {  # noqa: SLF001
            winning_grid[1][c] for c in range(5)
        }
        assert len(bingo.winning_patterns(winning_grid, engine._called, room.win_patterns)) == 2  # noqa: SLF001

        result = await engine.claim(winner, 1)
        assert result == ClaimResult(False, "no_pattern")  # a real win under the default rule
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_a_valid_claim_stops_the_round_immediately_no_further_calls(pool, redis, card_pool, conn):
    """Real-money fairness requirement, not just UX polish: once a player's
    "Bingo" is valid, the round must freeze right there -- calling even one
    more number after a winning claim already landed would be indistinguishable
    from the house quietly getting one more free draw once it already knows
    who won. _run_running()'s call loop (round_engine.py) checks
    self._status before every single _call_next_number(), and claim()
    flips status to "settling" synchronously the moment a valid claim is
    processed (no await between the pattern check and the flip) -- this
    proves that actually holds under real timing, not just by reading the
    code: call_interval_ms is set low enough that several more calls
    *would* have fired in the sleep window below if the freeze didn't work.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=150
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, card_b, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]

        def ready() -> bool:
            return bingo.has_won(grid_a, engine._called, room.win_patterns)  # noqa: SLF001

        await wait_until(ready, timeout=10)

        call_index_at_claim = engine._call_index  # noqa: SLF001
        round_id_at_claim = engine._round_id  # noqa: SLF001
        claim_started_at = asyncio.get_running_loop().time()
        result = await engine.claim(user_a, card_a)
        claim_latency = asyncio.get_running_loop().time() - claim_started_at
        assert result.ok, result

        assert engine.status == "settling"
        assert claim_latency < 1.0, f"claim() itself took {claim_latency:.3f}s -- too slow to feel immediate"

        # The real proof: sleep several call intervals' worth of real time
        # (long enough that _run_running()'s loop would have called at
        # least 2-3 more numbers by now if the freeze were broken), then
        # read the round's own persisted call_index back from Postgres --
        # the in-memory engine can't be read directly here since
        # _reset_to_idle() (settlement's own cleanup, which the sleep
        # window is deliberately long enough to also let finish) zeroes
        # self._call_index back out once this round is done.
        await asyncio.sleep(room.call_interval_ms / 1000 * 4)
        await wait_until(lambda: engine.status == "idle", timeout=5)
        final_call_index = await pool.fetchval(
            "SELECT call_index FROM rounds WHERE id = $1", round_id_at_claim
        )
        assert final_call_index == call_index_at_claim, (
            "a number was called after a valid claim already landed"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_two_simultaneous_auto_mark_winners_both_split_derash(pool, redis, card_pool, conn, monkeypatch):
    # Regression: a real code review pass caught that _call_next_number()'s
    # own auto-mark scan returned as soon as the *first* winning entry
    # flipped self._status away from "running" -- any *later* entry in
    # that same call who also completed a winning pattern on this exact
    # same number (a genuine simultaneous auto-mark tie) was never even
    # offered to claim(), silently losing that player's share of the
    # derash to whoever happened to come first in dict order.
    #
    # Unlike test_two_simultaneous_claims_split_derash_evenly (which polls
    # for two *real* cards to naturally both become winning, close enough
    # in time to land within the 50ms tie window via two manual claim()
    # calls), this fix specifically needs both winning conditions true
    # within the exact same _call_next_number() invocation -- the real
    # card pool's natural draw order doesn't reliably put two specific
    # cards' final winning numbers on the exact same call. bingo.
    # winning_patterns() is monkeypatched instead, so this test controls
    # precisely when each of the two real, real-joined players' cards
    # "complete" -- deterministic, not relying on draw-order luck, and it
    # still exercises the real _call_next_number()/claim()/settlement
    # path end to end, only the pattern-matching decision itself is faked.
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2
        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]
        # Two patterns, not one -- a real win now needs
        # bingo.MIN_WINNING_LINES complete lines.
        winning_patterns = [
            bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4))),
            bingo.Pattern(name="col_0", kind="col", cells=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))),
        ]

        def fake_winning_patterns(grid, called, enabled):
            if grid is grid_a or grid is grid_b:
                return winning_patterns
            return []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)

        await wait_until(lambda: engine.status == "idle", timeout=15)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        assert round_row["pot"] == Decimal("40.00")
        assert round_row["derash"] == Decimal("32.00")

        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1 ORDER BY user_id",
            round_row["id"],
        )
        # The actual bug, made concrete: without the fix this is 1 winner
        # (whichever of user_a/user_b came first in dict order) taking the
        # full derash, not 2 splitting it evenly.
        assert len(winners) == 2
        assert winners[0]["amount"] == winners[1]["amount"] == Decimal("16.00")
        assert winners[0]["amount"] + winners[1]["amount"] == round_row["derash"]
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_settlement_publishes_winner_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # A code review pass caught _settle_with_winners()'s own balance-update
    # push as a plain sequential `for ... await` loop -- each publish is
    # fully independent (a different user, its own pool connection, its
    # own Redis channel), so a simultaneous-tie round with several winners
    # used to serialize several round trips before round_end could even
    # broadcast, delaying that message for every player in the room, not
    # just whichever winner's own push was still waiting its turn.
    #
    # Same bingo.winning_patterns() monkeypatch technique as the sibling
    # tie test above, for the same reason: makes both real, real-joined
    # players' cards deterministic simultaneous winners, no reliance on
    # the real card pool's draw-order luck. Additionally monkeypatches
    # ledger.publish_balance_update() itself to make user_a's own publish
    # artificially slow -- proving the property under test directly:
    # user_b's publish must not be delayed behind it.
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2
        grid_a = card_pool[card_a]
        grid_b = card_pool[card_b]
        # Two patterns, not one -- a real win now needs
        # bingo.MIN_WINNING_LINES complete lines.
        winning_patterns = [
            bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4))),
            bingo.Pattern(name="col_0", kind="col", cells=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))),
        ]

        def fake_winning_patterns(grid, called, enabled):
            if grid is grid_a or grid is grid_b:
                return winning_patterns
            return []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)

        real_publish = ledger.publish_balance_update
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        start = asyncio.get_running_loop().time()
        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in published_at and user_b in published_at
        # Sequential (the old bug) would put user_b's publish ~0.5s after
        # user_a's slow one finished; concurrent puts it close to `start`
        # regardless of user_a's own delay.
        b_delay = published_at[user_b] - start
        assert b_delay < 0.3, (
            f"user_b's balance update landed {b_delay:.2f}s after settlement "
            "started -- stalled behind user_a's slow publish"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_an_unexpected_exception_during_auto_claim_does_not_crash_the_room(
    pool, redis, card_pool, conn, monkeypatch
):
    # Regression: a real code review pass caught that _call_next_number()'s
    # auto-claim scan had no exception isolation at all, unlike
    # _handle_command()'s own identical fix for the manual command path
    # (see its own comment there). An unexpected exception from claim() --
    # realistically _record_claim_attempt()'s own audit-log write, the one
    # real DB call left unguarded in claim() -- propagated straight out of
    # this loop, through _call_next_number(), _run_running()'s bare for
    # loop, and run_forever()'s own while loop, killing this room's entire
    # engine task. Nothing restarts it: the round would sit stuck until a
    # *different* engine worker started and recovery.py's crash sweep
    # found it -- which VOIDS AND REFUNDS the round rather than resuming
    # it, so the legitimate winner would lose their win entirely, along
    # with every other player in the room losing their round to a refund,
    # over one exception.
    #
    # Same bingo.winning_patterns() monkeypatch technique as the test
    # above, for the same reason: makes exactly one real, real-joined
    # player's card a deterministic immediate winner, so the auto-claim
    # scan's only ever call to claim() is fully predictable -- no reliance
    # on the real card pool's draw-order luck to land a specific player's
    # win on a specific call.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, call_interval_ms=15
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a, card_b = 1, 2
        grid_a = card_pool[card_a]
        # Two patterns, not one -- a real win now needs
        # bingo.MIN_WINNING_LINES complete lines.
        winning_patterns = [
            bingo.Pattern(name="row_0", kind="row", cells=((0, 0), (0, 1), (0, 2), (0, 3), (0, 4))),
            bingo.Pattern(name="col_0", kind="col", cells=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))),
        ]

        def fake_winning_patterns(grid, called, enabled):
            return winning_patterns if grid is grid_a else []

        assert (await engine.join(user_a, card_a, auto_mark=True)).ok
        assert (await engine.join(user_b, card_b, auto_mark=True)).ok

        real_claim = engine.claim
        call_count = 0

        async def flaky_claim(user_id, card_no, *, source="manual"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated bug in claim()")
            return await real_claim(user_id, card_no, source=source)

        await wait_until(lambda: engine.status == "running", timeout=5)
        monkeypatch.setattr(round_engine.bingo, "winning_patterns", fake_winning_patterns)
        monkeypatch.setattr(engine, "claim", flaky_claim)

        await wait_until(lambda: engine.status == "idle" and engine.round_id is None, timeout=15)

        # The whole point of isolation: the engine task survived the
        # exception instead of dying and waiting for recovery.py's next
        # crash sweep to void and refund the round.
        assert not task.done()
        assert call_count >= 2, "user_a's failed auto-claim was never retried on a later call"

        round_row = await pool.fetchrow(
            "SELECT id, status FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        assert round_row["status"] == "done"  # not voided/refunded
        winners = await pool.fetch(
            "SELECT user_id FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        # user_a still won -- the retry actually recovered the claim, not
        # just avoided crashing while silently losing it.
        assert {w["user_id"] for w in winners} == {user_a}
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_same_user_double_claim_race_settles_exactly_once(pool, redis, card_pool, conn):
    """Regression test: a real crash found by the Mini App's E2E test.

    A single player can produce two independent valid claims for the same
    *card* in the same round -- the server's own AUTO-mode scan and a
    client-sent `claim` message racing each other (a player with
    client-side AUTO on, or a manual double-tap). Both used to land in
    _pending_winners, crashing round_winners' (round_id, user_id) primary
    key at settlement. Exactly one must win; the other must be cleanly
    rejected, and settlement must still complete. (round_winners' key is
    now (round_id, user_id, card_no) -- a real, *different* card
    genuinely winning at the same time is a different scenario, covered
    separately below by test_same_user_two_different_winning_cards_both_
    paid.)
    """
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a = 1

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, 2, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]

        def a_ready() -> bool:
            return bingo.has_won(grid_a, engine._called, room.win_patterns)  # noqa: SLF001

        await wait_until(a_ready, timeout=10)

        results = await asyncio.gather(
            engine.claim(user_a, card_a, source="auto"),
            engine.claim(user_a, card_a, source="manual"),
        )
        oks = [r for r in results if r.ok]
        rejected = [r for r in results if not r.ok]
        assert len(oks) == 1, results
        assert len(rejected) == 1 and rejected[0].reason == "already_claimed", results

        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        winners = await pool.fetch(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert len(winners) == 1
        assert winners[0]["user_id"] == user_a

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_same_user_two_different_winning_cards_both_paid(pool, redis, card_pool, conn):
    """Multi-card product decision, confirmed with the user while planning
    this feature: a player whose multiple cards each independently
    complete a valid pattern in the same round gets paid for *every*
    winning card, not capped at one payout per round -- matching the
    reference product's visibly independent per-card claim buttons. Two
    of the same user's cards claiming at the same time must both win and
    both get their own full settlement share, proving round_winners'
    widened (round_id, user_id, card_no) primary key and claim()'s
    per-card (not per-user) already_pending guard actually deliver this,
    not just that they no longer crash.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), house_cut_bps=2000, min_players=2, call_interval_ms=15,
        max_cards_per_player=2,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn, Decimal("100.00"))
        user_b = await create_funded_user(conn)
        card_1, card_2 = 1, 2

        assert (await engine.join(user_a, card_1, auto_mark=False)).ok
        assert (await engine.join(user_a, card_2, auto_mark=False)).ok
        assert (await engine.join(user_b, 3, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_1, grid_2 = card_pool[card_1], card_pool[card_2]

        def both_ready() -> bool:
            called = engine._called  # noqa: SLF001
            return bingo.has_won(grid_1, called, room.win_patterns) and bingo.has_won(
                grid_2, called, room.win_patterns
            )

        await wait_until(both_ready, timeout=10)

        results = await asyncio.gather(
            engine.claim(user_a, card_1), engine.claim(user_a, card_2)
        )
        assert all(r.ok for r in results), results

        await wait_until(lambda: engine.status == "idle", timeout=5)

        round_row = await pool.fetchrow(
            "SELECT id, pot, derash FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1",
            room_id,
        )
        # 3 cards staked (2 by user_a, 1 by user_b) at 20 ETB, 80% derash.
        assert round_row["pot"] == Decimal("60.00")
        assert round_row["derash"] == Decimal("48.00")

        winners = await pool.fetch(
            "SELECT user_id, card_no, amount FROM round_winners WHERE round_id = $1 ORDER BY card_no",
            round_row["id"],
        )
        assert len(winners) == 2
        assert {w["card_no"] for w in winners} == {card_1, card_2}
        assert all(w["user_id"] == user_a for w in winners)
        assert winners[0]["amount"] == winners[1]["amount"] == Decimal("24.00")
        assert winners[0]["amount"] + winners[1]["amount"] == round_row["derash"]

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []

        # services/gateway/queries.py::user_history() must show this as
        # ONE round with the combined amount, not one row per card and
        # not the multiplicative blowup the old (round_id, user_id)-only
        # join produced (2 entries x 2 winner rows = 4 result rows) --
        # see that function's own comment for the full failure mode.
        history = await gateway_queries.user_history(pool, user_a)
        this_round = [h for h in history if h["round_id"] == round_row["id"]]
        assert len(this_round) == 1, f"expected exactly one history row for this round, got {this_round}"
        assert this_round[0]["won"] is True
        assert this_round[0]["won_amount"] == "48.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_claim_rejected_on_one_line_then_accepted_once_a_second_line_completes(
    pool, redis, card_pool, conn
):
    """The actual product rule, proved end to end against a real engine and
    a real draw (not a monkeypatched pattern check, unlike the tie/exception
    -isolation tests above that need a *deterministic* simultaneous win):
    a card with exactly one complete line must be rejected outright
    ("no_pattern", not merely "not yet" -- and specifically must not get
    silently locked out the way a genuinely-false claim does, since the
    player may go on to complete a second line on this same card), and the
    same card must then be accepted once a second line completes, however
    that second line got there (row/col/diagonal, any combination).
    """
    # A deliberately wider interval than this file's usual 15ms: this test
    # makes two sequential await engine.claim() calls after its own
    # wait_until() and asserts the game state hasn't moved on between
    # them. At 15ms, two real Postgres-transaction-plus-Redis-publish
    # round trips can occasionally take longer than one call interval
    # under real system load (confirmed directly: a flaky run reproduced
    # the *engine* correctly recognizing a genuine second-line win, or
    # correctly recognizing the round had already ended, exactly because
    # the next number(s) were called in the gap between these two claim()
    # awaits -- not a bug in the accept/reject logic under test, a race in
    # this test's own timing margin). The win-condition logic being tested
    # is identical regardless of call speed, so widening the interval only
    # removes the false precision requirement, it doesn't weaken what's
    # actually verified.
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=300)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a = 1

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, 2, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]

        def exactly_one_line() -> bool:
            called = engine._called  # noqa: SLF001
            return len(bingo.winning_patterns(grid_a, called, room.win_patterns)) == 1

        await wait_until(exactly_one_line, timeout=30)

        # One line is not a win: rejected, and specifically not locked out
        # -- a player who genuinely goes on to complete a second line on
        # this exact card must still be able to claim it for real.
        first = await engine.claim(user_a, card_a)
        assert first == ClaimResult(False, "no_pattern")

        second_attempt_too_early = await engine.claim(user_a, card_a)
        assert second_attempt_too_early == ClaimResult(False, "no_pattern")

        def has_real_win() -> bool:
            return bingo.has_won(grid_a, engine._called, room.win_patterns)  # noqa: SLF001

        # Matches the wider call_interval_ms above: worst case needs
        # close to the full 75-ball draw at 300ms/call (~22.5s), so this
        # needs real margin beyond that, not just beyond the typical case.
        await wait_until(has_real_win, timeout=30)

        final = await engine.claim(user_a, card_a)
        assert final.ok, final

        await wait_until(lambda: engine.status == "idle", timeout=5)
        round_row = await pool.fetchrow(
            "SELECT id FROM rounds WHERE room_id = $1 ORDER BY seq DESC LIMIT 1", room_id
        )
        winners = await pool.fetch(
            "SELECT user_id, pattern FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert len(winners) == 1
        assert winners[0]["user_id"] == user_a
        # The recorded pattern reflects every line the winning claim
        # completed, not just one.
        assert len(winners[0]["pattern"].split(",")) >= 2

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_false_claim_lockout_is_per_card_not_per_player(pool, redis, card_pool, conn):
    """claim()'s own lockout is scoped to (user_id, card_no), not just
    user_id -- a false claim on one of a player's cards says nothing
    about whether a *different* card they hold has a genuine pattern
    right now, both are validated against completely independent grids.
    Proves both halves: the same card stays locked out on a second
    attempt, and a different, genuinely winning card the same player
    holds is completely unaffected.
    """
    room_id = await create_room(
        conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15, max_cards_per_player=2,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_1, card_2 = 1, 2

        assert (await engine.join(user_a, card_1, auto_mark=False)).ok
        assert (await engine.join(user_a, card_2, auto_mark=False)).ok
        assert (await engine.join(user_b, 3, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        # At call_index 0, nothing on any card can possibly have two real
        # complete lines yet (the free cell alone never satisfies even one
        # row/col/diag) -- a deterministic "no_pattern" claim, not a race.
        first = await engine.claim(user_a, card_1)
        assert first == ClaimResult(False, "no_pattern")

        # The SAME card is now locked out.
        second = await engine.claim(user_a, card_1)
        assert second == ClaimResult(False, "locked_out")

        # A DIFFERENT card the same player holds must be completely
        # unaffected by card_1's lockout -- wait for it to actually
        # complete a real pattern, then claim it for real.
        grid_2 = card_pool[card_2]

        def card_2_ready() -> bool:
            called = engine._called  # noqa: SLF001
            return bingo.has_won(grid_2, called, room.win_patterns)

        await wait_until(card_2_ready, timeout=10)
        third = await engine.claim(user_a, card_2)
        assert third.ok, third

        await wait_until(lambda: engine.status == "idle", timeout=5)
        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_claim_settlement_pushes_a_live_balance_update_to_the_winner(pool, redis, card_pool, conn):
    # A code review pass caught that only services/payments/deposits.py
    # ever pushed a live balance_update -- staking and winning moved real
    # money but never told a connected player's UI its balance had
    # changed. round_engine.py's _settle_with_winners() now does.
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a = 1

        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, 2, auto_mark=False)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)

        grid_a = card_pool[card_a]

        def a_ready() -> bool:
            return bingo.has_won(grid_a, engine._called, room.win_patterns)  # noqa: SLF001

        await wait_until(a_ready, timeout=10)

        async def _claim() -> None:
            result = await engine.claim(user_a, card_a)
            assert result.ok, result.reason

        push = await recv_balance_update(redis, user_a, _claim)
        assert Decimal(push["cash"]) > Decimal("0.00")

        await wait_until(lambda: engine.status == "idle", timeout=5)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_round_end_broadcast_includes_the_winners_display_name(pool, redis, card_pool, conn):
    # A code-review pass caught that round_end's own "winners" list only
    # ever carried user_id -- not a real display value -- so every player
    # OTHER than the winner had no way to show who actually won, only a
    # bare amount. Confirms the broadcast itself (not just the DB row)
    # carries the same public display_name this codebase already shows a
    # player to everyone else elsewhere (admin console, bot messages).
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn)
        user_b = await create_funded_user(conn)
        card_a = 1
        assert (await engine.join(user_a, card_a, auto_mark=False)).ok
        assert (await engine.join(user_b, 2, auto_mark=False)).ok

        expected_name = await conn.fetchval("SELECT display_name FROM users WHERE id = $1", user_a)
        assert expected_name  # create_user() always sets one -- sanity-check the fixture, not this feature

        await wait_until(lambda: engine.status == "running", timeout=5)
        grid_a = card_pool[card_a]

        def a_ready() -> bool:
            return bingo.has_won(grid_a, engine._called, room.win_patterns)  # noqa: SLF001

        await wait_until(a_ready, timeout=10)

        pubsub = redis.pubsub()
        await pubsub.subscribe(f"room:{room_id}")
        try:
            result = await engine.claim(user_a, card_a)
            assert result.ok, result.reason

            round_end_msg = None
            for _ in range(50):  # other message types (call, etc.) share this same channel
                raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if raw is None:
                    continue
                decoded = json.loads(raw["data"])
                if decoded.get("t") == "round_end":
                    round_end_msg = decoded
                    break
            assert round_end_msg is not None, "round_end was never published"
        finally:
            await pubsub.unsubscribe(f"room:{room_id}")
            await pubsub.aclose()

        assert len(round_end_msg["winners"]) == 1
        assert round_end_msg["winners"][0]["user_id"] == user_a
        assert round_end_msg["winners"][0]["display_name"] == expected_name

        await wait_until(lambda: engine.status == "idle", timeout=5)
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_claim_from_user_not_in_round_rejected_and_logged(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)
        intruder = await create_funded_user(conn)
        await engine.join(p1, 1)
        await engine.join(p2, 2)
        await wait_until(lambda: engine.status == "running", timeout=5)

        result = await engine.claim(intruder, 99)
        assert result == ClaimResult(False, "not_in_round")

        round_id = engine.round_id
        row = await pool.fetchrow(
            "SELECT valid FROM claim_attempts WHERE round_id = $1 AND user_id = $2",
            round_id,
            intruder,
        )
        assert row is not None
        assert row["valid"] is False
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_claim_for_a_card_someone_else_holds_is_rejected(pool, redis, card_pool, conn):
    # Launch-readiness audit gap: the "unauthorized claim" test above only
    # ever claims a card_no (99) nobody joined at all -- it doesn't prove
    # the rejection is specifically about *ownership*, only that an
    # unclaimed card can't be claimed. This targets the sharper case: a
    # real player claims a card a *different*, real player actually holds.
    room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2, call_interval_ms=15)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        owner = await create_funded_user(conn)
        other = await create_funded_user(conn)
        attacker = await create_funded_user(conn)
        await engine.join(owner, 1)
        await engine.join(other, 2)
        await wait_until(lambda: engine.status == "running", timeout=5)

        # attacker never joined at all, but names card 1 -- which `owner`,
        # a real participant in this exact round, genuinely holds.
        result = await engine.claim(attacker, 1)
        assert result == ClaimResult(False, "not_in_round")

        round_id = engine.round_id
        row = await pool.fetchrow(
            "SELECT valid FROM claim_attempts WHERE round_id = $1 AND user_id = $2",
            round_id,
            attacker,
        )
        assert row is not None
        assert row["valid"] is False
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_round_exhausted_no_winner_full_refund(pool, redis, card_pool, conn):
    # win_patterns=[] means nothing can ever validate -- guarantees
    # exhaustion of all 75 calls with zero winners, deterministically.
    room_id = await create_room(
        conn,
        stake=Decimal("15.00"),
        min_players=2,
        call_interval_ms=2,
        win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("100.00"))
        p2 = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(p1, 1)
        await engine.join(p2, 2)

        round_id_before = engine.round_id
        await wait_until(lambda: engine.status == "idle", timeout=15)

        round_row = await pool.fetchrow(
            "SELECT status, call_index FROM rounds WHERE id = $1", round_id_before
        )
        assert round_row["status"] == "voided"
        assert round_row["call_index"] == 75

        winners = await pool.fetch(
            "SELECT 1 FROM round_winners WHERE round_id = $1", round_id_before
        )
        assert winners == []

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        cash2 = await ledger.get_or_create_account(conn, p2, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("100.00")
        assert await ledger.balance(conn, cash2.id) == Decimal("100.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_exhausted_no_winner_refund_pushes_a_live_balance_update(pool, redis, card_pool, conn):
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=2, win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("100.00"))
        p2 = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(p1, 1)
        await engine.join(p2, 2)

        push = await recv_balance_update(
            redis, p1, lambda: wait_until(lambda: engine.status == "idle", timeout=15)
        )
        assert push["cash"] == "100.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_exhausted_no_winner_refund_publishes_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # Same fix, same proof technique as
    # test_lobby_underfilled_refund_publishes_balance_updates_concurrently
    # above, for this file's other refund-then-publish loop.
    room_id = await create_room(
        conn, stake=Decimal("15.00"), min_players=2, call_interval_ms=2, win_patterns=[],
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn, Decimal("100.00"))
        user_b = await create_funded_user(conn, Decimal("100.00"))
        await engine.join(user_a, 1)
        await engine.join(user_b, 2)

        real_publish = ledger.publish_balance_update
        entered_at: dict[int, float] = {}
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            entered_at[user_id] = asyncio.get_running_loop().time()
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in entered_at and user_b in entered_at
        assert published_at[user_a] - entered_at[user_a] >= 0.5, "user_a's own artificial delay didn't apply"
        entry_gap = abs(entered_at[user_b] - entered_at[user_a])
        assert entry_gap < 0.3, (
            f"user_b's publish call started {entry_gap:.2f}s after user_a's -- "
            "stalled behind it instead of starting concurrently"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_lobby_underfilled_all_refunded_room_returns_to_idle(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        result = await engine.join(p1, 1)
        assert result.ok

        round_id = engine.round_id
        await wait_until(lambda: engine.status == "idle", timeout=10)

        round_row = await pool.fetchrow("SELECT status FROM rounds WHERE id = $1", round_id)
        assert round_row["status"] == "voided"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")

        assert engine.status == "idle"
        assert engine.round_id is None
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_a_room_gets_a_live_round_before_any_player_ever_joins(pool, redis, card_pool, conn):
    """The core rule behind this whole product correction: a round is
    server-owned and server-created, not started by a player's own card
    selection. Deliberately never calls engine.join() at all -- if a round
    only ever existed because someone joined, this test would time out
    exactly the way the pre-fix version of
    test_a_room_whose_last_round_ended_can_still_be_reentered (gateway/
    queries.py's own history) did.
    """
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.status == "lobby", timeout=5)
        assert engine.round_id is not None
        assert engine.player_count() == 0

        round_row = await pool.fetchrow(
            "SELECT status, lobby_deadline FROM rounds WHERE id = $1", engine.round_id
        )
        assert round_row["status"] == "lobby"
        assert round_row["lobby_deadline"] is not None
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_an_empty_room_automatically_opens_the_next_round_with_no_join_at_all(
    pool, redis, card_pool, conn
):
    """The other half of the same rule: the loop actually continues on its
    own, forever, with zero player involvement -- not just that the first
    round exists. min_players=2 and zero joins guarantees every round in
    this test voids as underfilled; no_player_next_round_delay_seconds=1
    (conftest's own fast-test default) keeps this quick without needing to
    fake the clock. is_active=True is required now that run_forever()'s
    loop actually re-checks it before starting a second round (see
    RoundEngine._room_is_still_active()) -- conftest's own create_room()
    otherwise defaults it False (a real, unrelated dev-database-hygiene
    fix, not a statement that this test's room shouldn't be active).
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=1, is_active=True
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        await wait_until(lambda: engine.status == "lobby", timeout=5)
        first_round_id = engine.round_id
        assert first_round_id is not None

        # The first round voids (nobody joined) -- wait for its OWN row to
        # go terminal, not just for engine.status, which is exactly the
        # kind of transient in-memory signal a continuously-cycling engine
        # makes unreliable to poll for a *specific* round's own outcome.
        async def first_round_is_terminal() -> bool:
            status = await pool.fetchval("SELECT status FROM rounds WHERE id = $1", first_round_id)
            return status == "voided"

        for _ in range(500):
            if await first_round_is_terminal():
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("first round never voided")

        # With nobody ever joining, only the engine itself can ever create
        # a second round -- proving the continuous loop, not a one-shot.
        for _ in range(500):
            if engine.round_id is not None and engine.round_id != first_round_id:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("no second round was ever created without a join")

        second_round_row = await pool.fetchrow(
            "SELECT seq, status FROM rounds WHERE id = $1", engine.round_id
        )
        assert second_round_row["status"] == "lobby"
        assert second_round_row["seq"] == 2
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_a_single_player_is_enough_to_go_active_and_win(pool, redis, card_pool, conn):
    """An explicit, repeated product correction: the round engine must
    never gate on player count beyond what settlement genuinely needs. A
    room's own min_players is still a real, per-room, configurable value
    (rooms.min_players, CHECK >= 1 as of this same change) -- this proves
    the floor itself now genuinely allows 1, not just that the column
    accepts it: a lone real player takes a card, the lobby timer expires
    with nobody else ever joining, and the round still goes active, calls
    numbers, and pays out -- the exact case min_players=2 used to refund
    and void instead.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), house_cut_bps=2000, min_players=1, call_interval_ms=15,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        solo_player = await create_funded_user(conn)
        assert (await engine.join(solo_player, 1)).ok

        await wait_until(lambda: engine.status == "running", timeout=5)
        assert engine.player_count() == 1

        # A single 5x5 card (minus FREE) will, in real 75-number draws,
        # essentially always complete some pattern well before all 75
        # calls -- no need to force or wait out an exhaustion path.
        await wait_until(lambda: engine.status == "idle", timeout=30)

        round_row = await pool.fetchrow(
            "SELECT id, status, pot, derash FROM rounds WHERE room_id = $1 ORDER BY seq ASC LIMIT 1",
            room_id,
        )
        assert round_row["status"] == "done", "a real win, not an exhausted refund"
        assert round_row["pot"] == Decimal("10.00")

        winner_row = await pool.fetchrow(
            "SELECT user_id, amount FROM round_winners WHERE round_id = $1", round_row["id"]
        )
        assert winner_row is not None
        assert winner_row["user_id"] == solo_player
        assert winner_row["amount"] == round_row["derash"]

        cash = await ledger.get_or_create_account(conn, solo_player, "user_cash")
        # create_funded_user()'s own default: 1000.00. Staked 10.00, paid
        # back derash (80% of the 10.00 pot after a 20% house cut) --
        # correctly a net loss in ETB terms for a lone player, exactly as
        # intended: the house cut has to come from somewhere, and with
        # nobody else's stake in the pot, it comes out of the only player
        # who staked at all.
        assert await ledger.balance(conn, cash.id) == Decimal("1000.00") - Decimal("10.00") + round_row["derash"]

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_a_genuinely_empty_round_still_goes_active_and_calls_real_numbers(
    pool, redis, card_pool, conn
):
    """The "zero-player round must not look dead" correction: a room the
    public can watch at any moment must never present as WAITING or VOID
    just because nobody has taken a card yet -- the live call board,
    current ball, and call history must all be genuinely real and moving,
    not merely "the room still exists." Deliberately never calls
    engine.join() at all -- self._entries stays empty the whole time.
    This is not a new code path: claim()'s own "not_in_round" guard
    already makes an empty entries set safe (nobody can ever claim
    against it), and _run_running()'s exhausted-no-winner branch already
    handles zero entrants correctly (refund_round()'s refund loop is a
    no-op over zero rows) -- this is the exact path two real players
    already take when nobody happens to win, just reached now with zero
    players from the very start instead.

    is_active=True is required for the second-round assertion below now
    that run_forever() re-checks it before starting a new round (see
    RoundEngine._room_is_still_active()) -- conftest's create_room()
    otherwise defaults it False for unrelated dev-database-hygiene
    reasons, not because this test's room shouldn't be active.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=1, lobby_seconds=1, call_interval_ms=10,
        is_active=True,
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        first_round_id = None

        def reached_running_with_zero_players() -> bool:
            nonlocal first_round_id
            if engine.status == "running" and engine.player_count() == 0:
                first_round_id = engine.round_id
                return True
            return False

        await wait_until(reached_running_with_zero_players, timeout=5)
        assert first_round_id is not None

        # Real numbers actually get called -- not a frozen "active" label
        # with nothing moving behind it.
        await wait_until(lambda: engine.status == "idle", timeout=15)

        round_row = await pool.fetchrow(
            "SELECT status, call_index, pot, started_at, ended_at FROM rounds WHERE id = $1",
            first_round_id,
        )
        assert round_row["status"] == "voided", "no possible winner with zero cards -- correctly terminal"
        assert round_row["call_index"] == 75, "the full real draw actually ran, not a skipped/faked one"
        assert round_row["pot"] == Decimal("0.00")
        assert round_row["started_at"] is not None, "genuinely entered the running phase"

        winners = await pool.fetch("SELECT 1 FROM round_winners WHERE round_id = $1", first_round_id)
        assert winners == []

        # The continuous loop still closes: a second live round opens on
        # its own, still with nobody watching or playing.
        for _ in range(500):
            if engine.round_id is not None and engine.round_id != first_round_id:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("no second round was ever created after the empty one completed")

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_lobby_underfilled_refund_pushes_a_live_balance_update(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok

        push = await recv_balance_update(
            redis, p1, lambda: wait_until(lambda: engine.status == "idle", timeout=10)
        )
        assert push["cash"] == "50.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_lobby_underfilled_refund_publishes_balance_updates_concurrently(
    pool, redis, card_pool, conn, monkeypatch
):
    # A code-review pass caught this as the same plain sequential
    # for/await loop already fixed for _settle_with_winners() (see
    # test_settlement_publishes_winner_balance_updates_concurrently
    # above, same technique mirrored here) -- a lobby that fills with
    # several entrants and then times out under min_players used to
    # serialize their refund balance-update pushes one after another.
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=5, lobby_seconds=1)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        user_a = await create_funded_user(conn, Decimal("50.00"))
        user_b = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(user_a, 1)).ok
        assert (await engine.join(user_b, 2)).ok

        real_publish = ledger.publish_balance_update
        entered_at: dict[int, float] = {}
        published_at: dict[int, float] = {}

        async def instrumented_publish(pool_, redis_, user_id):
            entered_at[user_id] = asyncio.get_running_loop().time()
            if user_id == user_a:
                await asyncio.sleep(0.5)
            result = await real_publish(pool_, redis_, user_id)
            published_at[user_id] = asyncio.get_running_loop().time()
            return result

        monkeypatch.setattr(round_engine.ledger, "publish_balance_update", instrumented_publish)

        await wait_until(lambda: engine.status == "idle", timeout=15)

        assert user_a in entered_at and user_b in entered_at
        assert published_at[user_a] - entered_at[user_a] >= 0.5, "user_a's own artificial delay didn't apply"
        # The property that actually distinguishes concurrent from
        # sequential: *when each call started*, not how long its own
        # work took. asyncio.gather() schedules both coroutines together,
        # so both entered_at timestamps land close together regardless of
        # how slow user_a's own publish is. Sequential (the old bug)
        # can't even call instrumented_publish(user_b) until user_a's own
        # await -- including its artificial 0.5s sleep -- fully resolves,
        # so entered_at[user_b] would land ~0.5s after entered_at[user_a].
        # Measured this way rather than against a fixed test-start
        # baseline on purpose: this test's first draft measured against
        # test start and failed on real numbers, not because the fix was
        # wrong, but because the lobby_seconds=1 wait before the engine
        # even calls refund_round() swamped that baseline with unrelated
        # time having nothing to do with publish concurrency.
        entry_gap = abs(entered_at[user_b] - entered_at[user_a])
        assert entry_gap < 0.3, (
            f"user_b's publish call started {entry_gap:.2f}s after user_a's -- "
            "stalled behind it instead of starting concurrently"
        )
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=15)


async def test_join_and_drop_card_push_live_balance_updates(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))

        async def _join() -> None:
            result = await engine.join(p1, 1)
            assert result.ok, result.reason

        join_push = await recv_balance_update(redis, p1, _join)
        assert join_push["cash"] == "40.00"

        async def _drop() -> None:
            result = await engine.drop_card(p1, 1)
            assert result.ok, result.reason

        drop_push = await recv_balance_update(redis, p1, _drop)
        assert drop_push["cash"] == "50.00"
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_drop_card_during_lobby_refunds(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("40.00")

        drop_result = await engine.drop_card(p1, 1)
        assert drop_result.ok
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_rejoining_a_dropped_card_is_refused_not_silently_undercharged(pool, redis, card_pool, conn):
    """A code review pass caught that join()/drop_card()'s stake/refund
    idempotency keys are static per (round_id, user_id, card_no) -- a
    genuine drop followed by a genuine rejoin of the identical card_no
    would collide with the original stake's key, ledger.post() would
    silently skip the real second charge (its own ON CONFLICT DO
    NOTHING), and join() would still unconditionally credit rounds.pot/
    self._pot for it -- a real money-integrity gap. The official Mini
    App UI doesn't expose a drop control today, but drop_card is still a
    live engine/WS command any client can send, and this codebase's own
    standing rule is that the server must be safe against what the wire
    protocol allows, not just what the shipped UI happens to click.
    Proves the fix: rejoining a card already dropped this round is
    refused outright, before any DB work, so the pot can never drift
    from real money collected.
    """
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("50.00"))
        assert (await engine.join(p1, 1)).ok

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("40.00")

        assert (await engine.drop_card(p1, 1)).ok
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")

        rejoin = await engine.join(p1, 1)
        assert rejoin.ok is False
        assert rejoin.reason == "card_already_dropped"

        # Refused before any DB work -- balance and pot both stay exactly
        # where the drop left them, not silently inflated.
        assert await ledger.balance(conn, cash1.id) == Decimal("50.00")
        assert engine.card_count() == 0
        round_row = await pool.fetchrow("SELECT pot FROM rounds WHERE id = $1", engine.round_id)
        assert round_row["pot"] == Decimal("0.00")

        # A genuinely different card is completely unaffected.
        assert (await engine.join(p1, 2)).ok
        assert await ledger.balance(conn, cash1.id) == Decimal("40.00")

        mismatches = await ledger.reconcile(conn)
        assert mismatches == []
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_duplicate_card_and_double_join_rejected(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn)
        p2 = await create_funded_user(conn)

        assert (await engine.join(p1, 7)).ok

        same_card = await engine.join(p2, 7)
        assert same_card.ok is False
        assert same_card.reason == "card_taken"

        # rooms.max_cards_per_player defaults to 1 (create_room() above
        # didn't raise it) -- a second card by the same user is correctly
        # rejected, just with a more specific reason than the old DB
        # constraint gave. test_a_player_can_hold_several_cards_up_to_the_
        # rooms_configured_limit below covers the actual multi-card
        # success path with a room that configures a higher limit.
        second_card_same_user = await engine.join(p1, 8)
        assert second_card_same_user.ok is False
        assert second_card_same_user.reason == "max_cards_reached"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        # Only the one successful stake should have been charged.
        assert await ledger.balance(conn, cash1.id) == Decimal("990.00")
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_a_player_can_hold_several_cards_up_to_the_rooms_configured_limit(
    pool, redis, card_pool, conn
):
    """The actual multi-card success path, with the highest-value
    assertion the plan called for: real *transaction count*, not just a
    balance delta -- a balance debited once can look identical to one
    debited correctly three times unless the transaction count is also
    checked, which is exactly what would have caught the original
    idempotency-key bug (stake-{round_id}-{user_id}, missing card_no,
    silently no-opped every card past a user's first).
    """
    room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2, max_cards_per_player=3)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        p1 = await create_funded_user(conn, Decimal("100.00"))

        assert (await engine.join(p1, 1)).ok
        assert (await engine.join(p1, 2)).ok
        assert (await engine.join(p1, 3)).ok

        fourth_card = await engine.join(p1, 4)
        assert fourth_card.ok is False
        assert fourth_card.reason == "max_cards_reached"

        cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
        assert await ledger.balance(conn, cash1.id) == Decimal("70.00")

        stake_txns = await conn.fetch(
            "SELECT idempotency_key FROM ledger_transactions "
            "WHERE kind = 'stake' AND idempotency_key LIKE $1 ORDER BY idempotency_key",
            f"stake-%-{p1}-%",
        )
        assert len(stake_txns) == 3, (
            "expected 3 separate stake transactions, one per card -- a count of fewer than 3 "
            "here (even with the right total balance) means the idempotency key collided and "
            "silently no-opped a real charge"
        )

        assert engine.player_count() == 1
        assert engine.card_count() == 3
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_multi_card_stake_drop_and_void_refund_reconciles_cleanly(pool, redis, card_pool, conn):
    """Ledger reconciliation across a full multi-card lifecycle: take 3
    cards (3 separate real stakes), drop one during the lobby (its own
    real refund), then force-void the round through the exact
    refund_round_in_transaction() path crash recovery/an underfilled
    lobby both use, for the 2 cards still left (plus a different
    player's own card) -- proving every card gets its own correctly-keyed
    ledger entry at every step, not just a balance that happens to net
    out right, and that the full sequence leaves ledger.reconcile()
    clean.
    """
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, lobby_seconds=5, max_cards_per_player=3,
    )
    room = await load_room_config(pool, room_id)
    engine = RoundEngine(pool, redis, room, card_pool)
    task = asyncio.create_task(engine.run_forever())

    p1 = await create_funded_user(conn, Decimal("100.00"))
    p2 = await create_funded_user(conn)

    assert (await engine.join(p1, 1)).ok
    assert (await engine.join(p1, 2)).ok
    assert (await engine.join(p1, 3)).ok
    assert (await engine.join(p2, 4)).ok

    cash1 = await ledger.get_or_create_account(conn, p1, "user_cash")
    assert await ledger.balance(conn, cash1.id) == Decimal("70.00")

    # Drop one of p1's three cards during the lobby -- its own real
    # refund, keyed by card_no, not shared with the other two.
    drop_result = await engine.drop_card(p1, 2)
    assert drop_result.ok
    assert await ledger.balance(conn, cash1.id) == Decimal("80.00")

    round_id = engine.round_id
    assert round_id is not None

    # Force a crash-recovery-style void, the same real, idempotent,
    # ledger-backed path an underfilled lobby and crash recovery both
    # use -- a hard cancel (not a graceful engine.stop(), which only
    # takes effect between rounds) is what actually leaves the round
    # non-terminal for refund_round() to act on.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await redis.delete(f"room:lock:{room_id}")

    refunded = await refunds.refund_round(pool, round_id, reason="test-forced-void")
    # p1's two remaining cards (1 and 3) + p2's card (4) -- card 2 is
    # already gone from round_entries, refunded separately by the drop
    # above, so it's correctly not in this count.
    assert refunded == 3

    assert await ledger.balance(conn, cash1.id) == Decimal("100.00")

    stake_txns = await conn.fetch(
        "SELECT idempotency_key FROM ledger_transactions "
        "WHERE kind = 'stake' AND idempotency_key LIKE $1 ORDER BY idempotency_key",
        f"stake-%-{p1}-%",
    )
    assert len(stake_txns) == 3, "expected 3 separate stake transactions, one per card p1 took"

    # Joined through ledger_entries rather than LIKE-matching the
    # idempotency key -- drop_card()'s own refund is keyed "drop-...",
    # not "refund-...", even though its kind is "refund" the same as
    # refund_round_in_transaction()'s, so a prefix match alone would
    # undercount.
    refund_txns = await conn.fetch(
        """
        SELECT lt.idempotency_key FROM ledger_transactions lt
        JOIN ledger_entries le ON le.transaction_id = lt.id
        WHERE lt.kind = 'refund' AND le.account_id = $1
        ORDER BY lt.idempotency_key
        """,
        cash1.id,
    )
    assert len(refund_txns) == 3, (
        "expected 3 separate refund transactions crediting p1 (the lobby drop of card 2, and "
        "the void of cards 1 and 3) -- fewer than 3 means an idempotency key collided and "
        "silently no-opped a real refund"
    )

    mismatches = await ledger.reconcile(conn)
    assert mismatches == []


async def test_max_players_cap_holds_under_real_concurrent_joins(pool, redis, card_pool, conn):
    # Regression: a real code review pass caught that the capacity check
    # (len(self._entries) >= max_players) had no lock of its own -- two
    # different users with two different card numbers (so the round_
    # entries UNIQUE constraint on card_no can't catch it) could both
    # read the same under-capacity count before either updated
    # self._entries, overfilling the room past its configured cap. A
    # small max_players and more concurrent joins than that cap, fired
    # genuinely simultaneously via asyncio.gather (not sequentially),
    # proves the fix holds under real concurrency, not just sequential
    # calls -- exactly the load/chaos style of test this codebase already
    # uses for its other real concurrency guarantees.
    # min_players=2 is met partway through this batch, but _run_lobby()
    # doesn't poll engine.stop() -- it only re-checks between its own
    # 1-second ticks -- so lobby_seconds has to stay short (matching
    # every other test in this file) or this test's own cleanup
    # (engine.stop() + wait_for(task, timeout=10)) would itself time out
    # waiting for a long lobby to naturally elapse. 3 seconds is still
    # comfortably longer than 10 concurrent, now-serialized local joins
    # should ever take, even under contention.
    room_id = await create_room(
        conn, stake=Decimal("10.00"), min_players=2, max_players=3, lobby_seconds=3
    )
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        players = [await create_funded_user(conn) for _ in range(10)]
        results = await asyncio.gather(
            *(engine.join(user_id, card_no) for card_no, user_id in enumerate(players, start=1))
        )
        successes = [r for r in results if r.ok]
        failures = [r for r in results if not r.ok]
        assert len(successes) == 3
        assert len(failures) == 7
        assert all(r.reason == "room_full" for r in failures)
        assert engine.player_count() == 3

        row = await pool.fetchrow("SELECT player_count FROM rounds WHERE id = $1", engine.round_id)
        assert row["player_count"] == 3  # in-memory count and the DB row agree
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)


async def test_two_different_stake_rooms_run_fully_concurrently_and_independently(
    pool, redis, card_pool, conn
):
    """The real, product-level question this proves end to end, not just
    by reading worker.py's own "one asyncio task per room" docstring:
    can a 10 ETB room and a 20 ETB room actually run live at the same
    time, each with its own players, with zero cross-talk? Every active
    room already gets its own independent RoundEngine task in real
    production (services/engine/worker.py::run_active_rooms) -- this
    constructs two such engines directly, the same way every other test
    in this file constructs one, and runs them concurrently via
    asyncio.gather so both rounds are genuinely in flight at once, not
    just sequentially exercised one after the other.

    Also proves per-room card isolation: card_no is scoped to *this
    engine's own* self._entries, not shared across rooms (see
    round_engine.py's own comment on why that dict is keyed by
    (user_id, card_no)), so the exact same card_no can be legitimately
    held by two different players in two different rooms at once.
    """
    cheap_room_id = await create_room(conn, stake=Decimal("10.00"), min_players=2)
    pricey_room_id = await create_room(conn, stake=Decimal("20.00"), min_players=2)
    cheap_engine = await make_engine(pool, redis, card_pool, cheap_room_id)
    pricey_engine = await make_engine(pool, redis, card_pool, pricey_room_id)
    cheap_task = asyncio.create_task(cheap_engine.run_forever())
    pricey_task = asyncio.create_task(pricey_engine.run_forever())
    try:
        cheap_p1 = await create_funded_user(conn, Decimal("100.00"))
        cheap_p2 = await create_funded_user(conn, Decimal("100.00"))
        pricey_p1 = await create_funded_user(conn, Decimal("100.00"))
        pricey_p2 = await create_funded_user(conn, Decimal("100.00"))

        # Same card_no (1 and 2) taken in both rooms at once, by entirely
        # different players -- would collide if card assignment were
        # global instead of per-room.
        results = await asyncio.gather(
            cheap_engine.join(cheap_p1, 1),
            cheap_engine.join(cheap_p2, 2),
            pricey_engine.join(pricey_p1, 1),
            pricey_engine.join(pricey_p2, 2),
        )
        assert all(r.ok for r in results), results

        await asyncio.gather(
            wait_until(lambda: cheap_engine.status == "running", timeout=5),
            wait_until(lambda: pricey_engine.status == "running", timeout=5),
        )

        # Independent round rows, independent stakes -- neither room's
        # config leaked into the other's round.
        assert cheap_engine.round_id != pricey_engine.round_id
        cheap_round = await pool.fetchrow(
            "SELECT stake FROM rounds WHERE id = $1", cheap_engine.round_id
        )
        pricey_round = await pool.fetchrow(
            "SELECT stake FROM rounds WHERE id = $1", pricey_engine.round_id
        )
        assert cheap_round["stake"] == Decimal("10.00")
        assert pricey_round["stake"] == Decimal("20.00")

        # Each player was debited their own room's stake, not the other
        # room's -- real ledger balances, not just in-memory bookkeeping.
        for user_id in (cheap_p1, cheap_p2):
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            assert await ledger.balance(conn, cash.id) == Decimal("90.00")
        for user_id in (pricey_p1, pricey_p2):
            cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
            assert await ledger.balance(conn, cash.id) == Decimal("80.00")

        # Both engines are genuinely calling numbers concurrently, not
        # one blocking the other -- each room's own call_index advances
        # on its own schedule.
        cheap_calls_before = len(cheap_engine._called)  # noqa: SLF001
        pricey_calls_before = len(pricey_engine._called)  # noqa: SLF001
        await wait_until(
            lambda: len(cheap_engine._called) > cheap_calls_before  # noqa: SLF001
            and len(pricey_engine._called) > pricey_calls_before,  # noqa: SLF001
            timeout=10,
        )
    finally:
        await asyncio.gather(cheap_engine.stop(), pricey_engine.stop())
        await asyncio.wait_for(asyncio.gather(cheap_task, pricey_task), timeout=15)


async def test_insufficient_balance_join_rejected_no_partial_state(pool, redis, card_pool, conn):
    room_id = await create_room(conn, stake=Decimal("500.00"), min_players=2)
    engine = await make_engine(pool, redis, card_pool, room_id)
    task = asyncio.create_task(engine.run_forever())
    try:
        poor_user = await create_funded_user(conn, Decimal("10.00"))
        result = await engine.join(poor_user, 1)
        assert result.ok is False
        assert result.reason == "insufficient_funds"

        # No stake means no round_entries row either -- the two must move
        # together or not at all.
        row = await pool.fetchrow(
            "SELECT 1 FROM round_entries WHERE round_id = $1 AND user_id = $2",
            engine.round_id,
            poor_user,
        )
        assert row is None
        assert engine.player_count() == 0
    finally:
        await engine.stop()
        await asyncio.wait_for(task, timeout=10)
