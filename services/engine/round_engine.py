"""The room state machine. One RoundEngine instance owns exactly one room,
runs one round at a time inside it, and is the sole writer of that room's
state -- see room_lock.py for how that single-writer property is enforced
across worker processes.

State machine (spec section 3.3):

    IDLE --first join--> LOBBY --countdown hits 0, enough players--> RUNNING
                            |                                          |
                            +--not enough players (refund, void)       |
                                                                        |
    RUNNING --valid claim (+ 50ms tie window)--> settle --> IDLE       |
    RUNNING --75 calls, no winner--> refund, void --> IDLE  <----------+

Nothing here trusts the client for anything that matters: the marked grid is
always recomputed server-side from the round's own called-numbers set, never
from anything a caller supplies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import redis.exceptions
import structlog
from redis.asyncio import Redis

from packages.core import bingo, ledger, metrics, responsible_gaming
from packages.core.bingo import Grid
from packages.core.ledger import Entry, InsufficientFunds
from services.engine import commands, refunds, settlement
from services.engine.room_lock import RoomLock

logger = structlog.get_logger()

WINNER_TIE_WINDOW_SECONDS = 0.05


@dataclass(frozen=True)
class RoomConfig:
    id: int
    code: str
    stake: Decimal
    house_cut_bps: int
    min_players: int
    max_players: int
    lobby_seconds: int
    call_interval_ms: int
    result_seconds: int
    win_patterns: list[str]
    max_cards_per_player: int
    no_player_next_round_delay_seconds: int
    min_winning_lines: int


@dataclass(frozen=True)
class RoundEntryState:
    card_no: int
    auto_mark: bool


@dataclass(frozen=True)
class PendingWinner:
    user_id: int
    card_no: int
    pattern: str  # every completed line's name, comma-joined (>= the room's own min_winning_lines)
    call_index: int


@dataclass(frozen=True)
class JoinResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    reason: str | None = None


async def load_room_config(pool: asyncpg.Pool, room_id: int) -> RoomConfig:
    row = await pool.fetchrow("SELECT * FROM rooms WHERE id = $1", room_id)
    if row is None:
        raise ValueError(f"no such room: {room_id}")
    win_patterns = json.loads(row["win_patterns"]) if isinstance(
        row["win_patterns"], str
    ) else row["win_patterns"]
    return RoomConfig(
        id=row["id"],
        code=row["code"],
        stake=row["stake"],
        house_cut_bps=row["house_cut_bps"],
        min_players=row["min_players"],
        max_players=row["max_players"],
        lobby_seconds=row["lobby_seconds"],
        call_interval_ms=row["call_interval_ms"],
        result_seconds=row["result_seconds"],
        win_patterns=list(win_patterns),
        max_cards_per_player=row["max_cards_per_player"],
        no_player_next_round_delay_seconds=row["no_player_next_round_delay_seconds"],
        min_winning_lines=row["min_winning_lines"],
    )


async def load_card_pool(pool: asyncpg.Pool) -> dict[int, Grid]:
    rows = await pool.fetch("SELECT card_no, grid FROM cards")
    out: dict[int, Grid] = {}
    for row in rows:
        grid = row["grid"]
        out[row["card_no"]] = json.loads(grid) if isinstance(grid, str) else grid
    return out


class RoundEngine:
    def __init__(
        self,
        pool: asyncpg.Pool,
        redis: Redis,
        room: RoomConfig,
        card_pool: dict[int, Grid],
        *,
        worker_id: str | None = None,
    ) -> None:
        self._pool = pool
        self._redis = redis
        self._room = room
        self._card_pool = card_pool
        self._lock = RoomLock(redis, room.id, worker_id)

        self._stop_requested = False
        self._round_active_event = asyncio.Event()
        self._winner_lock = asyncio.Lock()
        self._round_start_lock = asyncio.Lock()
        # Serializes join()'s capacity check through its self._entries
        # update -- in real production this is already effectively
        # single-threaded (one engine's _serve_commands() consumes its
        # room's command stream one entry at a time), but a code review
        # pass correctly noted the check itself has no lock of its own,
        # so a caller invoking join() concurrently (this codebase's own
        # load/chaos tests do exactly that) could let a room's
        # max_players cap be raced past: two different users, two
        # different card numbers, both reading the same under-capacity
        # count before either updates self._entries.
        self._join_lock = asyncio.Lock()
        self._settlement_task: asyncio.Task[None] | None = None

        self._round_id: int | None = None
        self._seq: int = 0
        self._status: str = "idle"
        self._server_seed: bytes | None = None
        self._server_seed_hash: str | None = None
        self._client_seed: str | None = None
        self._pot = Decimal("0")
        # Keyed by (user_id, card_no), not user_id alone -- a player can
        # hold more than one card per round (spec: configurable via
        # room.max_cards_per_player). See player_count()/card_count() for
        # the two different counts this now needs to distinguish.
        self._entries: dict[tuple[int, int], RoundEntryState] = {}
        self._called: set[int] = set()
        self._draw_order: list[int] = []
        self._call_index: int = 0
        self._running_started_at: float = 0.0
        self._lobby_deadline_monotonic: float = 0.0
        self._locked_out: set[tuple[int, int]] = set()
        self._auto_claimed: set[tuple[int, int]] = set()
        # A code review pass caught that join()/drop_card()'s stake/refund
        # idempotency keys are still static per (round_id, user_id,
        # card_no) -- correct for deduping a genuine retry of the *same*
        # in-flight command, but a real drop followed by a real rejoin of
        # the identical card_no reuses the original stake key. ledger.post
        # ()'s ON CONFLICT DO NOTHING then silently skips the second real
        # charge while join() still unconditionally credits rounds.pot/
        # self._pot for it -- inflating the pot with no money behind it,
        # which pot_escrow (excluded from ledger.py's USER_BALANCE_KINDS)
        # can quietly go negative to cover at settlement. Redesigning the
        # key scheme to make repeated holds of the same card safely
        # idempotent is real work with its own risk of a *worse* double-
        # charge bug if gotten wrong; forbidding the one situation that
        # triggers it -- rejoining a card already dropped this round --
        # is the safe fix; nothing in the product needs a player to
        # retake the exact card they just gave up.
        self._dropped_this_round: set[tuple[int, int]] = set()
        self._pending_winners: list[PendingWinner] = []
        self._winner_window_deadline: float | None = None

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, value: str) -> None:
        # engine_rooms_active counts idle-vs-not, so it only moves on the
        # actual idle boundary crossing -- not on every one of the five
        # status assignments below (settling/running/done are all already
        # "active" and shouldn't double-increment).
        if value != "idle" and self._status == "idle":
            metrics.engine_rooms_active.inc()
        elif value == "idle" and self._status != "idle":
            metrics.engine_rooms_active.dec()
        self._status = value

    @property
    def round_id(self) -> int | None:
        return self._round_id

    @property
    def pot(self) -> Decimal:
        return self._pot

    def player_count(self) -> int:
        """Distinct players -- not total cards. Matches how min_players/
        max_players already read in product terms ("this room fits N
        people"), and is the real fix for a real abuse vector: before this,
        one player holding N cards could single-handedly satisfy
        min_players and start (and win) a round alone.
        """
        return len({user_id for user_id, _ in self._entries})

    def card_count(self) -> int:
        return len(self._entries)

    def is_lock_held(self) -> bool:
        return self._lock.is_held()

    # --- lifecycle -----------------------------------------------------

    async def run_forever(self) -> bool:
        if not await self._lock.acquire():
            return False
        commands_task = asyncio.create_task(self._serve_commands())
        try:
            while not self._stop_requested and self._lock.is_held():
                # Server-owned, continuous round lifecycle: a room's round
                # is created proactively the moment this engine claims the
                # room (and again immediately after every previous round
                # resets to idle), never lazily by a player's own
                # take_card. A player selecting a card only ever joins a
                # round that already exists -- there is no path left where
                # "a card was selected" is what causes a round to start.
                # The double-checked _round_start_lock is the exact same
                # one join()'s own (now purely a defensive fallback, for
                # the brief window before this has run, and for tests that
                # construct an engine without a run_forever() task) idle
                # -handling below already uses, so the two can never race
                # each other into creating two rounds for the same room.
                if self._status == "idle":
                    if self._seq > 0:
                        # Not this engine's very first round (self._seq
                        # only advances inside _start_new_round()) -- give
                        # idle a real, observable resting window before
                        # proactively creating the next one. Every
                        # termination path (a winner, an exhausted round
                        # with none, an underfilled lobby) funnels through
                        # here, so this is the one place that needs it,
                        # rather than duplicating a wait at each of those
                        # call sites. Without it, self._status went
                        # "idle" and immediately back to "lobby" again
                        # within microseconds -- a tight insert-void
                        # -insert loop hammering Postgres for an empty
                        # room's benefit, and too narrow a window for
                        # anything polling for "idle" (this file's own
                        # tests included) to reliably observe. The very
                        # first round skips this: a room should become
                        # live the instant its engine claims it, not sit
                        # idle for a few seconds first.
                        await self._wait_before_next_round()
                        if self._stop_requested or not self._lock.is_held():
                            break
                        if not await self._room_is_still_active():
                            # An admin deactivated (or emergency-stopped)
                            # this room while this same engine instance
                            # was still alive -- honor that at the one
                            # point it's always safe to: between rounds,
                            # never abandoning a round already in
                            # progress. See _room_is_still_active()'s own
                            # docstring for the gap this closes.
                            break
                    async with self._round_start_lock:
                        if self._status == "idle":
                            await self._start_new_round()
                            self._round_active_event.set()
                await self._run_lobby()
        finally:
            commands_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await commands_task
            await self._lock.release()
        return True

    async def stop(self) -> None:
        self._stop_requested = True
        self._round_active_event.set()

    async def _room_is_still_active(self) -> bool:
        """A real, previously-unverified gap this closes: rooms.is_active
        was only ever read by services/engine/worker.py's own claim scan
        (`WHERE is_active = true`), deciding whether to *start* a new
        engine task for a room -- nothing in this class's own run_forever()
        loop ever re-checked it, so an admin's Deactivate or even the
        emergency Stop Room action (services/admin/queries.py::
        stop_room_admin, which does correctly halt the *current* round and
        durably sets is_active = false) had no way to reach an engine that
        was still alive and still holding this room's lock: it would just
        go on proactively starting another round, silently contradicting
        the admin console's own displayed promise ("...cannot restart
        automatically"). self._room itself is a frozen snapshot loaded
        once at claim time (see RoomConfig's own docstring reasoning
        elsewhere in this file), so this deliberately re-reads the live
        column each time instead of trusting that snapshot.
        """
        return bool(
            await self._pool.fetchval("SELECT is_active FROM rooms WHERE id = $1", self._room.id)
        )

    # --- player actions --------------------------------------------------

    async def join(self, user_id: int, card_no: int, *, auto_mark: bool = True) -> JoinResult:
        if self._status == "idle":
            # A defensive fallback now, not the primary mechanism -- in
            # real production run_forever()'s own loop always beats any
            # join() to creating a room's round (a player joins an
            # already-live round; joining never itself starts one). This
            # only ever actually fires in the brief window before that
            # loop's first iteration completes, and for tests that
            # construct a RoundEngine and call join() without ever running
            # a run_forever() task at all. Many joins can arrive
            # concurrently against a genuinely idle room either way (a
            # burst of players hitting an empty room at once) -- without
            # this lock, every one of them would see "idle" before the
            # first had a chance to flip it to "lobby", and each would try
            # to INSERT the same next round seq number, raising a
            # UniqueViolationError on rounds_room_id_seq_key instead of
            # just joining the one round that actually got created. The
            # inner re-check is what makes only the first caller actually
            # start a round; everyone else (including run_forever()'s own
            # loop, guarded by the exact same lock) finds it already
            # started once they get the lock.
            async with self._round_start_lock:
                if self._status == "idle":
                    await self._start_new_round()
                    self._round_active_event.set()

        if self._status != "lobby":
            return JoinResult(False, "not_joinable")
        if card_no not in self._card_pool:
            return JoinResult(False, "invalid_card")

        # Covers the capacity check through the self._entries update below
        # -- see this lock's own definition in __init__ for why a room's
        # max_players cap needs an explicit lock here, not just the
        # single-consumer command-stream loop production already
        # naturally serializes joins through.
        async with self._join_lock:
            if (user_id, card_no) in self._dropped_this_round:
                # See __init__'s own comment on self._dropped_this_round --
                # the idempotency keys below are static per (round_id,
                # user_id, card_no), so a genuine rejoin of a card already
                # dropped this round would collide with the original
                # stake's key: ledger.post() would silently skip the real
                # charge while this function still credits the pot for
                # it. Nothing in the product needs a player to retake the
                # exact card they just gave up, so this is refused
                # outright rather than risking a money-accounting gap.
                return JoinResult(False, "card_already_dropped")
            cards_held = sum(1 for uid, _ in self._entries if uid == user_id)
            already_has_a_card = cards_held > 0
            # max_players caps distinct players, not total cards -- an
            # existing player taking a 2nd/3rd card doesn't consume a new
            # "player slot", only a genuinely new player does.
            if not already_has_a_card and self.player_count() >= self._room.max_players:
                return JoinResult(False, "room_full")
            if cards_held >= self._room.max_cards_per_player:
                return JoinResult(False, "max_cards_reached")

            round_id = self._round_id
            assert round_id is not None
            idem = f"stake-{round_id}-{user_id}-{card_no}"

            async with self._pool.acquire() as conn:
                try:
                    async with conn.transaction():
                        # A per-user advisory lock, held for this whole
                        # transaction: self._join_lock above only serializes
                        # joins within *this* room, so without this, the same
                        # user joining two different rooms at once could have
                        # both check_stake_allowed() calls read today's net
                        # loss before either stake commits, letting both pass
                        # a daily_loss_cap that either one alone would have
                        # blocked. A row lock on the user_cash account would
                        # only help once that account_balances row exists (it
                        # doesn't for a brand-new user with no ledger history
                        # yet), so this uses pg_advisory_xact_lock keyed on
                        # user_id instead -- unconditional, and released
                        # automatically on commit or rollback. No other code
                        # in this codebase takes an advisory lock, so there's
                        # no cross-purpose key collision to worry about.
                        await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

                        # The round's real status, locked, not this engine's
                        # in-memory one: an admin void or emergency stop
                        # refunds the round from another process and never
                        # tells this engine, which keeps saying 'lobby'. A
                        # stake taken into that voided round is one nothing
                        # would ever refund (platform audit, 2026-09-25;
                        # test_bingo_external_void.py). Locked before any
                        # balance row, the same order refund_round() and
                        # settlement take it in.
                        round_status = await conn.fetchval(
                            "SELECT status FROM rounds WHERE id = $1 FOR NO KEY UPDATE", round_id
                        )
                        if round_status != "lobby":
                            return JoinResult(False, "not_joinable")

                        block = await responsible_gaming.check_stake_allowed(
                            conn, user_id, self._room.stake
                        )
                        if block.blocked:
                            assert block.reason is not None
                            return JoinResult(False, block.reason)

                        await conn.execute(
                            "INSERT INTO round_entries (round_id, card_no, user_id, auto_mark) "
                            "VALUES ($1, $2, $3, $4)",
                            round_id,
                            card_no,
                            user_id,
                            auto_mark,
                        )
                        cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
                        pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                        txn = await ledger.post(
                            conn,
                            "stake",
                            [Entry(cash.id, -self._room.stake), Entry(pot_account.id, self._room.stake)],
                            idempotency_key=idem,
                            round_id=round_id,
                        )
                        await conn.execute(
                            "UPDATE round_entries SET stake_txn_id = $1 "
                            "WHERE round_id = $2 AND card_no = $3",
                            txn.id,
                            round_id,
                            card_no,
                        )
                        await conn.execute(
                            "UPDATE rounds SET pot = pot + $1, player_count = player_count + 1 "
                            "WHERE id = $2",
                            self._room.stake,
                            round_id,
                        )
                    # Only reachable once the transaction above has
                    # actually committed -- see ledger.post()'s own
                    # comment for why it can't safely record this itself
                    # when called nested, which every real call is.
                    metrics.ledger_transactions_total.labels(kind=txn.kind).inc()
                except asyncpg.exceptions.UniqueViolationError:
                    # round_entries' UNIQUE(round_id, user_id) is gone as of
                    # this same change (a player can hold several cards
                    # now) -- the PRIMARY KEY (round_id, card_no) is the
                    # only remaining source of a violation here, always
                    # meaning someone already holds this exact card.
                    return JoinResult(False, "card_taken")
                except InsufficientFunds:
                    return JoinResult(False, "insufficient_funds")

            self._entries[(user_id, card_no)] = RoundEntryState(card_no=card_no, auto_mark=auto_mark)
            self._pot += self._room.stake

        await ledger.publish_balance_update(self._pool, self._redis, user_id)
        await self._publish_room({"t": "card_taken", "card_no": card_no, "taken": True})
        return JoinResult(True, None)

    async def drop_card(self, user_id: int, card_no: int) -> JoinResult:
        if self._status != "lobby":
            return JoinResult(False, "not_droppable")
        entry = self._entries.get((user_id, card_no))
        if entry is None:
            return JoinResult(False, "not_in_round")

        round_id = self._round_id
        assert round_id is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # The round's real status, locked, and the entry actually
                # removed, before any money moves: if the round was already
                # refunded (an admin void, or this engine's own underfilled-
                # lobby refund racing the drop), this card's stake is already
                # back with the player. The drop's own refund carries a
                # different key (drop- vs refund-), so nothing else would stop
                # it paying the stake back twice (platform audit, 2026-09-25;
                # test_bingo_external_void.py).
                round_status = await conn.fetchval(
                    "SELECT status FROM rounds WHERE id = $1 FOR NO KEY UPDATE", round_id
                )
                if round_status != "lobby":
                    return JoinResult(False, "not_droppable")
                removed = await conn.fetchval(
                    "DELETE FROM round_entries WHERE round_id = $1 AND user_id = $2 AND card_no = $3 "
                    "RETURNING card_no",
                    round_id,
                    user_id,
                    card_no,
                )
                if removed is None:
                    return JoinResult(False, "not_in_round")
                cash = await ledger.get_or_create_account(conn, user_id, "user_cash")
                pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                txn = await ledger.post(
                    conn,
                    "refund",
                    [Entry(pot_account.id, -self._room.stake), Entry(cash.id, self._room.stake)],
                    idempotency_key=f"drop-{round_id}-{user_id}-{card_no}",
                    round_id=round_id,
                )
                await conn.execute(
                    "UPDATE rounds SET pot = pot - $1, player_count = player_count - 1 "
                    "WHERE id = $2",
                    self._room.stake,
                    round_id,
                )
            # Only reachable once the transaction above has actually
            # committed -- see ledger.post()'s own comment for why it
            # can't safely record this itself when called nested, which
            # every real call is.
            metrics.ledger_transactions_total.labels(kind=txn.kind).inc()

        del self._entries[(user_id, card_no)]
        # See __init__'s own comment on self._dropped_this_round -- this
        # card_no can't be rejoined by this user again this round, so the
        # static stake idempotency key above can never collide with a
        # future real charge.
        self._dropped_this_round.add((user_id, card_no))
        self._pot -= self._room.stake
        await ledger.publish_balance_update(self._pool, self._redis, user_id)
        await self._publish_room({"t": "card_taken", "card_no": entry.card_no, "taken": False})
        return JoinResult(True, None)

    async def set_auto(self, user_id: int, auto: bool) -> JoinResult:
        # Applies to every card this user holds, not just one -- there's no
        # per-card AUTO toggle in the product, and this already correctly
        # bulk-updates every one of this user's round_entries rows below.
        held_cards = [card_no for uid, card_no in self._entries if uid == user_id]
        if not held_cards:
            return JoinResult(False, "not_in_round")

        for card_no in held_cards:
            entry = self._entries[(user_id, card_no)]
            self._entries[(user_id, card_no)] = RoundEntryState(card_no=entry.card_no, auto_mark=auto)
        if self._round_id is not None:
            await self._pool.execute(
                "UPDATE round_entries SET auto_mark = $1 WHERE round_id = $2 AND user_id = $3",
                auto,
                self._round_id,
                user_id,
            )
        return JoinResult(True, None)

    async def claim(self, user_id: int, card_no: int, *, source: str = "manual") -> ClaimResult:
        # Every attempt is logged to claim_attempts, including ones from a
        # user who isn't even in the round -- that's still an event worth an
        # audit trail, not just the attempts that get as far as a pattern
        # check. valid stays False unless a real pattern check below passes.
        valid = False
        try:
            with metrics.engine_claim_validation_seconds.time():
                # Per-card lockout, not per-user: a false claim on one card
                # says nothing about whether a *different* card this same
                # user holds has a genuine pattern right now -- both are
                # validated against completely independent grids below
                # either way, so locking out every other card a player
                # holds over one false claim on one of them would be worse
                # UX than the reference with no fraud-prevention benefit.
                # The existing session-level FALSE_CLAIM_SESSION_LIMIT
                # (services/gateway/connection.py) and the per-user
                # rate_limit.CLAIM token bucket already bound the "spam
                # false claims across many cheap cards" concern.
                if (user_id, card_no) in self._locked_out:
                    return ClaimResult(False, "locked_out")
                entry = self._entries.get((user_id, card_no))
                if entry is None:
                    return ClaimResult(False, "not_in_round")
                if self._status not in ("running", "settling"):
                    return ClaimResult(False, "round_not_running")

                grid = self._card_pool[entry.card_no]
                # Inlined rather than calling has_won() a second time on
                # top of this: both ultimately reduce to the exact same
                # comparison, but has_won() would recompute winning_
                # patterns() (a full grid re-scan) a second time for no
                # benefit, and this call site needs the real pattern list
                # (won) regardless, not just the boolean verdict.
                # self._room.min_winning_lines (not bingo.MIN_WINNING_LINES)
                # is what makes this respect the room's own configured
                # rule -- see that module constant's own comment on why a
                # hardcoded reference here would silently drift out of
                # sync with a per-room configurable value.
                won = bingo.winning_patterns(grid, self._called, self._room.win_patterns)
                valid = len(won) >= self._room.min_winning_lines
                if not valid:
                    # Lock out only a claim with zero real progress (a
                    # clearly bogus/spam attempt) -- a claim with at least
                    # one genuine complete line just hasn't reached the
                    # room's own required min_winning_lines yet, and
                    # locking the card out here
                    # would strand a player who is honestly one line away
                    # from a real win for the rest of the round, over one
                    # early tap.
                    if source == "manual" and self._status == "running" and not won:
                        self._locked_out.add((user_id, card_no))
                    return ClaimResult(False, "no_pattern")
        finally:
            if self._round_id is not None:
                await self._record_claim_attempt(user_id, card_no, valid)

        now = time.monotonic()
        async with self._winner_lock:
            # A user can reach a valid claim through two independent paths
            # for the *same card* in the same round -- the server's own
            # AUTO-mode scan (_call_next_number) and a client-sent `claim`
            # message (a player with AUTO on client-side races to send one
            # too, or a manual player double-taps) -- either of which could
            # otherwise add the same (user_id, card_no) to _pending_winners
            # twice and crash the round_winners (round_id, user_id,
            # card_no) primary key at settlement. One claim per *card* per
            # round -- a player's other, different cards are unaffected and
            # can each independently claim and win too (each of a player's
            # winning cards gets its own full share, matching the
            # reference's independent per-card buttons -- confirmed as the
            # intended product behavior, not assumed).
            already_pending = any(
                w.user_id == user_id and w.card_no == card_no for w in self._pending_winners
            )
            if already_pending:
                return ClaimResult(False, "already_claimed")

            if self._status == "running":
                self._set_status("settling")
                deadline = now + WINNER_TIE_WINDOW_SECONDS
                self._winner_window_deadline = deadline
                self._pending_winners.append(
                    PendingWinner(
                        user_id, entry.card_no, ", ".join(p.name for p in won), self._call_index
                    )
                )
                self._settlement_task = asyncio.create_task(self._finalize_after_window(deadline))
                return ClaimResult(True, None)
            if (
                self._status == "settling"
                and self._winner_window_deadline is not None
                and now <= self._winner_window_deadline
            ):
                self._pending_winners.append(
                    PendingWinner(
                        user_id, entry.card_no, ", ".join(p.name for p in won), self._call_index
                    )
                )
                return ClaimResult(True, None)
        return ClaimResult(False, "round_already_settled")

    # --- round creation and phases --------------------------------------

    async def _start_new_round(self) -> None:
        server_seed = secrets.token_bytes(32)
        server_seed_hash = hashlib.sha256(server_seed).hexdigest()

        async with self._pool.acquire() as conn:
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM rounds WHERE room_id = $1",
                self._room.id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO rounds
                    (room_id, seq, status, stake, house_cut_bps, server_seed_hash, lobby_deadline)
                VALUES ($1, $2, 'lobby', $3, $4, $5, now() + make_interval(secs => $6))
                RETURNING id
                """,
                self._room.id,
                seq,
                self._room.stake,
                self._room.house_cut_bps,
                server_seed_hash,
                self._room.lobby_seconds,
            )

        assert row is not None
        self._round_id = row["id"]
        self._seq = seq
        self._set_status("lobby")
        self._server_seed = server_seed
        self._server_seed_hash = server_seed_hash
        self._client_seed = None
        self._pot = Decimal("0")
        self._entries = {}
        self._called = set()
        self._draw_order = []
        self._call_index = 0
        self._locked_out = set()
        self._auto_claimed = set()
        self._dropped_this_round = set()
        self._pending_winners = []
        self._winner_window_deadline = None
        self._settlement_task = None
        self._lobby_deadline_monotonic = time.monotonic() + self._room.lobby_seconds

    async def _run_lobby(self) -> None:
        while True:
            remaining = self._lobby_deadline_monotonic - time.monotonic()
            if remaining <= 0:
                break
            if not self._lock.is_held() or self._stop_requested:
                return
            await self._publish_room(
                {
                    "t": "lobby_tick",
                    "seconds_left": max(0, round(remaining)),
                    "players": self.player_count(),
                    "cards": self.card_count(),
                    "pot": str(self._pot),
                    "derash": str(settlement.compute_derash(self._pot, self._room.house_cut_bps)[0]),
                }
            )
            await asyncio.sleep(min(1.0, remaining))

        if not self._lock.is_held() or self._stop_requested:
            return

        # Zero real players is not the same case as "some players joined
        # but not enough": nothing was ever staked, so there's nothing to
        # refund and no fairness reason to void -- and an explicit,
        # repeated product requirement is that the public room must never
        # look dead just because nobody has bought a card yet. Rather than
        # invent a separate "empty round" code path, this runs the exact
        # same _transition_to_running() a real round uses: claim()'s own
        # "not_in_round" guard already makes an empty self._entries safe
        # (nobody can ever claim a card that was never taken), and
        # _run_running()'s existing exhausted-no-winner branch already
        # handles zero entrants correctly (refund_round()'s refund loop is
        # a no-op over zero rows) -- this was already the exact path two
        # real players take when nobody happens to win, just never
        # reached before with zero players from the start.
        if self.player_count() >= self._room.min_players or self.player_count() == 0:
            await self._transition_to_running()
        else:
            round_id = self._round_id
            assert round_id is not None
            refunded_user_ids = list({user_id for user_id, _ in self._entries})
            await refunds.refund_round(self._pool, round_id, reason="lobby_underfilled")
            # A code-review pass caught this as the same plain sequential
            # for/await already fixed in _settle_with_winners() below --
            # each publish is independent (its own pool connection, its
            # own Redis channel) and runs only after refund_round()'s own
            # transaction has already committed, so sequential ordering
            # here buys nothing but delay for a room with several
            # entrants refunded at once. Concurrent, same pattern.
            await asyncio.gather(
                *(
                    ledger.publish_balance_update(self._pool, self._redis, refunded_user_id)
                    for refunded_user_id in refunded_user_ids
                )
            )
            # A real production incident, caught on video: a player who
            # took a card, then just waited (never touching the app
            # again), saw the lobby countdown hit 0 and freeze there
            # forever -- the countdown reaching 0 is a purely local
            # client computation from lobby_deadline_ms, and lobby_tick's
            # own broadcast loop above has already stopped by the time
            # this branch runs, so nothing was ever telling an already
            # -connected client this round had voided and a new one had
            # already opened (server-side, correctly, per run_forever()'s
            # own continuous-lifecycle loop) -- only a fresh "join" ever
            # produces another state_sync, and nothing prompts a player
            # sitting still to send one. Every other termination path
            # (a winner, an exhausted round) already broadcasts something
            # (round_end); this was the one silent path.
            await self._publish_room(
                {"t": "round_voided", "round_id": round_id, "reason": "lobby_underfilled"}
            )
            self._reset_to_idle()

    async def _transition_to_running(self) -> None:
        round_id = self._round_id
        assert round_id is not None
        assert self._server_seed is not None

        card_numbers = sorted(e.card_no for e in self._entries.values())
        client_seed = f"{','.join(str(c) for c in card_numbers)}-{round_id}"
        draw_order = bingo.derive_draw(self._server_seed, client_seed)

        async with self._pool.acquire() as conn:
            started = await conn.fetchval(
                "UPDATE rounds SET status = 'running', client_seed = $2, "
                "draw_order = $3, started_at = now() WHERE id = $1 AND status = 'lobby' RETURNING id",
                round_id,
                client_seed,
                draw_order,
            )
        if started is None:
            # Voided from outside this engine while its lobby ran (an admin
            # void or emergency stop). Unguarded, this UPDATE flipped the
            # voided round back to 'running', and it played out and settled:
            # winners were paid from a pot that had already been refunded
            # (platform audit, 2026-09-25; test_bingo_external_void.py).
            logger.warning("round_voided_before_start", room_id=self._room.id, round_id=round_id)
            await self._publish_room({"t": "round_voided", "round_id": round_id, "reason": "voided"})
            self._reset_to_idle()
            return

        self._set_status("running")
        self._client_seed = client_seed
        self._draw_order = draw_order
        self._running_started_at = time.monotonic()

        derash_preview, _ = settlement.compute_derash(self._pot, self._room.house_cut_bps)
        await self._publish_room(
            {
                "t": "round_start",
                "round_id": round_id,
                "seq": self._seq,
                "players": self.player_count(),
                "cards": self.card_count(),
                "pot": str(self._pot),
                "derash": str(derash_preview),
                "seed_hash": self._server_seed_hash,
            }
        )

        await self._run_running()

    async def _run_running(self) -> None:
        exhausted = False
        stopped_externally = False
        for idx in range(75):
            if self._status != "running" or not self._lock.is_held():
                break
            target = self._running_started_at + (idx + 1) * (self._room.call_interval_ms / 1000)
            now = time.monotonic()
            if target > now:
                await asyncio.sleep(target - now)
            if self._status != "running" or not self._lock.is_held():
                break
            if not await self._call_next_number():
                stopped_externally = True
                break
            if self._status != "running":
                break
        else:
            exhausted = True

        if self._settlement_task is not None:
            task, self._settlement_task = self._settlement_task, None
            await task
            return

        if stopped_externally:
            # Something outside this engine (an admin's emergency stop)
            # already voided this round and refunded its entrants in the
            # database -- confirmed by _call_next_number()'s own guard
            # returning False. This engine's own self._status/round_id
            # are now stale relative to that; resetting to idle is what
            # lets run_forever()'s outer loop safely move on (to a fresh
            # round, or to a real exit if the room was also deactivated)
            # instead of _run_lobby() being called next with leftover
            # state from a round that no longer exists in any live sense.
            logger.info(
                "run_running_stopped_externally", room_id=self._room.id, round_id=self._round_id
            )
            self._reset_to_idle()
            return

        if exhausted and self._status == "running":
            round_id = self._round_id
            assert round_id is not None
            assert self._server_seed is not None
            refunded_user_ids = list({user_id for user_id, _ in self._entries})
            await refunds.refund_round(self._pool, round_id, reason="exhausted_no_winner")
            # Same fix as the lobby-underfilled refund path above -- a
            # code-review pass caught both of this file's refund-then
            # -publish loops as the same plain sequential for/await
            # already fixed for _settle_with_winners()'s winner payouts.
            await asyncio.gather(
                *(
                    ledger.publish_balance_update(self._pool, self._redis, refunded_user_id)
                    for refunded_user_id in refunded_user_ids
                )
            )
            await self._pool.execute(
                "UPDATE rounds SET server_seed = $2 WHERE id = $1", round_id, self._server_seed
            )
            await self._publish_room(
                {
                    "t": "round_end",
                    "round_id": round_id,
                    "winners": [],
                    "derash": "0.00",
                    "server_seed": self._server_seed.hex(),
                }
            )
            self._reset_to_idle()
        # Otherwise we lost the room lock mid-round with no winner and no
        # exhaustion. Leave state as-is -- run_forever's own loop exits
        # because the lock is gone, and services/engine/recovery.py voids
        # and refunds this round the next time an engine worker starts.

    async def _call_next_number(self) -> bool:
        """Returns False if this round has been stopped out from under
        this engine by something external (an admin's emergency stop is
        the only real producer of this today) -- detected by piggy-
        backing on this method's own already-existing per-call UPDATE
        (no extra round trip): it only actually writes call_index when
        the row's status is still 'running' at that exact moment,
        RETURNING confirms whether it did. A plain, unconditional UPDATE
        here would silently keep advancing a round's call_index after an
        admin had already voided it -- harmless to the ledger (the
        refund already happened, and _settle_with_winners()'s own new
        FOR UPDATE guard is what actually prevents a double-payment
        regardless of this check), but confusing for any player still
        watching a "voided" room's screen keep calling numbers as if
        nothing happened.
        """
        round_id = self._round_id
        assert round_id is not None

        self._call_index += 1
        number = self._draw_order[self._call_index - 1]
        self._called.add(number)
        metrics.engine_calls_total.inc()

        row = await self._pool.fetchrow(
            "UPDATE rounds SET call_index = $1 WHERE id = $2 AND status = 'running' "
            "RETURNING id",
            self._call_index,
            round_id,
        )
        if row is None:
            logger.info(
                "call_next_number_stopped_round_no_longer_running",
                room_id=self._room.id,
                round_id=round_id,
            )
            return False
        await self._publish_room(
            {
                "t": "call",
                "round_id": round_id,
                "index": self._call_index,
                "number": number,
                "letter": bingo.letter_for(number),
            }
        )

        # Keep scanning every auto-mark-eligible entry for this call, even
        # after the first winner flips self._status to "settling" -- a
        # real bug a code review pass caught: returning early here meant
        # any *later* entry in this same iteration who also completes a
        # winning pattern on this exact same number (a genuine
        # simultaneous auto-mark tie) was never even offered to claim(),
        # silently losing that player's share of the derash to whichever
        # entry happened to come first in dict order. claim() itself
        # already handles this correctly on its own -- a call while
        # status is "settling" and still within WINNER_TIE_WINDOW_SECONDS
        # registers a genuine tie (the same path a second *manual* claim
        # already relies on, per test_two_simultaneous_claims_split_
        # derash_evenly); nothing here needs to short-circuit for that to
        # work, it only needs to not give up early.
        for (user_id, card_no), entry in list(self._entries.items()):
            if not entry.auto_mark:
                continue
            if (user_id, card_no) in self._auto_claimed or (user_id, card_no) in self._locked_out:
                continue
            grid = self._card_pool[entry.card_no]
            if bingo.has_won(
                grid, self._called, self._room.win_patterns,
                min_winning_lines=self._room.min_winning_lines,
            ):
                self._auto_claimed.add((user_id, card_no))
                try:
                    await self.claim(user_id, card_no, source="auto")
                except Exception:
                    # A code review pass caught this had no isolation at
                    # all, unlike _handle_command()'s own identical fix
                    # for the manual command path (see its own comment
                    # there): an unexpected exception from claim() --
                    # realistically _record_claim_attempt()'s own
                    # audit-log write, the one real DB call left
                    # unguarded in claim() -- propagated straight out of
                    # this loop, through _call_next_number(),
                    # _run_running()'s bare for loop, and run_forever()'s
                    # own while loop, killing this room's entire engine
                    # task. Nothing restarts it: the round sits stuck
                    # until a *different* engine worker starts and
                    # recovery.py's crash sweep finds it -- which VOIDS
                    # AND REFUNDS the round rather than resuming it, so
                    # the legitimate winner loses their win entirely,
                    # along with every other player in the room losing
                    # their round to a refund, over one exception.
                    # Un-claims user_id so the *next* call retries them --
                    # claim() raising means it never reached its own
                    # state-mutating section (that happens well after the
                    # one DB write that can actually fail), so nothing
                    # about the round was left inconsistent; this user's
                    # winning pattern is still exactly as valid on the
                    # next call as it was on this one.
                    self._auto_claimed.discard((user_id, card_no))
                    logger.exception(
                        "engine_auto_claim_raised",
                        room_id=self._room.id,
                        round_id=round_id,
                        user_id=user_id,
                        card_no=card_no,
                    )
        return True

    async def _finalize_after_window(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        async with self._winner_lock:
            winners = list(self._pending_winners)
            self._pending_winners = []
            self._winner_window_deadline = None

        await self._settle_with_winners(winners)

    async def _settle_with_winners(self, winners: list[PendingWinner]) -> None:
        round_id = self._round_id
        assert round_id is not None
        assert self._server_seed is not None

        pot = self._pot
        derash, house_cut = settlement.compute_derash(pot, self._room.house_cut_bps)
        shares, leftover = settlement.split_derash(derash, len(winners))
        house_total = house_cut + leftover

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Launch-readiness audit finding: this transaction used to
                # go straight to crediting winners with no check that the
                # round is still actually settleable. refund_round_in_
                # transaction() (the admin void/emergency-stop path) can
                # run concurrently against the same round_id from a
                # completely different process (the admin console, not
                # this engine) -- without a shared lock, both paths could
                # commit independently: this one paying out a "winner"
                # for a round the admin console had *already* refunded
                # everyone out of moments earlier, a real double-payment
                # (refunded AND paid) that no application-level check in
                # either code path alone would ever catch, since neither
                # one currently looks at what the other already did.
                # FOR UPDATE serializes the two against Postgres's own
                # row lock: whichever transaction reaches this row first
                # commits its terminal state, and the second one to
                # arrive -- refund_round_in_transaction() already does
                # exactly this same check -- sees that terminal status
                # and backs off instead of proceeding. This makes "stop
                # room" and "settle this round" mutually exclusive by
                # construction, not by hoping the timing never overlaps.
                round_row = await conn.fetchrow(
                    "SELECT status FROM rounds WHERE id = $1 FOR UPDATE", round_id
                )
                if round_row is None or round_row["status"] in refunds.TERMINAL_STATUSES:
                    logger.warning(
                        "settlement_aborted_round_already_terminal",
                        room_id=self._room.id,
                        round_id=round_id,
                        actual_status=round_row["status"] if round_row else None,
                    )
                    # Whoever changed this row (an admin's emergency stop,
                    # almost certainly) already published its own player-
                    # facing notification and handled the refund -- this
                    # engine's only remaining job is to stop believing
                    # it's still mid-settlement so run_forever()'s own
                    # loop can move on cleanly (to idle, and from there
                    # either a fresh round or a real exit if the room was
                    # also deactivated) instead of sitting stuck at
                    # "settling" forever with nothing left to advance it.
                    self._reset_to_idle()
                    return
                pot_account = await ledger.get_or_create_account(conn, None, "pot_escrow")
                house_account = await ledger.get_or_create_account(conn, None, "house_revenue")

                winner_cash_accounts = [
                    await ledger.get_or_create_account(conn, w.user_id, "user_cash")
                    for w in winners
                ]

                entries = [Entry(pot_account.id, -pot)]
                entries.extend(
                    Entry(acct.id, share) for acct, share in zip(winner_cash_accounts, shares)
                )
                entries.append(Entry(house_account.id, house_total))

                txn = await ledger.post(
                    conn,
                    "payout",
                    entries,
                    idempotency_key=f"settle-{round_id}",
                    round_id=round_id,
                )

                for w, share in zip(winners, shares):
                    await conn.execute(
                        """
                        INSERT INTO round_winners
                            (round_id, user_id, card_no, pattern, won_on_call, amount, payout_txn_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        round_id,
                        w.user_id,
                        w.card_no,
                        w.pattern,
                        w.call_index,
                        share,
                        txn.id,
                    )

                await conn.execute(
                    "UPDATE rounds SET status = 'done', derash = $2, server_seed = $3, "
                    "ended_at = now() WHERE id = $1",
                    round_id,
                    derash,
                    self._server_seed,
                )

                # For the result screen's own "X has won!" line (spec: a
                # winner identifier, not just a bare amount) -- every
                # *other* player in the room only ever learned a winner's
                # user_id before this, which isn't a real display value.
                # display_name is the same public-facing identity this
                # codebase already uses everywhere else a player is shown
                # to someone other than themselves (admin console, bot
                # messages) -- not new exposure, just reusing it here too.
                display_name_rows = await conn.fetch(
                    "SELECT id, display_name FROM users WHERE id = ANY($1::bigint[])",
                    [w.user_id for w in winners],
                )
                display_names = {row["id"]: row["display_name"] for row in display_name_rows}
            # Only reachable once the transaction above has actually
            # committed -- see ledger.post()'s own comment for why it
            # can't safely record this itself when called nested, which
            # every real call is.
            metrics.ledger_transactions_total.labels(kind=txn.kind).inc()

        # A code review pass caught this as a plain sequential for/await --
        # each publish is fully independent (a different user, its own
        # pool connection, its own Redis channel), so a simultaneous-tie
        # round with several winners serialized several round trips before
        # round_end could even broadcast, delaying that message for every
        # player in the room, not just the winners waiting on their own
        # balance push. Concurrent instead, the same pattern already used
        # elsewhere in this codebase for independent per-item work
        # (services/gateway/queries.py, services/bot/notification_relay.py).
        await asyncio.gather(
            *(ledger.publish_balance_update(self._pool, self._redis, w.user_id) for w in winners)
        )

        await self._publish_room(
            {
                "t": "round_end",
                "round_id": round_id,
                "winners": [
                    {
                        "user_id": w.user_id,
                        "display_name": display_names.get(w.user_id, ""),
                        "card_no": w.card_no,
                        "pattern": w.pattern,
                        "amount": str(share),
                        # The result screen renders an actual winning-card
                        # preview (spec: "Render a proper Bingo card
                        # preview," not just text) -- self._card_pool is
                        # already this engine's own in-memory source of
                        # truth for every card's grid, so this is free:
                        # no new query, no new data, just exposing what
                        # settlement already used to validate the claim.
                        "grid": self._card_pool[w.card_no],
                    }
                    for w, share in zip(winners, shares)
                ],
                "derash": str(derash),
                "server_seed": self._server_seed.hex(),
            }
        )

        self._set_status("done")
        await asyncio.sleep(self._room.result_seconds)
        self._reset_to_idle()

    async def _wait_before_next_round(self) -> None:
        """Called from run_forever()'s own loop, right after it observes
        this engine is back at "idle" (every termination path -- a winner,
        an exhausted round with none, an underfilled lobby -- funnels
        through here), before it proactively creates the next round. See
        that call site's own comment for why this needs to exist at all
        once round creation stopped being player-triggered.

        Polled in short steps (matching _run_lobby()'s own 1s granularity),
        not a single sleep, so a real stop() during a genuinely empty
        room's idle stretch is still noticed promptly rather than blocking
        shutdown.
        """
        delay_deadline = time.monotonic() + self._room.no_player_next_round_delay_seconds
        while time.monotonic() < delay_deadline:
            if self._stop_requested or not self._lock.is_held():
                return
            await asyncio.sleep(min(1.0, delay_deadline - time.monotonic()))

    def _reset_to_idle(self) -> None:
        self._round_id = None
        self._set_status("idle")
        self._server_seed = None
        self._server_seed_hash = None
        self._client_seed = None
        self._pot = Decimal("0")
        self._entries = {}
        self._called = set()
        self._draw_order = []
        self._call_index = 0
        self._locked_out = set()
        self._auto_claimed = set()
        self._dropped_this_round = set()
        self._pending_winners = []
        self._winner_window_deadline = None
        self._settlement_task = None
        self._round_active_event.clear()

    async def _record_claim_attempt(self, user_id: int, card_no: int, valid: bool) -> None:
        # card_no has a FK to cards(card_no) -- sanitized to NULL, not
        # passed through raw, for a genuinely unknown value (a client that
        # never sent one at all, still defaulted to 0 upstream; a stale or
        # malformed card_no from a buggy/malicious client). This call has
        # no try/except of its own -- it runs in claim()'s finally block,
        # unconditionally, on every attempt including ones that never even
        # found a real entry -- so an FK violation here would raise
        # straight out of claim(), with nothing isolating it (unlike the
        # auto-claim scan's own already-fixed exception handling), killing
        # this room's entire engine over a single bad claim attempt. The
        # audit trail is still valuable with card_no NULL; a crashed room
        # is not an acceptable price for logging it precisely.
        await self._pool.execute(
            "INSERT INTO claim_attempts (round_id, user_id, card_no, call_index, valid) "
            "VALUES ($1, $2, $3, $4, $5)",
            self._round_id,
            user_id,
            card_no if card_no in self._card_pool else None,
            self._call_index,
            valid,
        )

    async def _publish_room(self, message: dict[str, object]) -> None:
        await self._redis.publish(f"room:{self._room.id}", json.dumps(message))

    # --- gateway command channel -----------------------------------------
    #
    # A gateway process holds the player's WebSocket, not this engine. Every
    # action a connected player takes (join, drop, claim, toggle auto) comes
    # in over the room's Redis Stream (services/engine/commands.py) rather
    # than a direct method call, because the gateway and the engine that
    # owns this room are, in production, different processes. Exactly this
    # engine instance is the sole reader of its own room's stream, for as
    # long as it holds the room lock.

    async def _serve_commands(self) -> None:
        stream = commands.stream_key(self._room.id)
        # Resolved to a real, concrete stream id exactly once, up front --
        # deliberately NOT the special "$" sentinel re-used on every loop
        # iteration. "$" means "only entries added after *this exact call*
        # resolves it", so retrying a failed read with "$" again would
        # silently skip anything added during the retry's own backoff
        # window. A one-off xrevrange() peek converts "start from now"
        # into a fixed id before the loop ever begins; every iteration
        # after that (success or error-retry alike) advances from a real
        # id, never re-resolving "now" a second time.
        tail = await self._redis.xrevrange(stream, count=1)
        last_id: str
        if tail:
            resolved_id, _fields = tail[0]
            assert resolved_id is not None
            last_id = resolved_id if isinstance(resolved_id, str) else resolved_id.decode()
        else:
            last_id = "0"
        while True:
            try:
                response = await self._redis.xread({stream: last_id}, block=1000, count=20)
            except redis.exceptions.RedisError:
                # A launch-readiness audit found this loop had no defense
                # of its own against a transient Redis error -- unlike
                # every periodic sweep elsewhere in this codebase (payout
                # worker's _run_periodic_sweep, campaign_worker's
                # run_forever), an uncaught exception here doesn't just
                # skip one tick, it permanently kills this task for the
                # entire remaining lifetime of this engine (this method is
                # only ever started once per run_forever() call, not once
                # per round) -- silently disabling join/claim/drop_card/
                # set_auto for this room until the whole engine cycles.
                # Log and retry after a short backoff instead, the same
                # "one bad tick must not kill the loop" principle applied
                # everywhere else Redis is polled on a timer. Retrying
                # with the same last_id (not "$") is exactly what makes
                # this safe -- see the comment above.
                logger.warning("serve_commands_redis_error_retrying", room_id=self._room.id)
                await asyncio.sleep(1.0)
                continue
            if not response:
                continue
            # Plain xread() (no consumer group) always returns this shape;
            # the client library's return type is a broad union covering
            # other call patterns too, so narrow it explicitly.
            assert isinstance(response, list)
            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    last_id = entry_id
                    await self._handle_command(fields)

    async def _handle_command(self, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id")
        action = fields.get("action", "")
        try:
            user_id = int(fields.get("user_id", "0"))
        except ValueError:
            user_id = 0
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = {}

        result: JoinResult | ClaimResult
        try:
            if action == "join":
                result = await self.join(
                    user_id, payload.get("card_no", 0), auto_mark=payload.get("auto_mark", True)
                )
            elif action == "drop_card":
                result = await self.drop_card(user_id, payload.get("card_no", 0))
            elif action == "claim":
                result = await self.claim(user_id, payload.get("card_no", 0), source="manual")
            elif action == "set_auto":
                result = await self.set_auto(user_id, bool(payload.get("auto", True)))
            else:
                result = JoinResult(False, "unknown_action")
        except Exception:
            # A code review pass caught that this had no isolation at
            # all: one malformed payload or edge-case bug anywhere in
            # join()/drop_card()/claim()/set_auto() would propagate
            # straight out of _serve_commands()'s loop and kill this
            # room's single long-lived command consumer permanently (no
            # restart) -- every subsequent join/drop/claim/set_auto for
            # this room would silently time out for players (5s
            # CommandTimeout) while the round itself kept running
            # unattended. One bad command must fail that command, not
            # the room.
            logger.exception(
                "engine_command_handler_raised", room_id=self._room.id, action=action, user_id=user_id
            )
            result = JoinResult(False, "internal_error")

        if request_id:
            await self._redis.publish(
                commands.reply_channel(request_id),
                json.dumps({"ok": result.ok, "reason": result.reason, "payload": {}}),
            )
