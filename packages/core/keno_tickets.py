"""Keno ticket placement and settlement -- the money-moving core.

Deliberately stateless / directly-DB-transactional (unlike Bingo's
RoundEngine.join(), which needs in-memory per-room state because many
concurrently-owned rooms exist). Keno has exactly one global round
stream, and "is betting still open for round X" is answered correctly
by a single row lock on keno_rounds -- no in-process engine state is
needed for correctness, so ticket placement can run directly from
whichever gateway/API process receives the request, the same way
services/payments/deposits.py and withdrawals.py are plain async
functions callable from any process holding a pool. See
docs/keno/00-discovery.md section G for the full reasoning.

Every function here goes through packages/core/ledger.py -- there is no
second wallet, no direct account_balances write anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg
import structlog

from packages.core import keno, keno_config, keno_exposure, ledger, metrics, responsible_gaming

logger = structlog.get_logger()


class TicketRejected(Exception):
    """Base for every typed Keno bet-placement rejection. Never a bare
    500 -- see Part 3.3: "Every rejection is a typed error code the
    frontend can translate -- never a raw 500, never a silent reduction
    of payout." """

    code = "ticket_rejected"


class KenoDisabled(TicketRejected):
    code = "keno_disabled"


class NotOnBetaAllowlist(TicketRejected):
    """A staged launch's allowlist (2026-09-23, spec-adjacent -- a CTO
    review's own required Stage 1/2 gate) is still restricting access
    and this user isn't on it. Distinct from KenoDisabled: keno_enabled
    can be true (the game is genuinely running, real rounds are
    settling) while a specific user is still not allowed in yet -- the
    two states are independently meaningful for anyone reading the
    audit trail or debugging a rejection."""

    code = "keno_not_on_allowlist"


class RoundNotAcceptingBets(TicketRejected):
    code = "round_not_accepting_bets"


class InvalidPicks(TicketRejected):
    code = "invalid_picks"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StakeNotAllowed(TicketRejected):
    code = "stake_not_allowed"


class PickCountNotAllowedAtTier(TicketRejected):
    code = "pick_count_not_allowed_at_tier"


class TooManyTicketsThisRound(TicketRejected):
    code = "too_many_tickets_this_round"


class RoundCapacityReached(TicketRejected):
    code = "round_capacity_reached"


class UserRoundShareExceeded(TicketRejected):
    code = "user_round_share_exceeded"


class UnknownUser(TicketRejected):
    code = "unknown_user"


class InsufficientBalance(TicketRejected):
    code = "insufficient_balance"


class AutoplaySessionNotActive(TicketRejected):
    """An autoplay placement for a session the player has already stopped
    (or that ended) since the round's session list was read."""

    code = "autoplay_session_not_active"


class SimulatedPlayerCannotPlaceKenoTicket(TicketRejected):
    """Same defense-in-depth as services/payments/withdrawals.py's
    SimulatedPlayerCannotWithdraw -- a house-float-funded simulated
    player's balance must never be extractable as a real Keno win
    either. Nothing currently drives a simulated player into Keno, but
    the check costs nothing and closes the door structurally rather than
    by omission."""

    # Matches web/miniapp/js/keno.js's own ERROR_KEYS set and the
    # keno.error.simulated_player i18n key exactly -- the gateway used to
    # map this one exception type to this shorter string via a separate
    # dict (services/gateway/app.py's own now-removed
    # _KENO_TICKET_ERROR_CODES); reading .code directly off the instance
    # now, so the two need to agree here instead.
    code = "simulated_player"


class ResponsibleGamingBlock(TicketRejected):
    """packages/core/responsible_gaming.py's own PlayBlock.reason, one
    of 'self_excluded' | 'banned' | 'cooling_off' | 'loss_limit_reached'
    -- a spec-compliance audit (2026-09-21) found Keno never checked this
    at all, unlike Bingo's round_engine.py::join(). The daily loss cap
    this feeds is combined across both games (today_net_loss() sums both
    games' own stake/payout kinds together), not tracked separately."""

    def __init__(self, reason: str) -> None:
        self.code = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PlacedTicket:
    id: int
    round_id: int
    user_id: int
    picks: frozenset[int]
    stake: Decimal
    idempotency_key: str
    status: str


async def place_ticket(
    pool: asyncpg.Pool,
    redis: object,
    *,
    user_id: int,
    picks: list[int],
    stake: Decimal,
    idempotency_key: str,
    autoplay_session_id: int | None = None,
) -> PlacedTicket:
    """Public entry point: delegates to _place_ticket, incrementing
    keno_bet_rejections_total{reason} on any typed rejection (Part 16)
    before re-raising -- one choke point rather than an increment at
    every individual raise site in _place_ticket below."""
    try:
        return await _place_ticket(
            pool, redis, user_id=user_id, picks=picks, stake=stake, idempotency_key=idempotency_key,
            autoplay_session_id=autoplay_session_id,
        )
    except TicketRejected as exc:
        metrics.keno_bet_rejections_total.labels(reason=exc.code).inc()
        raise


