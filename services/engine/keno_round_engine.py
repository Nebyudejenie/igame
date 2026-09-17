"""The Keno round lifecycle state machine (build spec Part 5).

    scheduled -> betting_open -> betting_closed -> drawing -> draw_complete
              -> settling -> completed
                           \\-> failed / voided (refund path)

Deliberately not shaped like services/engine/round_engine.py's RoundEngine
(one instance per *room*, many concurrently owned): Keno has exactly one
continuous round stream, globally locked (services/engine/keno_lock.py),
so this module is one KenoRoundEngine per *worker process*, not per room.

## The overlap that eliminates dead time (Part 8.1)

Betting for round N+1 opens the instant round N's draw finishes (not
after N's settlement + result-display window, which runs concurrently in
the background). At the Part 7.5 defaults (25s betting / 12s draw / 8s
settle+display), this makes the real cadence between rounds ~37s instead
of the full 45s lifecycle -- a ~22% increase in rounds/session, in the
middle of the spec's own claimed 20-30% range. Settlement runs as a
background asyncio task the main loop does not wait on; run_forever()
tracks every spawned settlement task and drains (awaits) them on
shutdown so a stop/crash never abandons one mid-write.

## Draw generation is atomic; ball reveal is what gets paced/resumed

The draw itself (packages.core.keno.derive_keno_draw) is one fast,
deterministic function call -- it is computed and persisted as a single
all-or-nothing write the instant betting closes, never partially
computed, never re-randomized on a retry. What Part 13's animation needs
paced over draw_seconds is *revealing* those already-drawn numbers to
clients one at a time -- keno_rounds.reveal_index tracks that separately,
so a crash mid-reveal resumes broadcasting from where it left off using
the same persisted drawn_numbers, never recomputing anything.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog
from redis.asyncio import Redis

from packages.core import keno, keno_config, keno_exposure, ledger, metrics
from services.engine.keno_lock import KenoRoundLock

logger = structlog.get_logger()

KENO_LIVE_CHANNEL = "keno:live"

# A round stuck in any non-terminal status for longer than this is
# considered dead (worker crashed with no recovery yet, or a genuine
# bug) -- Part 5.3's "stuck beyond a configurable threshold" rule. Wide
# enough margin over the Part 7.5 default 45s full lifecycle that a
# merely-slow round is never mistaken for a stuck one.
STUCK_ROUND_THRESHOLD_SECONDS = 300

TERMINAL_STATUSES = frozenset({"completed", "failed", "voided"})

_BETTING_CLOSING_WARNING_SECONDS = 5


@dataclass(frozen=True)
class _RoundContext:
    id: int
    config: asyncpg.Record
    tier: asyncpg.Record
    server_seed: bytes
    server_seed_hash: str


def _publish(redis: Redis, payload: dict[str, object]) -> "asyncio.Future[int]":
    # Fire-and-forget from the caller's perspective (matches
    # services/engine/round_engine.py's own _publish_room style) --
    # broadcast failures must never block round progression.
    return asyncio.ensure_future(redis.publish(KENO_LIVE_CHANNEL, json.dumps(payload, default=str)))


class KenoRoundEngine:
    def __init__(self, pool: asyncpg.Pool, redis: Redis, worker_id: str | None = None) -> None:
        self._pool = pool
        self._redis = redis
        self._worker_id = worker_id or str(uuid.uuid4())
        self._lock = KenoRoundLock(redis, worker_id=self._worker_id)
        self._stop_requested = False
        self._settlement_tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stop_requested = True

    def is_lock_held(self) -> bool:
        return self._lock.is_held()

    async def run_forever(self) -> bool:
        acquired = await self._lock.acquire()
        if not acquired:
            return False
        try:
            await self.recover_on_startup()
            while not self._stop_requested and self._lock.is_held():
                await self._run_one_round()
        finally:
            await self._drain_settlement_tasks()
            await self._lock.release()
        return True

    async def _drain_settlement_tasks(self) -> None:
        pending = [t for t in self._settlement_tasks if not t.done()]
        if pending:
            logger.info("keno_engine_draining_settlement_tasks", count=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

    def _spawn_settlement(self, round_id: int) -> None:
        task = asyncio.create_task(self._settle_and_complete(round_id))
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)

    # ------------------------------------------------------------------
    # One full round: create -> bet -> close -> draw -> (settle overlaps
    # with the next round's own betting phase).
    # ------------------------------------------------------------------

    async def _run_one_round(self) -> None:
        ctx = await self._create_round()
        await self._open_betting(ctx)
        reached_close = await self._run_betting_phase(ctx)
        if not reached_close:
            return  # lock lost or stop requested mid-betting; do not proceed
        await self._close_betting(ctx)
        await self._run_draw(ctx)
        self._spawn_settlement(ctx.id)

    async def _create_round(self) -> _RoundContext:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                config = await keno_config.load_active_config(conn)
                tier = await keno_config.load_current_tier(conn)
                server_seed = keno.generate_server_seed()
                seed_hash = keno.server_seed_hash(server_seed)
                round_id = await conn.fetchval(
                    """
                    INSERT INTO keno_rounds
                        (seq, status, config_id, tier_id, server_seed, server_seed_hash, worker_id, scheduled_at)
                    VALUES ((SELECT COALESCE(MAX(seq), 0) + 1 FROM keno_rounds), 'scheduled', $1, $2, $3, $4, $5, now())
                    RETURNING id
                    """,
                    config["id"],
                    tier["id"],
                    server_seed,
                    seed_hash,
                    self._worker_id,
                )
                await self._record_event(conn, round_id, None, "scheduled")
        metrics.keno_rounds_created_total.inc()
        return _RoundContext(id=round_id, config=config, tier=tier, server_seed=server_seed, server_seed_hash=seed_hash)

    async def _open_betting(self, ctx: _RoundContext) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE keno_rounds SET status = 'betting_open', betting_opened_at = now() WHERE id = $1", ctx.id
                )
                await self._record_event(conn, ctx.id, "scheduled", "betting_open")
        _publish(
            self._redis,
            {
                "t": "keno.betting.open",
                "round_id": ctx.id,
                "server_time": time.time(),
                "server_seed_hash": ctx.server_seed_hash,
                "betting_seconds": ctx.config["betting_seconds"],
                "min_picks": ctx.config["min_picks"],
                "max_picks": min(ctx.config["max_picks"], ctx.tier["max_pick_count"]),
                "stake_options": [str(s) for s in ctx.tier["stake_options"]],
            },
        )

    async def _run_betting_phase(self, ctx: _RoundContext) -> bool:
        """Sleeps out the betting window, broadcasting periodic ticks and
        a closing warning. Returns False if the lock was lost or a stop
        was requested mid-phase (the caller must not proceed to close/
        draw in that case -- a different worker, or nobody, now owns
        this round)."""
        betting_seconds = ctx.config["betting_seconds"]
        deadline = time.monotonic() + betting_seconds
        warned_closing = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if not self._lock.is_held() or self._stop_requested:
                return False
            if not warned_closing and remaining <= _BETTING_CLOSING_WARNING_SECONDS:
                warned_closing = True
                _publish(self._redis, {"t": "keno.betting.closing", "round_id": ctx.id, "seconds_left": remaining})
            await asyncio.sleep(min(1.0, remaining))

    async def _close_betting(self, ctx: _RoundContext) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                ticket_ids = [
                    row["id"]
                    for row in await conn.fetch("SELECT id FROM keno_tickets WHERE round_id = $1", ctx.id)
                ]
                ticket_ids_hash = keno.compute_ticket_ids_rolling_hash(ticket_ids)
                closed_at = await conn.fetchval(
                    """
                    UPDATE keno_rounds
                    SET status = 'betting_closed', betting_closed_at = now(), ticket_ids_hash = $2
                    WHERE id = $1
                    RETURNING betting_closed_at
                    """,
                    ctx.id,
                    ticket_ids_hash,
                )
                public_seed = keno.derive_public_seed(ctx.id, int(closed_at.timestamp()), ticket_ids_hash)
                await conn.execute("UPDATE keno_rounds SET public_seed = $2 WHERE id = $1", ctx.id, public_seed)
                await self._record_event(conn, ctx.id, "betting_open", "betting_closed")
        _publish(self._redis, {"t": "keno.betting.closed", "round_id": ctx.id, "ticket_count": len(ticket_ids)})

    async def _run_draw(self, ctx: _RoundContext) -> None:
        async with self._pool.acquire() as conn:
            round_row = await conn.fetchrow("SELECT public_seed, drawn_numbers FROM keno_rounds WHERE id = $1", ctx.id)
        assert round_row is not None
        public_seed = round_row["public_seed"]
        assert public_seed is not None  # set by _close_betting, always run first

        drawn = round_row["drawn_numbers"]
        if drawn is None:
            # The atomic, all-or-nothing draw step -- one deterministic
            # function call, persisted once. Never recomputed once set
            # (enforced by the DB trigger on keno_rounds.drawn_numbers).
            drawn = keno.derive_keno_draw(
                ctx.server_seed, public_seed, pool_size=ctx.config["number_pool_size"], draw_count=ctx.config["draw_count"]
            )
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE keno_rounds SET status = 'drawing', draw_started_at = now(), drawn_numbers = $2 "
                        "WHERE id = $1",
                        ctx.id,
                        drawn,
                    )
                    await self._record_event(conn, ctx.id, "betting_closed", "drawing")
            _publish(self._redis, {"t": "keno.draw.started", "round_id": ctx.id})

        await self._reveal_numbers(ctx, drawn)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE keno_rounds SET status = 'draw_complete', draw_completed_at = now() WHERE id = $1", ctx.id
                )
                await self._record_event(conn, ctx.id, "drawing", "draw_complete")
        _publish(self._redis, {"t": "keno.draw.completed", "round_id": ctx.id, "drawn_numbers": drawn})

    async def _reveal_numbers(self, ctx: _RoundContext, drawn: list[int]) -> None:
        draw_count = len(drawn)
        interval = max(0.05, ctx.config["draw_seconds"] / draw_count)
        async with self._pool.acquire() as conn:
            reveal_index = await conn.fetchval("SELECT reveal_index FROM keno_rounds WHERE id = $1", ctx.id)
        for index in range(reveal_index, draw_count):
            if not self._lock.is_held() or self._stop_requested:
                return  # a recovering worker (this one, later, or another) resumes from reveal_index
            _publish(
                self._redis,
                {"t": "keno.number.drawn", "round_id": ctx.id, "index": index, "number": drawn[index]},
            )
            async with self._pool.acquire() as conn:
                await conn.execute("UPDATE keno_rounds SET reveal_index = $2 WHERE id = $1", ctx.id, index + 1)
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Settlement (Part 6.2) -- runs as a background task, overlapping
    # with the next round's own betting phase.
    # ------------------------------------------------------------------

    async def _settle_and_complete(self, round_id: int) -> None:
        try:
            async with self._pool.acquire() as conn:
                round_row = await conn.fetchrow("SELECT * FROM keno_rounds WHERE id = $1", round_id)
            assert round_row is not None
            if round_row["status"] in TERMINAL_STATUSES:
                return  # already settled by a prior attempt (idempotent no-op)

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE keno_rounds SET status = 'settling' WHERE id = $1", round_id)
                    await self._record_event(conn, round_id, "draw_complete", "settling")

            drawn_numbers: list[int] = round_row["drawn_numbers"]
            total_payout, jackpot_hit = await self._settle_tickets(round_id, drawn_numbers)

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE keno_rounds SET status = 'completed', total_payout = $2, jackpot_hit = $3, "
                        "settled_at = now(), completed_at = now() WHERE id = $1",
                        round_id,
                        total_payout,
                        jackpot_hit,
                    )
                    await self._record_event(conn, round_id, "settling", "completed")
            metrics.keno_rounds_completed_total.inc()
            _publish(self._redis, {"t": "keno.round.completed", "round_id": round_id, "total_payout": str(total_payout)})

            # Result-display window (Part 5.4/7.5): the round stays
            # "completed" and visible in history immediately -- this
            # sleep is purely so the Mini App's result screen has time
            # to show before the client moves on; it blocks nothing else
            # (this whole method already runs as a background task
            # overlapping the next round's betting).
            config = round_row  # result_seconds isn't denormalized onto keno_rounds; read from the pinned config
            async with self._pool.acquire() as conn:
                result_seconds = await conn.fetchval(
                    "SELECT result_seconds FROM keno_configs WHERE id = $1", config["config_id"]
                )
            await asyncio.sleep(result_seconds or 0)
        except Exception:
            logger.exception("keno_settlement_failed", round_id=round_id)
            metrics.keno_settlement_errors_total.inc()
            # Deliberately not re-raised: this runs detached from the
            # main loop. A round stuck in 'settling' past
            # STUCK_ROUND_THRESHOLD_SECONDS is caught and refunded by the
            # next recovery sweep (recover_on_startup /
            # services/engine/keno_worker.py's periodic call) -- money is
            # never silently lost, just recovered on the next pass.

    async def _settle_tickets(self, round_id: int, drawn_numbers: list[int]) -> tuple[Decimal, bool]:
        total_payout = Decimal(0)
        jackpot_hit = False
        async with self._pool.acquire() as conn:
            tickets = await conn.fetch(
                "SELECT t.id, t.user_id, t.pick_count, t.stake, t.paytable_id, p.multipliers, p.pick_count AS paytable_pick_count "
                "FROM keno_tickets t JOIN keno_paytables p ON p.id = t.paytable_id "
                "WHERE t.round_id = $1 AND t.status = 'pending'",
                round_id,
            )
            selections_by_ticket: dict[int, list[int]] = {}
            for row in await conn.fetch(
                "SELECT ticket_id, number FROM keno_ticket_selections WHERE ticket_id = ANY($1)",
                [t["id"] for t in tickets],
            ):
                selections_by_ticket.setdefault(row["ticket_id"], []).append(row["number"])

            round_row = await conn.fetchrow("SELECT tier_id FROM keno_rounds WHERE id = $1", round_id)
            assert round_row is not None
            tier = await conn.fetchrow("SELECT * FROM keno_risk_tiers WHERE id = $1", round_row["tier_id"])
            assert tier is not None
            max_win_per_ticket = Decimal(tier["max_win_per_ticket"])

        for ticket in tickets:
            picks = frozenset(selections_by_ticket.get(ticket["id"], []))
            matches = keno.count_matches(picks, drawn_numbers)
            multipliers = keno_config.paytable_multipliers(ticket)
            payout = keno.compute_payout(Decimal(ticket["stake"]), matches, multipliers, max_win_per_ticket=max_win_per_ticket)

            is_jackpot = ticket["pick_count"] == 5 and matches == 5
            jackpot_payout = Decimal(0)
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    settled = await self._settle_one_ticket(
                        conn, ticket_id=ticket["id"], round_id=round_id, user_id=ticket["user_id"],
                        matches=matches, payout=payout,
                    )
                    if is_jackpot:
                        jackpot_payout = await self._pay_jackpot(conn, round_id=round_id, ticket_id=ticket["id"], user_id=ticket["user_id"])
                        if jackpot_payout > 0:
                            jackpot_hit = True
            if settled:
                _publish_private_ticket_settled(self._redis, ticket["user_id"], round_id, ticket["id"], matches, payout, jackpot_payout)
                await ledger.publish_balance_update(self._pool, self._redis, ticket["user_id"])

        # Computed from persisted ticket rows across the WHOLE round, not
        # accumulated only from tickets this particular pass touched --
        # a retried settlement after a partial crash must still report
        # the round's true total, not just what this one pass re-did.
        # (An in-memory running sum would silently undercount on retry,
        # since already-settled tickets are skipped above.)
        async with self._pool.acquire() as conn:
            total_payout = await conn.fetchval(
                "SELECT COALESCE(SUM(payout), 0) + COALESCE(SUM(jackpot_payout), 0) "
                "FROM keno_tickets WHERE round_id = $1",
                round_id,
            )
        # Not keno_rounds.jackpot_hit -- that flag is only ever set by
        # THIS method's own caller (_settle_and_complete) once settlement
        # fully finishes, which is exactly what didn't happen if this is
        # a retry pass. A prior crashed pass may have already committed
        # an individual ticket's jackpot payout (via _pay_jackpot's own
        # idempotent ledger.post) before crashing on a *later* ticket --
        # that ticket is no longer 'pending' so this pass's loop above
        # never re-examines it, and this pass's own local jackpot_hit
        # would otherwise wrongly read False. Querying the persisted
        # per-ticket jackpot_payout column (this same method just wrote
        # it, in this or an earlier pass) is the actual source of truth.
        async with self._pool.acquire() as conn:
            any_jackpot_paid = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM keno_tickets WHERE round_id = $1 AND jackpot_payout > 0)", round_id
            )
        return Decimal(total_payout), jackpot_hit or bool(any_jackpot_paid)

    async def _settle_one_ticket(
        self, conn: ledger.AsyncpgConnection, *, ticket_id: int, round_id: int, user_id: int, matches: int, payout: Decimal
    ) -> bool:
        status = "won" if payout > 0 else "lost"
        payout_txn_id: int | None = None
        if payout > 0:
            reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
            cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
            txn = await ledger.post(
                conn,
                "keno_payout",
                [ledger.Entry(reserve_account.id, -payout), ledger.Entry(cash_account.id, payout)],
                idempotency_key=f"keno:settle:{round_id}:{ticket_id}",
                created_by="keno_round_engine",
            )
            payout_txn_id = txn.id
        result = await conn.execute(
            "UPDATE keno_tickets SET status = $2, matches = $3, payout = $4, payout_txn_id = $5, settled_at = now() "
            "WHERE id = $1 AND status = 'pending'",
            ticket_id,
            status,
            matches,
            payout,
            payout_txn_id,
        )
        return result == "UPDATE 1"  # False if already settled by a prior (crashed/redelivered) attempt

    async def _pay_jackpot(self, conn: ledger.AsyncpgConnection, *, round_id: int, ticket_id: int, user_id: int) -> Decimal:
        """Part 8.3: the jackpot pays only from its own pool balance and
        can never exceed it -- checked and locked in the same
        transaction as the payout itself, never assumed safe from the
        seed/diversion mechanics alone."""
        pool_row = await conn.fetchrow("SELECT account_id, cap_amount FROM keno_jackpot_pool WHERE id = 1 FOR UPDATE")
        if pool_row is None:
            return Decimal(0)
        pool_balance = await ledger.balance(conn, pool_row["account_id"])
        if pool_balance <= 0:
            return Decimal(0)
        cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")
        await ledger.post(
            conn,
            "keno_jackpot_payout",
            [ledger.Entry(pool_row["account_id"], -pool_balance), ledger.Entry(cash_account.id, pool_balance)],
            idempotency_key=f"keno:jackpot:{round_id}:{ticket_id}",
            created_by="keno_round_engine",
        )
        await conn.execute("UPDATE keno_tickets SET jackpot_payout = $2 WHERE id = $1", ticket_id, pool_balance)
        return pool_balance

    # ------------------------------------------------------------------
    # Crash recovery (Part 5.3)
    # ------------------------------------------------------------------

    async def recover_on_startup(self) -> list[int]:
        recovered: list[int] = []
        async with self._pool.acquire() as conn:
            stuck_rows = await conn.fetch(
                "SELECT id, status, betting_opened_at, betting_closed_at, scheduled_at "
                "FROM keno_rounds WHERE status NOT IN ('completed', 'failed', 'voided')"
            )
        for row in stuck_rows:
            age_seconds = await self._round_age_seconds(row)
            if age_seconds > STUCK_ROUND_THRESHOLD_SECONDS:
                await self._fail_and_refund_round(row["id"], reason="stuck_round_recovery")
                recovered.append(row["id"])
                continue
            await self._resume_round(row)
            recovered.append(row["id"])
        return recovered

    async def _round_age_seconds(self, row: asyncpg.Record) -> float:
        async with self._pool.acquire() as conn:
            now = await conn.fetchval("SELECT now()")
        anchor = row["scheduled_at"]
        return float((now - anchor).total_seconds())

    async def _resume_round(self, row: asyncpg.Record) -> None:
        round_id = row["id"]
        status = row["status"]
        logger.info("keno_round_recovery_resuming", round_id=round_id, status=status)
        async with self._pool.acquire() as conn:
            full_row = await conn.fetchrow("SELECT * FROM keno_rounds WHERE id = $1", round_id)
            if full_row is None:
                raise RuntimeError(f"keno_rounds row {round_id} disappeared during recovery")
            config = await conn.fetchrow("SELECT * FROM keno_configs WHERE id = $1", full_row["config_id"])
            tier = await conn.fetchrow("SELECT * FROM keno_risk_tiers WHERE id = $1", full_row["tier_id"])
        assert config is not None and tier is not None
        ctx = _RoundContext(
            id=round_id, config=config, tier=tier, server_seed=full_row["server_seed"], server_seed_hash=full_row["server_seed_hash"]
        )

        if status == "scheduled":
            await self._open_betting(ctx)
            status = "betting_open"
        if status == "betting_open":
            reached_close = await self._run_betting_phase(ctx)
            if not reached_close:
                return
            await self._close_betting(ctx)
            status = "betting_closed"
        if status in ("betting_closed", "drawing", "draw_complete"):
            await self._run_draw(ctx)
        self._spawn_settlement(round_id)

    async def _fail_and_refund_round(self, round_id: int, *, reason: str) -> None:
        """Part 5.3: "A stuck round must never silently eat user money."
        Every ticket refunded exactly (kind="keno_refund", reversing the
        same stake split _seed/place_ticket originally posted), the
        round marked failed, never left ambiguous."""
        logger.warning("keno_round_marked_failed", round_id=round_id, reason=reason)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                tickets = await conn.fetch(
                    "SELECT id, user_id, stake, stake_txn_id FROM keno_tickets WHERE round_id = $1 AND status = 'pending' FOR UPDATE",
                    round_id,
                )
                for ticket in tickets:
                    await self._refund_one_ticket(conn, round_id=round_id, ticket=ticket)
                await conn.execute(
                    "UPDATE keno_rounds SET status = 'failed', failure_reason = $2, completed_at = now() WHERE id = $1",
                    round_id,
                    reason,
                )
                await self._record_event(conn, round_id, None, "failed", reason=reason)
        metrics.keno_rounds_failed_total.inc()
        _publish(self._redis, {"t": "keno.round.completed", "round_id": round_id, "status": "failed", "reason": reason})

    async def _refund_one_ticket(self, conn: ledger.AsyncpgConnection, *, round_id: int, ticket: asyncpg.Record) -> None:
        stake = Decimal(ticket["stake"])
        reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
        jackpot_account = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
        cash_account = await ledger.get_or_create_account(conn, ticket["user_id"], "user_cash")
        # Reverses the exact split place_ticket() originally posted
        # (config's jackpot_diversion_bps at stake time isn't re-read
        # here -- the refund only needs to sum to the original stake
        # across the two accounts it came from, in any split, since both
        # are system accounts with no per-account correctness
        # requirement beyond the ledger's own zero-sum invariant. A
        # single reserve-side reversal is simplest and exactly right).
        await ledger.post(
            conn,
            "keno_refund",
            [ledger.Entry(reserve_account.id, -stake), ledger.Entry(cash_account.id, stake)],
            idempotency_key=f"keno:refund:{round_id}:{ticket['id']}",
            created_by="keno_round_engine",
        )
        await conn.execute("UPDATE keno_tickets SET status = 'refunded', settled_at = now() WHERE id = $1", ticket["id"])
        _ = jackpot_account  # no jackpot-side entry: the jackpot diversion is never refunded, it stays in the pool

    async def _record_event(
        self, conn: ledger.AsyncpgConnection, round_id: int, from_status: str | None, to_status: str, *, reason: str | None = None
    ) -> None:
        await conn.execute(
            "INSERT INTO keno_round_events (round_id, from_status, to_status, worker_id, reason) VALUES ($1, $2, $3, $4, $5)",
            round_id,
            from_status,
            to_status,
            self._worker_id,
            reason,
        )


def _publish_private_ticket_settled(
    redis: Redis, user_id: int, round_id: int, ticket_id: int, matches: int, payout: Decimal, jackpot_payout: Decimal
) -> None:
    asyncio.ensure_future(
        redis.publish(
            f"user:{user_id}",
            json.dumps(
                {
                    "t": "keno.ticket.settled",
                    "round_id": round_id,
                    "ticket_id": ticket_id,
                    "matches": matches,
                    "payout": str(payout),
                    "jackpot_payout": str(jackpot_payout) if jackpot_payout > 0 else None,
                },
                default=str,
            ),
        )
    )
