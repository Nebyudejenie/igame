"""Round exposure estimation for Part 7.3's pre-acceptance capacity cap.

Ops/risk-management tooling, never the real-money draw itself (that is
packages/core/keno.py's CSPRNG-based commit-reveal derive_keno_draw --
never confuse the two).

## Why this is a mean+variance (CLT) aggregation, not a sum of per-ticket
## worst-cases

An earlier version of this module estimated round exposure by summing
each ticket's own "99.9th percentile payout" (the payout level above
which only 0.1% of that ticket's own outcomes fall). That collapses to
the ticket's *absolute worst-case* payout for any pick count whose
top-match probability already exceeds 0.1% -- which is true of every
pick count in the Part 7.5 launch tier (pick=1 matches 25% of the time,
pick=3 matches all 3 ~1.4% of the time, both far more likely than the
0.1% tail budget). Summing worst-cases across many tickets is exactly
the "absolute worst case ... unusable at this reserve size" the build
spec explicitly says to avoid (Part 7.3) -- so that approach, while an
honest first attempt, was wrong, not just conservative.

The statistically correct tool for "the 99.9th percentile of a sum of
many roughly-independent random variables" is the Central Limit
Theorem: track the round's running *expected* payout (sum of each
ticket's stake * RTP) and running *variance* (sum of each ticket's own
payout variance, stake^2 * volatility^2 -- packages.core.keno.volatility()
is exact Fraction-derived, see that module), then estimate the
99.9th-percentile aggregate payout as

    expected + Z * sqrt(variance)

where Z=3.09 is the standard normal distribution's 99.9th-percentile
z-score. This is the standard actuarial approach for aggregating many
individually-small risks into a portfolio-level percentile (an insurer
sizing capital against a book of policies faces the identical problem),
and it correctly captures the diversification benefit of many
tickets -- unlike summing worst-cases, sqrt(variance) grows with the
square root of ticket count for i.i.d.-ish tickets, not linearly.

One real approximation this makes, documented rather than hidden:
tickets within the same round are not perfectly independent (they share
one draw), so treating their variances as additive slightly understates
the true aggregate variance. The tickets are only weakly correlated in
practice (each picks an arbitrary subset of 80 numbers; the correlation
between two different tickets' match counts is small unless their pick
sets overlap heavily), and this module's whole purpose is a fast,
real-time-per-bet-feasible *estimate* gating ticket acceptance, not a
regulatory capital calculation -- the round's actual settlement always
uses the real draw and exact payout math regardless of what this
estimate said beforehand.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.core import keno

# 99.9th percentile of the standard normal distribution.
Z_SCORE_99_9 = Decimal("3.09")


def single_ticket_expected_payout(pick_count: int, multipliers: dict[int, Decimal], *, stake: Decimal) -> Decimal:
    """E[payout] for one ticket -- stake * RTP(pick_count)."""
    return stake * keno.compute_rtp(pick_count, multipliers)


def single_ticket_payout_variance(pick_count: int, multipliers: dict[int, Decimal], *, stake: Decimal) -> Decimal:
    """Var[payout] for one ticket at this stake. keno.volatility() is the
    standard deviation of the *per-unit-stake* payout (i.e. of the
    multiplier random variable), so this ticket's own payout variance
    scales by stake^2."""
    per_unit_stddev = keno.volatility(pick_count, multipliers)
    return (stake**2) * (per_unit_stddev**2)


@dataclass(frozen=True)
class TicketRiskContribution:
    """What one ticket adds to the round's running CLT accumulators --
    computed once per ticket and persisted (keno_tickets.expected_payout_
    contribution / payout_variance_contribution) so later tickets' checks
    don't need to re-derive it, and so a paytable change never silently
    alters an already-accepted ticket's own contribution."""

    expected_payout: Decimal
    payout_variance: Decimal


def compute_ticket_risk_contribution(
    pick_count: int, multipliers: dict[int, Decimal], *, stake: Decimal
) -> TicketRiskContribution:
    return TicketRiskContribution(
        expected_payout=single_ticket_expected_payout(pick_count, multipliers, stake=stake),
        payout_variance=single_ticket_payout_variance(pick_count, multipliers, stake=stake),
    )


def _decimal_sqrt(value: Decimal, *, precision: int = 40) -> Decimal:
    if value <= 0:
        return Decimal(0)
    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = precision
        return value.sqrt()


def estimate_percentile_exposure(
    *, total_expected_payout: Decimal, total_payout_variance: Decimal, z_score: Decimal = Z_SCORE_99_9
) -> Decimal:
    """expected + Z * stddev -- the CLT estimate of the round's aggregate
    99.9th-percentile total payout so far."""
    stddev = _decimal_sqrt(total_payout_variance)
    return total_expected_payout + z_score * stddev


@dataclass(frozen=True)
class ExposureCheck:
    projected_exposure: Decimal
    """NET exposure: the estimated 99.9th-percentile aggregate PAYOUT,
    minus the stakes this round has already collected into the reserve
    (net of the jackpot diversion, which never lands in the reserve at
    all). This is the quantity actually compared against `ceiling` --
    see this function's own docstring for why netting the funding side
    is the economically correct comparison, not the gross payout alone."""
    gross_exposure: Decimal
    """The estimated 99.9th-percentile aggregate payout with nothing
    netted out -- kept for dashboard/observability only (it's a more
    intuitive "how big could the payout get" figure for a human reading
    a chart), never compared against `ceiling` itself."""
    ceiling: Decimal
    allowed: bool


