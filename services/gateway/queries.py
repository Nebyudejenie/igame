"""Read-only Postgres access for the gateway.

Everything here reads from Postgres directly rather than asking a live
RoundEngine for its state. That's a deliberate choice: Postgres is already
the durable source of truth (round_engine.py writes every state transition
there before publishing anything), so `state_sync` can be served correctly
even if the room's engine just crashed and hasn't been replaced yet -- the
alternative (routing every read through the engine's command channel) would
make reconnection depend on a live engine for no real benefit.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import asyncpg

from packages.core.phone_crypto import decrypt_phone
from services.engine.settlement import compute_derash


async def get_or_create_user_by_telegram_id(
    pool: asyncpg.Pool, telegram_id: int, display_name: str
) -> int:
    """Bridges Telegram identity to our internal user id.

    In the target architecture the bot (Phase 1, not yet built) creates the
    user row during registration, before anyone ever opens the Mini App --
    this lazy get-or-create exists so the gateway is usable standalone until
    that phase lands, and becomes a pure no-op read once it does.
    """
    row = await pool.fetchrow(
        "SELECT id FROM users WHERE telegram_id = $1", telegram_id
    )
    if row is not None:
        return int(row["id"])

    row = await pool.fetchrow(
        """
        INSERT INTO users (telegram_id, display_name)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO NOTHING
        RETURNING id
        """,
        telegram_id,
        display_name,
    )
    if row is None:
        row = await pool.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1", telegram_id
        )
    assert row is not None
    return int(row["id"])


async def list_active_manual_payment_destinations(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Player-facing: only what a player choosing Manual Deposit needs to
    see (which account to pay into, and the instructions) -- no admin
    bookkeeping columns (created_by_admin_id, timestamps), and only rows
    an admin has actually left active.
    """
    rows = await pool.fetch(
        "SELECT id, method_kind, account_ref, account_name, instructions "
        "FROM manual_payment_destinations WHERE is_active ORDER BY method_kind, id"
    )
    return [dict(r) for r in rows]


async def user_phone(pool: asyncpg.Pool, user_id: int) -> str | None:
    blob = await pool.fetchval("SELECT phone_e164_encrypted FROM users WHERE id = $1", user_id)
    return decrypt_phone(bytes(blob)) if blob is not None else None


async def get_user_language(pool: asyncpg.Pool, user_id: int) -> str:
    value = await pool.fetchval("SELECT language FROM users WHERE id = $1", user_id)
    return str(value) if value is not None else "am"


async def get_auto_mark_preference(pool: asyncpg.Pool, user_id: int) -> bool:
    value = await pool.fetchval(
        "SELECT auto_mark_preference FROM users WHERE id = $1", user_id
    )
    return bool(value) if value is not None else True


async def set_auto_mark_preference(pool: asyncpg.Pool, user_id: int, auto: bool) -> None:
    await pool.execute(
        "UPDATE users SET auto_mark_preference = $1 WHERE id = $2", auto, user_id
    )


async def invite_summary(pool: asyncpg.Pool, user_id: int) -> dict[str, Any]:
    """The same two facts services/bot/handlers.py::cmd_invite() already
    sends over chat (the deep link's telegram_id and how many people have
    joined through it) -- surfaced here so the Mini App can offer the same
    invite, one tap, without a player leaving the game for the bot chat.
    `referred_by` is an internal users.id (see 81d041ff4513_ledger_
    foundation.py), the same column cmd_invite() counts against.
    """
    row = await pool.fetchrow("SELECT telegram_id FROM users WHERE id = $1", user_id)
    telegram_id = int(row["telegram_id"]) if row is not None else 0
    referral_count = await pool.fetchval(
        "SELECT count(*) FROM users WHERE referred_by = $1", user_id
    )
    return {"telegram_id": telegram_id, "referral_count": int(referral_count)}