async def _place_ticket(
    pool: asyncpg.Pool,
    redis: object,  # redis.asyncio.Redis -- typed loosely to avoid a hard import cycle here; see services callers
    *,
    user_id: int,
    picks: list[int],
    stake: Decimal,
    idempotency_key: str,
    autoplay_session_id: int | None = None,
) -> PlacedTicket:
    """Part 6.1's bet-placement transaction, in full:

        BEGIN
          validate ticket (picks in range, no duplicates, count within min/max, stake in allowed set)
          re-read round with lock; assert state = BETTING_OPEN
          validate risk tier, per-user limits, round exposure capacity
          lock user wallet row (SELECT ... FOR UPDATE)
          assert balance >= stake
          insert ledger debit (type: keno_stake, idempotency_key: client_request_id)
          update balance
          insert ticket + selections
        COMMIT

    Returns the existing ticket unchanged on a replayed idempotency_key
    (Part 6.1: "A retry returns the original ticket") rather than
    re-validating/re-charging.
    """
    # Pure validation first, before any connection is even acquired --
    # mirrors services/payments/deposits.py's own
    # _check_deposit_rate_limit_and_minimum() shape.
    try:
        validated_picks = keno.validate_picks(picks)
    except keno.KenoError as exc:
        raise InvalidPicks(str(exc)) from exc

    # The caller's key (a player-chosen string from the Mini App, or
    # autoplay's own) is namespaced to this player before it goes anywhere
    # near the ledger. ledger_transactions.idempotency_key is one global
    # namespace across every kind, and a raw player string there let a
    # player reuse any existing key for a ticket with no stake taken, claim
    # a key another operation hadn't posted yet (a Bingo settlement, a
    # deposit) so it silently moved no money, or read back another player's
    # ticket by sending their key (platform audit, 2026-09-25;
    # test_ledger_key_isolation.py). Stored namespaced in keno_tickets too,
    # which scopes the replay lookup below to this player.
    ledger_key = f"keno:stake:{user_id}:{idempotency_key}"

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Idempotency short-circuit -- a genuine retry (client
            # timeout, double-tap) returns the original ticket rather
            # than re-validating/re-charging. Checked first, before any
            # lock is taken, since a replay should be cheap.
            replay = await _replayed_ticket(conn, ledger_key, validated_picks, idempotency_key)
            if replay is not None:
                return replay

            # Same pg_advisory_xact_lock(user_id) round_engine.py's own
            # join() takes, and for the identical reason (its own
            # comment): without a lock held for this whole transaction,
            # a Bingo join and a Keno ticket racing for the same user
            # could both read today's net loss before either stake
            # commits, letting both pass a daily_loss_cap that either one
            # alone would have blocked. The lock is keyed only on
            # user_id, not per-game, so it correctly serializes against
            # Bingo's own call too -- pg_advisory_xact_lock is global to
            # the whole Postgres instance, not scoped to a table.
            #
            # Taken first, before any row lock, in the same order join()
            # takes it. join() holds this lock while inserting its
            # round_entries row, whose user_id foreign key needs FOR KEY
            # SHARE on the users row; had this transaction already locked
            # that row, each would wait on the other (a real deadlock,
            # test_responsible_gaming_paths.py).
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            # FOR NO KEY UPDATE, not FOR UPDATE: it still serializes
            # against another ticket for this user and against a status
            # change (a ban or self-exclusion is a non-key UPDATE), but
            # doesn't block the FOR KEY SHARE every foreign-key insert
            # referencing this user takes. FOR UPDATE did, which deadlocked
            # a Keno ticket against Bingo settlement paying the same player
            # (payout post locks the balance row, then round_winners'
            # foreign key wants this row).
            user_row = await conn.fetchrow(
                "SELECT id, status, is_simulated FROM users WHERE id = $1 FOR NO KEY UPDATE", user_id
            )
            if user_row is None:
                raise UnknownUser(str(user_id))
            if user_row["is_simulated"]:
                raise SimulatedPlayerCannotPlaceKenoTicket(str(user_id))

            # Checked again now that this player's lock is held: two requests
            # with the same key at once (a double-tap) could both pass the
            # unlocked lookup above, and the second then hit the
            # keno_tickets unique index as a raw 500 instead of getting the
            # first one's ticket back (platform audit, 2026-09-25).
            replay = await _replayed_ticket(conn, ledger_key, validated_picks, idempotency_key)
            if replay is not None:
                return replay

            # An autoplay placement re-checks its own session under a row
            # lock: place_for_active_sessions() read the session list before
            # this round's placements started, so a player who pressed Stop
            # in the meantime would otherwise still be charged for this
            # round. stop_session()'s UPDATE takes the same row lock, so
            # whichever commits first wins cleanly.
            if autoplay_session_id is not None:
                session_status = await conn.fetchval(
                    "SELECT status FROM keno_autoplay_sessions WHERE id = $1 FOR NO KEY UPDATE", autoplay_session_id
                )
                if session_status != "active":
                    raise AutoplaySessionNotActive(str(autoplay_session_id))

            block = await responsible_gaming.check_stake_allowed(conn, user_id, stake)
            if block.blocked:
                assert block.reason is not None
                raise ResponsibleGamingBlock(block.reason)

            config = await keno_config.load_active_config(conn)
            if not config["keno_enabled"]:
                raise KenoDisabled()
            if not await keno_config.is_user_allowed_to_play(conn, user_id, config):
                raise NotOnBetaAllowlist()

            round_row = await conn.fetchrow(
                "SELECT id, status, tier_id, config_id, total_stake, ticket_count "
                "FROM keno_rounds WHERE status = 'betting_open' ORDER BY id DESC LIMIT 1 FOR UPDATE"
            )
            if round_row is None:
                raise RoundNotAcceptingBets()
            if round_row["status"] != "betting_open":
                raise RoundNotAcceptingBets()
            round_id = round_row["id"]

            tier = await conn.fetchrow("SELECT * FROM keno_risk_tiers WHERE id = $1", round_row["tier_id"])
            if tier is None:
                raise RuntimeError(f"keno_rounds.tier_id {round_row['tier_id']} has no matching keno_risk_tiers row")
            pick_count = len(validated_picks)
            if pick_count > tier["max_pick_count"]:
                raise PickCountNotAllowedAtTier(str(pick_count))
            if stake not in set(tier["stake_options"]):
                raise StakeNotAllowed(str(stake))

            # A configured tier/pick-count combination with no matching
            # paytable is an admin configuration gap, not a player-facing
            # rejection reason -- keno_config.load_active_paytable raises
            # RuntimeError, failing loudly rather than silently letting
            # an unpriced ticket through.
            paytable = await keno_config.load_active_paytable(conn, pick_count, tier["paytable_profile"])

            existing_tickets_count = await conn.fetchval(
                "SELECT count(*) FROM keno_tickets WHERE round_id = $1 AND user_id = $2", round_id, user_id
            )
            if existing_tickets_count >= config["max_tickets_per_user_per_round"]:
                raise TooManyTicketsThisRound(str(existing_tickets_count))

            reserve_balance = await _keno_reserve_balance(conn)
            multipliers = keno_config.paytable_multipliers(paytable)
            ticket_risk = keno_exposure.compute_ticket_risk_contribution(pick_count, multipliers, stake=stake)

            round_accumulators = await conn.fetchrow(
                "SELECT total_expected_payout, total_payout_variance FROM keno_rounds WHERE id = $1 FOR UPDATE",
                round_id,
            )
            assert round_accumulators is not None  # already row-locked above in this same transaction
            exposure_check = keno_exposure.check_round_exposure(
                current_total_expected_payout=Decimal(round_accumulators["total_expected_payout"]),
                current_total_payout_variance=Decimal(round_accumulators["total_payout_variance"]),
                current_total_stake=Decimal(round_row["total_stake"]),
                new_ticket=ticket_risk,
                new_ticket_stake=stake,
                reserve_balance=reserve_balance,
                max_round_exposure_pct=tier["max_round_exposure_pct"],
                jackpot_diversion_bps=config["jackpot_diversion_bps"],
            )
            if not exposure_check.allowed:
                raise RoundCapacityReached(str(exposure_check.projected_exposure))
            # A real, previously-dead Prometheus gauge (spec 10.4's own
            # "no liability-near-cash alert, no exposure>80% alert" gap
            # traces back to this never being .set() anywhere) -- ceiling
            # is reserve_balance * max_round_exposure_pct, genuinely zero
            # before the reserve is ever funded, so guarded rather than a
            # ZeroDivisionError taking down a real ticket placement over a
            # metrics update.
            if exposure_check.ceiling > 0:
                metrics.keno_round_exposure_ratio.set(float(exposure_check.projected_exposure / exposure_check.ceiling))

            user_accumulators = await conn.fetchrow(
                "SELECT COALESCE(SUM(expected_payout_contribution), 0) AS expected, "
                "COALESCE(SUM(payout_variance_contribution), 0) AS variance "
                "FROM keno_tickets WHERE round_id = $1 AND user_id = $2",
                round_id,
                user_id,
            )
            assert user_accumulators is not None  # COALESCE guarantees a row even with zero matching tickets
            if not keno_exposure.check_per_user_round_share(
                user_expected_payout_this_round=Decimal(user_accumulators["expected"]),
                user_payout_variance_this_round=Decimal(user_accumulators["variance"]),
                new_ticket=ticket_risk,
                round_exposure_ceiling=exposure_check.ceiling,
                max_share_bps=config["per_user_round_capacity_share_bps"],
            ):
                raise UserRoundShareExceeded(str(stake))

            # Money movement: debit user_cash, split the stake between
            # keno_reserve and keno_jackpot_pool per the configured
            # diversion (Part 8.3 -- taken from the payout budget, not
            # added to player cost; the paytable's own RTP already
            # assumes this slice is diverted). One ledger.post() call,
            # still sums to zero.
            reserve_account = await ledger.get_or_create_account(conn, None, "keno_reserve")
            jackpot_account = await ledger.get_or_create_account(conn, None, "keno_jackpot_pool")
            cash_account = await ledger.get_or_create_account(conn, user_id, "user_cash")

            jackpot_cut = (stake * Decimal(config["jackpot_diversion_bps"]) / Decimal(10000)).quantize(Decimal("0.01"))
            reserve_cut = stake - jackpot_cut

            try:
                stake_txn = await ledger.post(
                    conn,
                    "keno_stake",
                    [
                        ledger.Entry(cash_account.id, -stake),
                        ledger.Entry(reserve_account.id, reserve_cut),
                        ledger.Entry(jackpot_account.id, jackpot_cut),
                    ],
                    idempotency_key=ledger_key,
                    created_by="keno_tickets.place_ticket",
                )
            except ledger.InsufficientFunds as exc:
                # Pre-existing bug this pass caught (not introduced by
                # it): raising the bare TicketRejected base class with a
                # message only ever set __str__, never .code -- every
                # caller reading exc.code (services/gateway/app.py's own
                # HTTPException(detail=exc.code), and now
                # keno_autoplay.place_for_active_sessions()'s stop_reason)
                # got the generic "ticket_rejected" fallback instead of
                # this specific, more useful reason.
                raise InsufficientBalance() from exc

            ticket_id = await conn.fetchval(
                """
                INSERT INTO keno_tickets
                    (round_id, user_id, paytable_id, pick_count, stake,
                     expected_payout_contribution, payout_variance_contribution,
                     status, idempotency_key, stake_txn_id, autoplay_session_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, $10)
                RETURNING id
                """,
                round_id,
                user_id,
                paytable["id"],
                pick_count,
                stake,
                ticket_risk.expected_payout,
                ticket_risk.payout_variance,
                ledger_key,
                stake_txn.id,
                autoplay_session_id,
            )
            await conn.executemany(
                "INSERT INTO keno_ticket_selections (ticket_id, number) VALUES ($1, $2)",
                [(ticket_id, number) for number in validated_picks],
            )
            await conn.execute(
                "UPDATE keno_rounds SET total_stake = total_stake + $1, ticket_count = ticket_count + 1, "
                "total_expected_payout = total_expected_payout + $2, "
                "total_payout_variance = total_payout_variance + $3, "
                "projected_exposure = $4 WHERE id = $5",
                stake,
                ticket_risk.expected_payout,
                ticket_risk.payout_variance,
                exposure_check.projected_exposure,
                round_id,
            )

            logger.info(
                "keno_ticket_placed",
                ticket_id=ticket_id,
                round_id=round_id,
                user_id=user_id,
                pick_count=pick_count,
                stake=str(stake),
            )
            return PlacedTicket(
                id=ticket_id,
                round_id=round_id,
                user_id=user_id,
                picks=validated_picks,
                stake=stake,
                idempotency_key=idempotency_key,
                status="pending",
            )


async def _replayed_ticket(
    conn: ledger.AsyncpgConnection, ledger_key: str, picks: frozenset[int], client_key: str
) -> PlacedTicket | None:
    existing = await conn.fetchrow(
        "SELECT id, round_id, user_id, stake, status FROM keno_tickets WHERE idempotency_key = $1", ledger_key
    )
    if existing is None:
        return None
    return PlacedTicket(
        id=existing["id"],
        round_id=existing["round_id"],
        user_id=existing["user_id"],
        picks=picks,
        stake=existing["stake"],
        idempotency_key=client_key,
        status=existing["status"],
    )


async def _keno_reserve_balance(conn: ledger.AsyncpgConnection) -> Decimal:
    account = await ledger.get_or_create_account(conn, None, "keno_reserve")
    return await ledger.balance(conn, account.id)
