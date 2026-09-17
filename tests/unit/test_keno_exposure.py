from __future__ import annotations

from decimal import Decimal

from packages.core import keno_exposure


def test_single_ticket_expected_payout_matches_stake_times_rtp() -> None:
    multipliers = {1: Decimal("3.40")}  # RTP = 0.25 * 3.40 = 0.85
    expected = keno_exposure.single_ticket_expected_payout(1, multipliers, stake=Decimal("10"))
    assert expected == Decimal("8.5")


def test_single_ticket_payout_variance_is_nonnegative() -> None:
    multipliers = {5: Decimal("16"), 4: Decimal("3"), 3: Decimal("1")}
    variance = keno_exposure.single_ticket_payout_variance(5, multipliers, stake=Decimal("50"))
    assert variance >= 0


def test_compute_ticket_risk_contribution_matches_the_two_underlying_functions() -> None:
    multipliers = {3: Decimal("61.26")}
    stake = Decimal("10")
    contribution = keno_exposure.compute_ticket_risk_contribution(3, multipliers, stake=stake)
    assert contribution.expected_payout == keno_exposure.single_ticket_expected_payout(3, multipliers, stake=stake)
    assert contribution.payout_variance == keno_exposure.single_ticket_payout_variance(3, multipliers, stake=stake)


def test_estimate_percentile_exposure_is_expected_plus_z_times_stddev() -> None:
    # variance=100 -> stddev=10; expected=50 -> 50 + 3.09*10 = 80.9
    result = keno_exposure.estimate_percentile_exposure(
        total_expected_payout=Decimal("50"), total_payout_variance=Decimal("100")
    )
    assert result == Decimal("80.90")


def test_estimate_percentile_exposure_handles_zero_variance() -> None:
    result = keno_exposure.estimate_percentile_exposure(
        total_expected_payout=Decimal("50"), total_payout_variance=Decimal("0")
    )
    assert result == Decimal("50")


def test_check_round_exposure_allows_under_ceiling_and_blocks_over() -> None:
    # Small ticket against a large reserve -> comfortably allowed.
    multipliers = {1: Decimal("3.40")}
    ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=Decimal("10"))
    allowed = keno_exposure.check_round_exposure(
        current_total_expected_payout=Decimal(0),
        current_total_payout_variance=Decimal(0),
        new_ticket=ticket,
        reserve_balance=Decimal("40000"),
        max_round_exposure_pct=Decimal("0.05"),
    )
    assert allowed.allowed is True
    assert allowed.ceiling == Decimal("2000.00")

    # The identical ticket against a near-zero reserve -> blocked.
    blocked = keno_exposure.check_round_exposure(
        current_total_expected_payout=Decimal(0),
        current_total_payout_variance=Decimal(0),
        new_ticket=ticket,
        reserve_balance=Decimal("1"),
        max_round_exposure_pct=Decimal("0.05"),
    )
    assert blocked.allowed is False


def test_check_round_exposure_aggregates_many_small_tickets_without_reaching_worst_case() -> None:
    """The whole point of the CLT redesign: summing many ordinary
    tickets' expected+variance must land far below the naive
    "every ticket hits its own maximum simultaneously" worst case,
    since that's the exact "absolute worst case is unusable" failure
    mode Part 7.3 warns against."""
    multipliers = {1: Decimal("3.40")}
    stake = Decimal("10")
    ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=stake)

    total_expected = Decimal(0)
    total_variance = Decimal(0)
    num_tickets = 200
    for _ in range(num_tickets):
        total_expected += ticket.expected_payout
        total_variance += ticket.payout_variance

    aggregate_estimate = keno_exposure.estimate_percentile_exposure(
        total_expected_payout=total_expected, total_payout_variance=total_variance
    )
    worst_case = stake * Decimal("3.40") * num_tickets  # every single ticket wins its max
    assert aggregate_estimate < worst_case


def test_check_per_user_round_share_allows_under_cap_and_blocks_over() -> None:
    multipliers = {1: Decimal("3.40")}
    small_ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=Decimal("10"))
    # A tiny ticket against a large ceiling -> comfortably under any
    # reasonable share cap.
    assert keno_exposure.check_per_user_round_share(
        user_expected_payout_this_round=Decimal(0),
        user_payout_variance_this_round=Decimal(0),
        new_ticket=small_ticket,
        round_exposure_ceiling=Decimal("10000"),
        max_share_bps=2000,
    )

    # The same ticket against a ceiling barely larger than this one
    # ticket's own estimated exposure -> exceeds a 20% share cap.
    tiny_ceiling = keno_exposure.estimate_percentile_exposure(
        total_expected_payout=small_ticket.expected_payout, total_payout_variance=small_ticket.payout_variance
    )
    assert not keno_exposure.check_per_user_round_share(
        user_expected_payout_this_round=Decimal(0),
        user_payout_variance_this_round=Decimal(0),
        new_ticket=small_ticket,
        round_exposure_ceiling=tiny_ceiling,
        max_share_bps=2000,
    )


def test_check_per_user_round_share_allows_the_very_first_ticket_of_a_round() -> None:
    # The exact bug the ceiling-based redesign fixed: a brand-new round's
    # first-ever ticket must not be auto-rejected just because it's
    # currently "100% of tickets placed so far" -- what matters is its
    # share of the round's fixed capacity ceiling, not of other bettors.
    multipliers = {1: Decimal("3.40")}
    ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=Decimal("10"))
    assert keno_exposure.check_per_user_round_share(
        user_expected_payout_this_round=Decimal(0),
        user_payout_variance_this_round=Decimal(0),
        new_ticket=ticket,
        round_exposure_ceiling=Decimal("1000"),
        max_share_bps=2000,
    )


def test_check_per_user_round_share_never_divides_by_zero() -> None:
    multipliers = {1: Decimal("3.40")}
    ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=Decimal("0.01"))
    assert keno_exposure.check_per_user_round_share(
        user_expected_payout_this_round=Decimal(0),
        user_payout_variance_this_round=Decimal(0),
        new_ticket=ticket,
        round_exposure_ceiling=Decimal("0"),
        max_share_bps=2000,
    )