def check_round_exposure(
    *,
    current_total_expected_payout: Decimal,
    current_total_payout_variance: Decimal,
    current_total_stake: Decimal,
    new_ticket: TicketRiskContribution,
    new_ticket_stake: Decimal,
    reserve_balance: Decimal,
    max_round_exposure_pct: Decimal,
    jackpot_diversion_bps: int,
) -> ExposureCheck:
    """The Part 7.3 gate itself: would accepting this ticket push the
    round's estimated 99.9th-percentile NET exposure -- payout minus
    the stakes that fund it -- past reserve * max_round_exposure_pct?
    Pure function -- the caller (keno_tickets.place_ticket) is
    responsible for reading the current accumulators/reserve_balance
    inside the same locked transaction that will apply the result, so
    the check-then-act is atomic per round.

    ## Why net, not gross (a real capacity bug found and fixed 2026-09-23)

    The original version of this function compared *gross* projected
    payout against the ceiling, never netting out the stakes that fund
    that payout -- even though every stake is credited into the
    reserve the instant its ticket is accepted (packages.core.
    keno_tickets.place_ticket's own ledger.post call), before the round
    ever settles. That made the ceiling far stricter than the reserve's
    actual risk: at a 20,000 ETB reserve (ceiling 2,000, pick=5, 50 ETB
    stakes), gross exposure accepted only ~20 tickets per round --
    with anything beyond a few hundred concurrent players, almost
    everyone would be rejected every round. A real, launch-blocking
    capacity problem, caught by a CTO review before go-live, not a
    tuning detail.

    The reserve's *actual* net cost from this round, at the 99.9th
    percentile, is `gross_payout_estimate - stakes_collected_this_round`
    (stakes net of the jackpot diversion, which is credited to a
    *separate* ledger account and never lands in the reserve at all).
    Verified directly against the real seeded low_variance paytables
    (packages/core/keno.py's own exact math, not approximated): for
    every pick count 1-5, this net figure has a genuine, finite peak
    (between roughly 1,460 and 1,910 ETB, at 177-228 tickets) and then
    *decreases* as more tickets join a round -- the house edge accrues
    linearly in ticket count while payout variance's standard deviation
    only grows as its square root, so once enough tickets have pooled,
    the round is profitable at high confidence regardless of how many
    more tickets join. This is the same Central-Limit-Theorem
    diversification this module's own docstring already describes for
    the gross side; the bug was never applying it to what the round
    actually costs the reserve net of its own funding.

    This does not remove the guardrail, it corrects its target: a
    round with a genuinely bad paytable (RTP miscalibrated above 100%,
    for instance) still has unboundedly growing net exposure as more
    tickets join, and is still correctly rejected past the ceiling --
    verified by test_check_round_exposure_still_rejects_a_bad_paytable
    in tests/unit/test_keno_exposure.py. What it no longer does is
    reject a *normal, correctly-priced* round for having "too many"
    players, which gross exposure was doing by nearly two orders of
    magnitude at realistic launch reserve sizes.
    """
    projected_expected = current_total_expected_payout + new_ticket.expected_payout
    projected_variance = current_total_payout_variance + new_ticket.payout_variance
    gross_exposure = estimate_percentile_exposure(
        total_expected_payout=projected_expected, total_payout_variance=projected_variance
    )
    projected_stake = current_total_stake + new_ticket_stake
    reserve_credited = (projected_stake * (Decimal(10000) - Decimal(jackpot_diversion_bps)) / Decimal(10000)).quantize(
        Decimal("0.01")
    )
    net_exposure = gross_exposure - reserve_credited
    ceiling = (reserve_balance * max_round_exposure_pct).quantize(Decimal("0.01"))
    return ExposureCheck(
        projected_exposure=net_exposure, gross_exposure=gross_exposure, ceiling=ceiling, allowed=net_exposure <= ceiling
    )


def check_per_user_round_share(
    *,
    user_expected_payout_this_round: Decimal,
    user_payout_variance_this_round: Decimal,
    new_ticket: TicketRiskContribution,
    round_exposure_ceiling: Decimal,
    max_share_bps: int,
) -> bool:
    """Part 3.3: "A single user may consume at most a configurable share
    (default 20%) of a round's remaining capacity."

    Measured against the round's fixed exposure *ceiling*
    (reserve * max_round_exposure_pct -- the same ceiling
    check_round_exposure() computes), applying the identical CLT estimate
    to just this user's own sub-portfolio of tickets this round. Not
    measured against other players' stakes: the very first ticket in any
    round is, by definition, 100% of the stake placed so far, so a
    share cap compared against other bettors' stakes would make it
    structurally impossible for anyone to ever place a round's first
    bet -- a real bug an earlier version of this function had, caught by
    tests/integration/test_keno_tickets.py actually running against a
    fresh round."""
    if round_exposure_ceiling <= 0:
        return True
    user_expected = user_expected_payout_this_round + new_ticket.expected_payout
    user_variance = user_payout_variance_this_round + new_ticket.payout_variance
    user_exposure = estimate_percentile_exposure(total_expected_payout=user_expected, total_payout_variance=user_variance)
    share_bps = int((user_exposure / round_exposure_ceiling) * 10000)
    return share_bps <= max_share_bps
