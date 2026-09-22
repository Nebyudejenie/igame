"""Risk-of-ruin simulator (Part 7.4): "given reserve, daily handle, and a
paytable, run N simulated days and report the distribution of ending
reserve, worst drawdown, and probability of breaching the floor."

Ops/planning tooling, never the real-money draw itself -- the same
"never confuse the two" distinction packages/core/keno_exposure.py's own
module docstring already draws for its own CLT-based estimate.

## Why this simulates one day's *aggregate* outcome per step, not every
## individual ticket

A day at realistic launch handle is thousands of tickets
(daily_handle / avg_stake). Simulating each one as its own random draw,
repeated across hundreds of days and hundreds of Monte Carlo runs, is
tens of millions of individual draws for a tool an operator runs
occasionally from the admin console, not a hot path -- needlessly slow
in pure Python (no numpy in this project's dependencies) for no
statistical benefit over the alternative: keno_exposure.py already
establishes, and documents at length, that a day's (or a round's) total
payout across many roughly-independent tickets is well-approximated by
its own mean+variance via the Central Limit Theorem. This module reuses
that exact machinery (compute_ticket_risk_contribution) to get one
day's aggregate expected payout and variance, then Monte Carlo samples
across *days* (and independently, across *simulation runs*) from a
Normal(expected, variance) distribution for each day's payout --
Monte Carlo where the spec asks for it (the multi-day reserve
trajectory, which is genuinely path-dependent and not reducible to a
single percentile), CLT where keno_exposure.py's own precedent already
justifies it (each day's own internal ticket-level aggregation).

## What this does NOT do

The spec also asks this tool to "report median session length and
handle per 100 ETB deposited for each candidate paytable" and "select
the paytable that maximizes handle per deposit, not the one with the
lowest RTP." Both need a real player-behavior/retention model (does a
player keep playing after a loss? after a small win? how does session
length vary by paytable variance?) -- there is no real player data for
Keno yet (it has never been live), and inventing specific retention
-curve parameters here would be exactly the kind of unfounded number
this codebase's own discipline refuses to fabricate (see DECISIONS.md
on SantimPay/ArifPay being left unbuilt rather than guessing a webhook
contract). This module deliberately covers only the reserve-survival
half of Part 7.4 -- real money, real math, no invented assumptions.
The handle/session-length comparison remains open, flagged, not silently
missing.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from decimal import Decimal

from packages.core import keno_exposure

MAX_DAYS = 3650  # ~10 years -- a sanity ceiling against a fat-fingered input, not a spec requirement
MAX_SIMULATIONS = 20000


class InvalidSimulationInput(Exception):
    pass


@dataclass(frozen=True)
class RiskOfRuinResult:
    days: int
    num_simulations: int
    starting_reserve: Decimal
    floor: Decimal
    # Distribution of ending (day=`days`) reserve across every simulation run.
    ending_reserve_p10: Decimal
    ending_reserve_p50: Decimal
    ending_reserve_p90: Decimal
    ending_reserve_mean: Decimal
    # Worst single-run drawdown (largest peak-to-subsequent-trough decline
    # within a run), averaged and worst-cased across all runs.
    worst_drawdown_mean: Decimal
    worst_drawdown_max: Decimal
    # Fraction of simulation runs whose reserve ever touched or crossed
    # `floor` on any day -- the headline "probability of ruin" figure.
    floor_breach_probability: Decimal


def simulate_risk_of_ruin(
    *,
    starting_reserve: Decimal,
    daily_handle: Decimal,
    avg_stake: Decimal,
    pick_count: int,
    multipliers: dict[int, Decimal],
    jackpot_diversion_bps: int,
    floor: Decimal,
    days: int,
    num_simulations: int,
    rng_seed: int | None = None,
) -> RiskOfRuinResult:
    if daily_handle <= 0:
        raise InvalidSimulationInput("daily_handle must be positive")
    if avg_stake <= 0:
        raise InvalidSimulationInput("avg_stake must be positive")
    if not (0 < days <= MAX_DAYS):
        raise InvalidSimulationInput(f"days must be between 1 and {MAX_DAYS}")
    if not (0 < num_simulations <= MAX_SIMULATIONS):
        raise InvalidSimulationInput(f"num_simulations must be between 1 and {MAX_SIMULATIONS}")
    if not (0 <= jackpot_diversion_bps <= 10000):
        raise InvalidSimulationInput("jackpot_diversion_bps must be between 0 and 10000")

    rng = random.Random(rng_seed)

    tickets_per_day = daily_handle / avg_stake
    per_ticket = keno_exposure.compute_ticket_risk_contribution(pick_count, multipliers, stake=avg_stake)
    day_expected_payout = per_ticket.expected_payout * tickets_per_day
    # Variance of a sum of n iid variables is n * (per-item variance) --
    # same additive-variance step keno_exposure.py's own round-level
    # accumulator already takes, same documented weak-correlation
    # approximation (tickets on the same day span many different rounds,
    # so they're considerably *more* independent here than same-round
    # tickets are, making this approximation if anything more accurate
    # at the day level than at the round level it was first justified for).
    day_variance = per_ticket.payout_variance * tickets_per_day
    day_stddev = float(day_variance.sqrt()) if day_variance > 0 else 0.0

    reserve_credit_per_day = daily_handle * (Decimal(10000 - jackpot_diversion_bps) / Decimal(10000))

    ending_reserves: list[Decimal] = []
    worst_drawdowns: list[Decimal] = []
    breach_count = 0

    for _ in range(num_simulations):
        reserve = starting_reserve
        peak = reserve
        worst_drawdown = Decimal(0)
        breached = False
        for _day in range(days):
            day_payout = Decimal(str(max(0.0, rng.gauss(float(day_expected_payout), day_stddev))))
            reserve = reserve + reserve_credit_per_day - day_payout
            if reserve > peak:
                peak = reserve
            drawdown = peak - reserve
            if drawdown > worst_drawdown:
                worst_drawdown = drawdown
            if reserve <= floor:
                breached = True
        ending_reserves.append(reserve)
        worst_drawdowns.append(worst_drawdown)
        if breached:
            breach_count += 1

    ending_reserves.sort()

    def _percentile(sorted_values: list[Decimal], pct: float) -> Decimal:
        if not sorted_values:
            return Decimal(0)
        index = min(len(sorted_values) - 1, max(0, int(round(pct * (len(sorted_values) - 1)))))
        return sorted_values[index]

    return RiskOfRuinResult(
        days=days,
        num_simulations=num_simulations,
        starting_reserve=starting_reserve,
        floor=floor,
        ending_reserve_p10=_percentile(ending_reserves, 0.10),
        ending_reserve_p50=_percentile(ending_reserves, 0.50),
        ending_reserve_p90=_percentile(ending_reserves, 0.90),
        ending_reserve_mean=(
            Decimal(str(statistics.fmean(float(v) for v in ending_reserves))).quantize(Decimal("0.01"))
        ),
        worst_drawdown_mean=(
            Decimal(str(statistics.fmean(float(v) for v in worst_drawdowns))).quantize(Decimal("0.01"))
            if worst_drawdowns
            else Decimal(0)
        ),
        worst_drawdown_max=max(worst_drawdowns) if worst_drawdowns else Decimal(0),
        floor_breach_probability=(
            Decimal(breach_count) / Decimal(num_simulations)
        ).quantize(Decimal("0.0001")),
    )
