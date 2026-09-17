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

from packages.core import keno, keno_config, keno_exposure, ledger, metrics

logger = structlog.get_logger()


class TicketRejected(Exception):
    """Base for every typed Keno bet-placement rejection. Never a bare
    500 -- see Part 3.3: "Every rejection is a typed error code the
    frontend can translate -- never a raw 500, never a silent reduction
    of payout." """

    code = "ticket_rejected"


class KenoDisabled(TicketRejected):
    code = "keno_disabled"


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


class SimulatedPlayerCannotPlaceKenoTicket(TicketRejected):
    """Same defense-in-depth as services/payments/withdrawals.py's
    SimulatedPlayerCannotWithdraw -- a house-float-funded simulated
    player's balance must never be extractable as a real Keno win
    either. Nothing currently drives a simulated player into Keno, but
    the check costs nothing and closes the door structurally rather than
    by omission."""

    code = "simulated_player_cannot_place_ticket"


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
) -> PlacedTicket:
    """Public entry point: delegates to _place_ticket, incrementing
    keno_bet_rejections_total{reason} on any typed rejection (Part 16)
    before re-raising -- one choke point rather than an increment at
    every individual raise site in _place_ticket below."""
    try:
        return await _place_ticket(
            pool, redis, user_id=user_id, picks=picks, stake=stake, idempotency_key=idempotency_key
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

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Idempotency short-circuit -- a genuine retry (client
            # timeout, double-tap) returns the original ticket rather
            # than re-validating/re-charging. Checked first, before any
            # lock is taken, since a replay should be cheap.
            existing = await conn.fetchrow(
                "SELECT id, round_id, user_id, stake, status FROM keno_tickets WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is not None:
                return PlacedTicket(
                    id=existing["id"],
                    round_id=existing["round_id"],
                    user_id=existing["user_id"],
                    picks=validated_picks,
                    stake=existing["stake"],
                    idempotency_key=idempotency_key,
                    status=existing["status"],
                )

            user_row = await conn.fetchrow(
                "SELECT id, status, is_simulated FROM users WHERE id = $1 FOR UPDATE", user_id
            )
            if user_row is None:
                raise UnknownUser(str(user_id))
            if user_row["is_simulated"]:
                raise SimulatedPlayerCannotPlaceKenoTicket(str(user_id))

            config = await keno_config.load_active_config(conn)
            if not config["keno_enabled"]:
                raise KenoDisabled()

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
                new_ticket=ticket_risk,
                reserve_balance=reserve_balance,
                max_round_exposure_pct=tier["max_round_exposure_pct"],
            )
            if not exposure_check.allowed:
                raise RoundCapacityReached(str(exposure_check.projected_exposure))

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
                    idempotency_key=idempotency_key,
                    created_by="keno_tickets.place_ticket",
                )
            except ledger.InsufficientFunds as exc:
                raise TicketRejected("insufficient_balance") from exc

            ticket_id = await conn.fetchval(
                """
                INSERT INTO keno_tickets
                    (round_id, user_id, paytable_id, pick_count, stake,
                     expected_payout_contribution, payout_variance_contribution,
                     status, idempotency_key, stake_txn_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9)
                RETURNING id
                """,
                round_id,
                user_id,
                paytable["id"],
                pick_count,
                stake,
                ticket_risk.expected_payout,
                ticket_risk.payout_variance,
                idempotency_key,
                stake_txn.id,
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


async def _keno_reserve_balance(conn: ledger.AsyncpgConnection) -> Decimal:
    account = await ledger.get_or_create_account(conn, None, "keno_reserve")
    return await ledger.balance(conn, account.id)
