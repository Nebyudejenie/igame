"""Large-N statistical validation of the Keno RNG and paytable (build
spec Part 17: ">=1,000,000 simulated draws" / ">=1,000,000 tickets per
pick count"). Excluded from the default test run by the `keno_statistical`
marker (pyproject.toml) purely for wall-clock cost (~1-2 minutes), not
flakiness -- run explicitly:

    pytest -m keno_statistical -v -s

A single shared set of 1,000,000 real draws (via the real
derive_keno_draw()) is generated once and reused across every pick-count
check in this file, rather than regenerating 1,000,000 draws per pick
count -- the draw itself has no knowledge of pick count, so reuse is
correct, not a shortcut, and keeps total wall time to one draw-generation
pass (empirically ~25k draws/sec on this dev machine -> ~40s for 1M).
"""

from __future__ import annotations

import time
from collections import Counter
from decimal import Decimal

import pytest

from packages.core import keno

pytestmark = pytest.mark.keno_statistical

DRAW_SAMPLE_SIZE = 1_000_000


@pytest.fixture(scope="module")
def draw_sample() -> list[list[int]]:
    seed = keno.generate_server_seed()
    start = time.perf_counter()
    draws = [keno.derive_keno_draw(seed, f"stat-sample-{i}") for i in range(DRAW_SAMPLE_SIZE)]
    elapsed = time.perf_counter() - start
    print(f"\ngenerated {DRAW_SAMPLE_SIZE} draws in {elapsed:.1f}s ({DRAW_SAMPLE_SIZE / elapsed:.0f}/s)")
    return draws


def test_one_million_draws_are_always_exactly_draw_count_and_duplicate_free(draw_sample: list[list[int]]) -> None:
    for draw in draw_sample:
        assert len(draw) == keno.DRAW_COUNT
    # Duplicate-free is structurally guaranteed by the pop-from-pool
    # algorithm (see derive_keno_draw's docstring) -- this re-confirms it
    # empirically across the full sample rather than only trusting the
    # small-N unit test.
    assert all(len(set(draw)) == keno.DRAW_COUNT for draw in draw_sample)


def test_one_million_draws_show_uniform_number_frequency(draw_sample: list[list[int]]) -> None:
    counts: Counter[int] = Counter()
    for draw in draw_sample:
        counts.update(draw)

    assert set(counts.keys()) == set(range(1, keno.NUMBER_POOL_SIZE + 1))

    # Each number's per-draw selection is Bernoulli(20/80 = 0.25),
    # independent draw to draw -> Binomial(DRAW_SAMPLE_SIZE, 0.25).
    # mean=250,000, stddev=sqrt(N*p*(1-p))=~433. A 6-sigma band is
    # extremely unlikely to trip on genuine randomness (~1 in 5e8 per
    # number) while still catching any real bias (a broken rejection
    # -sampling threshold, a pool-index bug, etc.) by a wide margin.
    p = keno.DRAW_COUNT / keno.NUMBER_POOL_SIZE
    expected = DRAW_SAMPLE_SIZE * p
    stddev = (DRAW_SAMPLE_SIZE * p * (1 - p)) ** 0.5
    band = 6 * stddev

    worst_deviation = max(abs(counts[n] - expected) for n in range(1, keno.NUMBER_POOL_SIZE + 1))
    print(f"expected={expected:.0f} stddev={stddev:.1f} 6-sigma band=+/-{band:.1f} worst_deviation={worst_deviation:.1f}")

    for number in range(1, keno.NUMBER_POOL_SIZE + 1):
        deviation = abs(counts[number] - expected)
        assert deviation < band, f"number {number}: count={counts[number]} expected={expected:.0f} (6-sigma band +/-{band:.1f})"