async def held_card_no_for_room(pool: asyncpg.Pool, room_id: int, user_id: int) -> int | None:
    """Resolves "the" card a user holds in a room's current round, for a
    drop_card/claim frame that didn't explicitly say which card (every
    Mini App build before multi-card support, which never sends card_no at
    all). Picks the lowest card_no if the user somehow holds more than
    one -- deliberately arbitrary but deterministic, since an old client
    genuinely can't express "which one I mean" and this only matters until
    every client is upgraded to always send card_no explicitly.
    """
    value = await pool.fetchval(
        """
        SELECT re.card_no
        FROM round_entries re
        JOIN rounds r ON r.id = re.round_id
        WHERE r.room_id = $1 AND re.user_id = $2 AND r.status NOT IN ('done', 'voided')
        ORDER BY re.card_no
        LIMIT 1
        """,
        room_id,
        user_id,
    )
    return int(value) if value is not None else None


async def held_card_no_for_round(pool: asyncpg.Pool, round_id: int, user_id: int) -> int | None:
    """Same resolution as held_card_no_for_room(), keyed by round_id
    instead of room_id -- claim frames carry round_id, not room_id."""
    value = await pool.fetchval(
        "SELECT card_no FROM round_entries WHERE round_id = $1 AND user_id = $2 ORDER BY card_no LIMIT 1",
        round_id,
        user_id,
    )
    return int(value) if value is not None else None


