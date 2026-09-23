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
    stake = Decimal("10")
    ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=stake)
    allowed = keno_exposure.check_round_exposure(
        current_total_expected_payout=Decimal(0),
        current_total_payout_variance=Decimal(0),
        current_total_stake=Decimal(0),
        new_ticket=ticket,
        new_ticket_stake=stake,
        reserve_balance=Decimal("40000"),
        max_round_exposure_pct=Decimal("0.05"),
        jackpot_diversion_bps=150,
    )
    assert allowed.allowed is True
    assert allowed.ceiling == Decimal("2000.00")
    # net must be strictly less than gross once any stake has been
    # collected -- the entire point of this check.
    assert allowed.projected_exposure < allowed.gross_exposure

    # The identical ticket against a near-zero reserve -> blocked (the
    # ceiling itself is a fraction of a cent; no realistic net exposure
    # fits under it).
    blocked = keno_exposure.check_round_exposure(
        current_total_expected_payout=Decimal(0),
        current_total_payout_variance=Decimal(0),
        current_total_stake=Decimal(0),
        new_ticket=ticket,
        new_ticket_stake=stake,
        reserve_balance=Decimal("1"),
        max_round_exposure_pct=Decimal("0.05"),
        jackpot_diversion_bps=150,
    )
    assert blocked.allowed is False


def test_check_round_exposure_nets_out_collected_stakes() -> None:
    """The real capacity bug found by a CTO review before go-live,
    2026-09-23: the original version of this function compared *gross*
    projected payout against the ceiling, never netting out the stakes
    that fund it -- even though every stake is credited into the
    reserve the instant its ticket is accepted, before the round ever
    settles. At a 20,000 ETB reserve (ceiling 2,000 at 10%), the real
    launch pick=5 low_variance paytable, 50 ETB stakes, the gross
    -exposure version accepted only ~20 tickets per round -- a genuine
    launch-blocking capacity problem at realistic player counts.

    This test proves the fix directly: replay accepting tickets one at
    a time (mirroring keno_tickets.place_ticket's own accumulation),
    and confirm that acceptance now continues far past where the old
    gross check would have cut it off, while gross_exposure alone
    (computed identically either way) documents exactly how much
    stricter the old check was."""
    multipliers = {1: Decimal("0.19"), 2: Decimal("0.97"), 3: Decimal("3.89"), 4: Decimal("11.67"), 5: Decimal("16.00")}
    stake = Decimal("50")
    reserve_balance = Decimal("20000")
    max_round_exposure_pct = Decimal("0.10")
    jackpot_diversion_bps = 150

    total_expected = Decimal(0)
    total_variance = Decimal(0)
    total_stake = Decimal(0)
    accepted = 0
    last_check = None
    for _ in range(500):
        ticket = keno_exposure.compute_ticket_risk_contribution(5, multipliers, stake=stake)
        check = keno_exposure.check_round_exposure(
            current_total_expected_payout=total_expected,
            current_total_payout_variance=total_variance,
            current_total_stake=total_stake,
            new_ticket=ticket,
            new_ticket_stake=stake,
            reserve_balance=reserve_balance,
            max_round_exposure_pct=max_round_exposure_pct,
            jackpot_diversion_bps=jackpot_diversion_bps,
        )
        if not check.allowed:
            last_check = check
            break
        accepted += 1
        total_expected += ticket.expected_payout
        total_variance += ticket.payout_variance
        total_stake += stake

    # The old gross-only check accepted ~20 tickets here (verified by
    # direct simulation during the review); net exposure's own
    # documented peak for this exact paytable is ~1,910 ETB at ~228
    # tickets -- comfortably under a 2,000 ceiling -- so acceptance
    # should run for hundreds of tickets, not stop at a couple dozen.
    assert accepted > 400, f"only {accepted} tickets accepted -- net exposure fix did not take effect"
    # Confirms the rejection, when it eventually happens (or would
    # happen at higher n), is a real net-exposure comparison, not
    # accidentally always-true: gross_exposure was already far past
    # the ceiling long before this point.
    if last_check is not None:
        assert last_check.gross_exposure > last_check.ceiling


def test_check_round_exposure_still_rejects_a_bad_paytable() -> None:
    """The net-exposure fix must not gut the guardrail entirely: a
    paytable priced with no real house edge (RTP effectively ~100%+)
    must still be rejected once enough tickets accumulate, since net
    exposure for a *bad* paytable keeps growing without the bound a
    correctly-priced one has -- proving this is a genuine economic
    correction, not a removal of the check."""
    # A deliberately mispriced pick=1 table -- pays out its full stake
    # back with certainty-ish odds (1/80 pool, drawn 20/80 -> P(hit) =
    # 20/80 = 25%), multiplier 5.00 means RTP = 0.25 * 5.00 = 125%: a
    # real bug, not a realistic launch table (compare against the real
    # 3.28 launch multiplier for pick=1, RTP=82%).
    multipliers = {1: Decimal("5.00")}
    stake = Decimal("50")
    reserve_balance = Decimal("20000")
    max_round_exposure_pct = Decimal("0.10")

    total_expected = Decimal(0)
    total_variance = Decimal(0)
    total_stake = Decimal(0)
    accepted = 0
    for _ in range(5000):
        ticket = keno_exposure.compute_ticket_risk_contribution(1, multipliers, stake=stake)
        check = keno_exposure.check_round_exposure(
            current_total_expected_payout=total_expected,
            current_total_payout_variance=total_variance,
            current_total_stake=total_stake,
            new_ticket=ticket,
            new_ticket_stake=stake,
            reserve_balance=reserve_balance,
            max_round_exposure_pct=max_round_exposure_pct,
            jackpot_diversion_bps=150,
        )
        if not check.allowed:
            break
        accepted += 1
        total_expected += ticket.expected_payout
        total_variance += ticket.payout_variance
        total_stake += stake

    assert accepted < 5000, "a paytable with no real house edge was never rejected -- guardrail did not fire"


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