@pytest.mark.parametrize("picks", [1, 2, 3, 5])
def test_empirical_rtp_converges_to_theoretical_within_tolerance(draw_sample: list[list[int]], picks: int) -> None:
    """A fixed representative ticket {1..picks} against all 1,000,000 real
    draws -- by symmetry of the draw (every number is equally likely to
    be drawn, see the uniformity test above), which specific numbers the
    ticket holds doesn't matter, only how many it holds. A low-variance,
    guardrail-compliant paytable is used so this also doubles as an
    end-to-end sanity check on compute_rtp() against real simulated play,
    not just the closed-form math already covered in test_keno.py.
    """
    ticket = frozenset(range(1, picks + 1))

    # A representative paytable per pick count, chosen to land near the
    # Part 7.5 target (82%) and pass the Part 3.2 guardrail -- shape
    # doesn't need to be the final launch paytable (that's an admin
    # config decision), only realistic enough to exercise convergence.
    multipliers = _representative_multipliers(picks)
    theoretical_rtp = keno.validate_paytable_rtp(picks, multipliers)

    stake = Decimal("10")
    max_win = Decimal("1000000")  # uncapped for this convergence check
    total_payout = Decimal(0)
    for draw in draw_sample:
        matches = keno.count_matches(ticket, draw)
        total_payout += keno.compute_payout(stake, matches, multipliers, max_win_per_ticket=max_win)

    total_stake = stake * len(draw_sample)
    empirical_rtp = total_payout / total_stake
    deviation = abs(empirical_rtp - theoretical_rtp)

    # Tolerance derived from the paytable's own theoretical variance
    # (keno.volatility -- exact Fraction arithmetic, see test_keno.py),
    # not a flat magic-number percentage: the standard error of a mean
    # over N samples is stddev/sqrt(N), and a 5-sigma band is extremely
    # unlikely to trip on genuine sampling noise while still catching a
    # real bug (a wrong multiplier, a match-counting error) by a wide
    # margin. A flat tolerance was tried first and genuinely failed for
    # picks=3 at 1-sigma-ish deviation -- not a code bug, a test-design
    # bug (the single-tier picks=3 table has real variance a fixed 1%
    # band doesn't account for). Fixed here rather than loosened blindly.
    theoretical_stddev = float(keno.volatility(picks, multipliers))
    standard_error = theoretical_stddev / (len(draw_sample) ** 0.5)
    tolerance = Decimal(str(5 * standard_error))

    print(
        f"picks={picks} theoretical_rtp={theoretical_rtp} empirical_rtp={empirical_rtp} "
        f"deviation={deviation} 5sigma_tolerance={tolerance} (n={len(draw_sample)})"
    )
    assert deviation < tolerance, (
        f"picks={picks}: empirical RTP {empirical_rtp} deviates from theoretical {theoretical_rtp} "
        f"by {deviation}, outside the 5-sigma tolerance {tolerance}"
    )


def _representative_multipliers(picks: int) -> dict[int, Decimal]:
    """One concrete, guardrail-passing multiplier table per pick count,
    solved so its top-tier probability isn't so rare that 1,000,000
    samples can't statistically converge on it. Not the final
    admin-configured launch paytable (Part 7.5 defaults are pinned
    separately in
    test_keno.py::test_launch_paytable_defaults_are_inside_guardrails_for_every_offered_pick_count)
    -- just a real, checkable table for each pick count this test
    parametrizes over.

    Only picks 1/2/3/5 are exercised here (the Part 7.5 launch tier's
    actual scope) -- deliberately, not an oversight. A single-tier
    "pay only on all N matched" table at picks=8 or picks=10 has a
    top-match probability of ~4.3e-6 / ~1.1e-7 respectively: over
    1,000,000 draws the *expected* hit count is ~4.3 and ~0.1, so a
    single unlucky (or lucky) run swings empirical RTP wildly -- that's
    a real property of high-multiplier rare-event paytables, not a test
    bug, and asserting tight convergence there at this sample size would
    be a flaky test, not a meaningful one. Verifying picks=8/10's
    guardrail compliance and structural correctness (covered in
    test_keno.py) doesn't need convergence; verifying convergence would
    need an N several orders of magnitude larger, which is exactly why a
    real paytable favors frequent, moderate wins as its RTP backbone
    (Part 8.2's own "low variance -> longer sessions" reasoning) rather
    than concentrating payout in a single astronomically rare tier.
    """
    table: dict[int, dict[int, Decimal]] = {
        1: {1: Decimal("3.40")},
        2: {2: Decimal("14.14")},
        3: {3: Decimal("61.26")},
        5: {3: Decimal("3.57"), 4: Decimal("20.67"), 5: Decimal("465.17")},
    }
    multipliers = table[picks]
    # Fail loudly (not silently) if a future edit to this table drifts
    # outside the guardrail -- this function's whole job is to hand back
    # something real.
    keno.validate_paytable_rtp(picks, multipliers)
    return multipliers