async def user_history(pool: asyncpg.Pool, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    # One row per *round* the user took part in, not one row per card --
    # a player can hold several cards in the same round now. The old
    # LEFT JOIN matched only (round_id, user_id), so a user holding N
    # cards in a round with M of their own winning cards produced N*M
    # result rows (every entry row joined against every one of that
    # user's winner rows in the round, not just its own card's) -- a real
    # multiplicative blowup, not just cosmetic duplication. Scoping the
    # join to the entry's own card_no makes each entry match at most one
    # winner row; grouping by round and summing amount mirrors
    # app.v6.js's own round_end fix (a player who won on two of their own
    # cards in one round shows one round with the combined amount, not
    # two rows).
    rows = await pool.fetch(
        """
        SELECT rd.id, rd.seq, rd.stake, rd.ended_at,
               count(rw.round_id) > 0 AS won,
               sum(rw.amount) AS won_amount
        FROM round_entries re
        JOIN rounds rd ON rd.id = re.round_id
        LEFT JOIN round_winners rw
            ON rw.round_id = re.round_id AND rw.user_id = re.user_id AND rw.card_no = re.card_no
        WHERE re.user_id = $1 AND rd.status IN ('done', 'voided')
        GROUP BY rd.id, rd.seq, rd.stake, rd.ended_at
        ORDER BY rd.ended_at DESC NULLS LAST
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [
        {
            "round_id": row["id"],
            "seq": row["seq"],
            "stake": str(row["stake"]),
            "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
            "won": row["won"],
            "won_amount": str(row["won_amount"]) if row["won_amount"] is not None else None,
        }
        for row in rows
    ]


async def list_rooms(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT
            r.id, r.code, r.stake, r.max_players,
            latest.status, latest.pot, latest.house_cut_bps, latest.lobby_deadline,
            (SELECT count(DISTINCT user_id) FROM round_entries
             WHERE round_id = latest.id) AS distinct_players
        FROM rooms r
        LEFT JOIN LATERAL (
            SELECT id, status, pot, house_cut_bps, lobby_deadline
            FROM rounds
            WHERE room_id = r.id
            ORDER BY seq DESC
            LIMIT 1
        ) latest ON true
        WHERE r.is_active = true
        ORDER BY r.stake
        """
    )
    out = []
    for row in rows:
        pot = row["pot"] if row["pot"] is not None else Decimal("0.00")
        # Only meaningful once a round is actually running (past the lobby
        # cutoff, per explicit request -- before that the pot is still
        # filling and showing a derash figure at all would be premature).
        # The Mini App only ever renders this for status == "running", but
        # it's computed for every row the same live way build_state_sync()
        # and round_engine.py itself do (settlement.compute_derash()), not
        # a stale/never-written column -- see this same fix already
        # applied there.
        derash = (
            compute_derash(pot, row["house_cut_bps"])[0]
            if row["house_cut_bps"] is not None
            else Decimal("0.00")
        )
        out.append(
            {
                "room_id": row["id"],
                "code": row["code"],
                "stake": str(row["stake"]),
                "max_players": row["max_players"],
                "status": row["status"] or "idle",
                "players": row["distinct_players"] or 0,
                "pot": str(pot),
                "derash": str(derash),
                # Mini App spec 2.1: the room list's own countdown ("0:18")
                # for a room still in its lobby -- the SQL above already
                # selected this column, but it was silently dropped here
                # before ever reaching a client, leaving the Mini App with
                # no real deadline to count down to (an architecture audit
                # found this).
                "lobby_deadline_ms": (
                    int(row["lobby_deadline"].timestamp() * 1000)
                    if row["lobby_deadline"] is not None
                    else None
                ),
            }
        )
    return out


async def build_state_sync(pool: asyncpg.Pool, room_id: int, user_id: int) -> dict[str, Any]:
    """The one-message reconnect payload (spec section 6.5): everything the
    Mini App needs to redraw exactly where the player left off, sourced
    entirely from Postgres so it works even if the room's engine just
    crashed and hasn't been replaced yet.
    """
    # A code review pass caught these two queries running sequentially --
    # neither depends on the other's result (both filter on the same
    # room_id, nothing more), so this reconnect payload's latency was
    # paying for two round trips end to end where one round trip's worth
    # (the slower of the two) would do. Genuinely independent reads,
    # unlike the get_or_create_account chains elsewhere in this codebase
    # that lock rows in a specific order for a reason -- there's no
    # ordering constraint to preserve here.
    room_row, round_row, card_pool_size = await asyncio.gather(
        pool.fetchrow(
            "SELECT stake, win_patterns, min_winning_lines, max_players, max_cards_per_player "
            "FROM rooms WHERE id = $1",
            room_id,
        ),
        pool.fetchrow(
            """
            SELECT id, status, call_index, draw_order, pot, derash, house_cut_bps,
                   stake, lobby_deadline,
                   (SELECT count(DISTINCT user_id) FROM round_entries
                    WHERE round_id = rounds.id) AS distinct_players
            FROM rounds
            WHERE room_id = $1
            ORDER BY seq DESC
            LIMIT 1
            """,
            room_id,
        ),
        # The one authoritative source for the card-selection range, not a
        # duplicated literal -- a real production incident: the frontend
        # had its own hardcoded "1..150" (and, briefly, a hand-edited
        # "1..432" after a video re-analysis corrected the pool size), a
        # separate number from packages/core/bingo.py's own _POOL_SIZE and
        # from whatever the cards table actually had seeded, with nothing
        # forcing them to agree. max(card_no) over the real seeded rows is
        # the actual ground truth (what a player can really be dealt),
        # not the Python constant that produced it -- if the pool is ever
        # widened again, every client picks up the new range automatically
        # the next time it syncs, no frontend deploy required.
        pool.fetchval("SELECT max(card_no) FROM cards"),
    )
    if room_row is None:
        raise ValueError(f"no such room: {room_id}")
    win_patterns = room_row["win_patterns"]
    if isinstance(win_patterns, str):
        win_patterns = json.loads(win_patterns)

    called: list[int] = []
    your_card: int | None = None
    your_card_grid: list[list[int]] | None = None
    auto_mark: bool | None = None
    your_cards: list[dict[str, Any]] = []
    taken_cards: list[int] = []
    status = "idle"
    round_id = None
    call_index = 0
    pot = Decimal("0")
    derash = Decimal("0")
    players = 0
    stake = room_row["stake"]
    lobby_deadline_ms: int | None = None

    # A terminal round (voided/done) is treated exactly like "no round at
    # all" here -- a real production incident: RoundEngine._reset_to_idle()
    # already flips the *in-memory* engine back to "idle" the instant a
    # round voids or finishes settling (round_engine.py), but this query
    # reads Postgres, where that same round's row keeps its terminal status
    # forever until a brand-new round is inserted. The only thing that ever
    # inserts one is RoundEngine.join() (via a take_card), which only runs
    # once a player is actually looking at the lobby/card-grid screen --
    # and app.v6.js's own state_sync handler routes "voided"/"done" straight
    # back to the room list, never to the lobby. Left as `round_row["status"]`,
    # every room went permanently unplayable the moment its first-ever round
    # ended: nobody could ever reach the lobby again to take a card, so no
    # new round could ever be created, forever -- not caught earlier because
    # no round had ever actually finished end-to-end against a real client
    # until this session's has_main_web_app fix let one complete for the
    # first time.
    round_is_terminal = round_row is not None and round_row["status"] in ("voided", "done")
    if round_row is not None and not round_is_terminal:
        status = round_row["status"]
        round_id = round_row["id"]
        call_index = round_row["call_index"]
        pot = round_row["pot"]
        # rounds.derash is only ever written at settlement (round_engine.py's
        # own UPDATE ... SET status = 'done', derash = $2 ...) -- for any
        # round still in "lobby" or "running" (the only statuses reachable
        # here; terminal rounds are treated as "no round" above), that
        # column is always NULL, so reading it directly made this reconnect
        # payload show a hardcoded 0.00 derash for the entire life of every
        # round regardless of the real pot. A live player connected exactly
        # when round_engine.py's own round_start/lobby_tick broadcasts fired
        # saw the correct value (those compute it fresh from the live pot);
        # anyone who reconnected, refreshed, or joined mid-round instead saw
        # 0.00 -- the exact "sometimes right, sometimes 0" behavior this
        # fixes by computing it the same way round_engine.py already does,
        # live from the real pot, instead of reading the not-yet-written
        # column.
        derash, _ = compute_derash(pot, round_row["house_cut_bps"])
        players = round_row["distinct_players"] or 0
        stake = round_row["stake"]
        draw_order = round_row["draw_order"] or []
        called = list(draw_order[:call_index])
        if round_row["lobby_deadline"] is not None:
            lobby_deadline_ms = int(round_row["lobby_deadline"].timestamp() * 1000)

        # A production gap, caught live: a player who joins after someone
        # else already took a card saw that card as still available --
        # taken_cards was never part of this reconnect payload, only ever
        # learned from a live card_taken broadcast, which a client that
        # joins *after* the take happened was never subscribed to receive.
        # A second player's tap on an already-taken card was always
        # correctly rejected server-side (join()'s own PRIMARY KEY on
        # round_entries(round_id, card_no) never let two owners share a
        # card), but the client had no way to show it as taken up front.
        all_entries = await pool.fetch(
            "SELECT user_id, card_no, auto_mark FROM round_entries WHERE round_id = $1 ORDER BY card_no",
            round_id,
        )
        taken_cards = [e["card_no"] for e in all_entries]

        # A player can hold more than one card in the same round now --
        # ordered by card_no for a deterministic "first" one. your_cards
        # is the real, complete list; your_card/your_card_grid/auto_mark
        # below stay populated from that first card too, purely so every
        # Mini App build that predates multi-card support (which only
        # ever reads those three singular fields) keeps working exactly
        # as before without needing to ship at the same time as this.
        entries = [e for e in all_entries if e["user_id"] == user_id]
        if entries:
            card_nos = [e["card_no"] for e in entries]
            grid_rows = await pool.fetch(
                "SELECT card_no, grid FROM cards WHERE card_no = ANY($1::smallint[])", card_nos
            )
            grids_by_card_no = {
                row["card_no"]: (
                    json.loads(row["grid"]) if isinstance(row["grid"], str) else row["grid"]
                )
                for row in grid_rows
            }
            your_cards = [
                {
                    "card_no": e["card_no"],
                    "grid": grids_by_card_no.get(e["card_no"]),
                    "auto_mark": e["auto_mark"],
                }
                for e in entries
            ]
            your_card = entries[0]["card_no"]
            auto_mark = entries[0]["auto_mark"]
            your_card_grid = grids_by_card_no.get(your_card)

    return {
        "t": "state_sync",
        "room_id": room_id,
        "round_id": round_id,
        "status": status,
        "call_index": call_index,
        "called": called,
        "pot": str(pot),
        "derash": str(derash),
        "players": players,
        "stake": str(stake),
        "win_patterns": win_patterns,
        "min_winning_lines": room_row["min_winning_lines"],
        "your_card": your_card,
        "your_card_grid": your_card_grid,
        "auto_mark": auto_mark,
        "your_cards": your_cards,
        "taken_cards": taken_cards,
        "card_pool_size": card_pool_size,
        "max_cards_per_player": room_row["max_cards_per_player"],
        "lobby_deadline_ms": lobby_deadline_ms,
        "server_time": int(time.time() * 1000),
    }
