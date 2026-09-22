from __future__ import annotations

from decimal import Decimal

import pytest

from packages.core import keno_risk_simulator as sim

_MULTIPLIERS = {1: Decimal("0.19"), 2: Decimal("0.97"), 3: Decimal("3.89"), 4: Decimal("11.67"), 5: Decimal("16.00")}


def _run(**overrides):
    params = dict(
        starting_reserve=Decimal("40000"),
        daily_handle=Decimal("50000"),
        avg_stake=Decimal("20"),
        pick_count=5,
        multipliers=_MULTIPLIERS,
        jackpot_diversion_bps=150,
        floor=Decimal("0"),
        days=30,
        num_simulations=200,
        rng_seed=1,
    )
    params.update(overrides)
    return sim.simulate_risk_of_ruin(**params)


def test_same_seed_is_fully_deterministic() -> None:
    """A planning tool an operator compares candidate paytables with
    needs reproducible results for the same inputs -- otherwise "table A
    looked safer than table B" could just be RNG noise between runs."""
    first = _run(rng_seed=7)
    second = _run(rng_seed=7)
    assert first == second


def test_different_seeds_produce_different_trajectories() -> None:
    first = _run(rng_seed=1)
    second = _run(rng_seed=2)
    assert first.ending_reserve_mean != second.ending_reserve_mean


def test_high_volume_strong_edge_reserve_grows_reliably() -> None:
    """At realistic launch scale (thousands of tickets/day against an 18%
    house edge), the Central Limit Theorem's own diversification makes a
    losing trajectory implausible -- the reserve should essentially
    always grow, not just on average."""
    result = _run(starting_reserve=Decimal("40000"), daily_handle=Decimal("50000"), days=90, num_simulations=500)
    assert result.ending_reserve_mean > result.starting_reserve
    assert result.floor_breach_probability < Decimal("0.01")


def test_thin_reserve_low_volume_shows_real_ruin_risk() -> None:
    """The same math, deliberately starved of both reserve and volume --
    proves the floor-breach mechanic genuinely fires under risky
    conditions, not just always reporting zero regardless of input."""
    result = _run(starting_reserve=Decimal("500"), daily_handle=Decimal("200"), days=90, num_simulations=1000)
    assert result.floor_breach_probability > Decimal("0")
    assert result.worst_drawdown_max > Decimal("0")


def test_worst_drawdown_max_is_always_at_least_the_mean() -> None:
    result = _run(starting_reserve=Decimal("500"), daily_handle=Decimal("200"), days=60, num_simulations=500)
    assert result.worst_drawdown_max >= result.worst_drawdown_mean


def test_percentiles_are_ordered() -> None:
    result = _run(num_simulations=500)
    assert result.ending_reserve_p10 <= result.ending_reserve_p50 <= result.ending_reserve_p90


@pytest.mark.parametrize(
    "overrides",
    [
        {"daily_handle": Decimal("0")},
        {"avg_stake": Decimal("0")},
        {"days": 0},
        {"days": sim.MAX_DAYS + 1},
        {"num_simulations": 0},
        {"num_simulations": sim.MAX_SIMULATIONS + 1},
        {"jackpot_diversion_bps": -1},
        {"jackpot_diversion_bps": 10001},
    ],
)
def test_invalid_inputs_are_rejected(overrides) -> None:
    with pytest.raises(sim.InvalidSimulationInput):
        _run(**overrides)


def test_higher_jackpot_diversion_leaves_less_for_the_reserve() -> None:
    """A sanity check on the mechanism itself: diverting more of the
    stake to the jackpot pool (not the reserve) should leave the reserve
    growing more slowly, all else equal."""
    low_diversion = _run(jackpot_diversion_bps=0, num_simulations=300)
    high_diversion = _run(jackpot_diversion_bps=2000, num_simulations=300)
    assert high_diversion.ending_reserve_mean < low_diversion.ending_reserve_mean
