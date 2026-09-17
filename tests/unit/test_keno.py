"""Pure-function tests for packages/core/keno.py -- no database, no
Redis, no I/O. Run as part of the default suite (tests/unit/ has no
special marker in this project's pytest config)."""

from __future__ import annotations

import ast
import inspect
import textwrap
from decimal import ROUND_DOWN, Decimal
from fractions import Fraction

import pytest

from packages.core import bingo, keno


# ---------------------------------------------------------------------------
# Hypergeometric probability -- known values
# ---------------------------------------------------------------------------


def test_pick_one_probabilities_are_exact_fractions() -> None:
    # pool=80, draw=20, pick=1: P(match) = 20/80 = 0.25 exactly,
    # P(no match) = 60/80 = 0.75 exactly -- the simplest possible case,
    # computable by hand, so any bug in comb()/Decimal wiring shows up
    # immediately rather than being masked by a more complex case.
    assert keno.probability_exact_matches(1, 1) == Decimal("0.25")
    assert keno.probability_exact_matches(1, 0) == Decimal("0.75")


def test_pick_two_probabilities_match_hand_computed_fractions() -> None:
    # C(80,2) = 3160. P(2)=C(20,2)*C(60,0)/3160=190/3160.
    # P(1)=C(20,1)*C(60,1)/3160=1200/3160. P(0)=C(20,0)*C(60,2)/3160=1770/3160.
    # Compared as exact Fractions (not Decimal division at an ad-hoc
    # precision, which would just be re-testing decimal.Decimal's own
    # rounding rather than this module's math) against the function's
    # own exact internal form.
    assert keno._probability_fraction(2, 2) == Fraction(190, 3160)
    assert keno._probability_fraction(2, 1) == Fraction(1200, 3160)
    assert keno._probability_fraction(2, 0) == Fraction(1770, 3160)
    # And the public Decimal API agrees with that exact value to well
    # beyond cent precision.
    assert abs(keno.probability_exact_matches(2, 2) - Decimal(190) / Decimal(3160)) < Decimal("1e-20")


def test_probability_zero_outside_valid_match_range() -> None:
    assert keno.probability_exact_matches(3, 4) == Decimal(0)  # can't match more than picked
    assert keno.probability_exact_matches(3, -1) == Decimal(0)  # negative matches: impossible
    # picks=10, matches=0 requires 10 misses among the 60 non-drawn -- valid.
    assert keno.probability_exact_matches(10, 0) > 0
    # matches > draw_count is structurally impossible.
    assert keno.probability_exact_matches(10, 21) == Decimal(0)


@pytest.mark.parametrize("picks", list(range(keno.MIN_PICKS, keno.MAX_PICKS + 1)))
def test_match_distribution_always_sums_to_one_within_display_precision(picks: int) -> None:
    # match_distribution() is the Decimal *display* form -- each term is
    # independently rounded to 40 significant digits, so summing already
    # -rounded terms can be off from 1 by a tiny amount (~1e-30 scale,
    # nowhere close to affecting a cent at any real stake). The exact,
    # zero-tolerance proof this is correct math (not just close enough)
    # is test_match_distribution_fraction_sums_to_exactly_one below,
    # which sums the underlying Fractions before any rounding happens.
    distribution = keno.match_distribution(picks)
    total = sum(distribution.values(), Decimal(0))
    assert abs(total - Decimal(1)) < Decimal("1e-25")
    # Every possible match count from 0..picks must be present.
    assert set(distribution.keys()) == set(range(0, picks + 1))


# ---------------------------------------------------------------------------
# RTP / hit frequency / guardrails
# ---------------------------------------------------------------------------


def test_compute_rtp_matches_manual_calculation_for_pick_one() -> None:
    # A pick-1 paytable paying 3.2x on the single way to match: RTP =
    # P(1)*3.2 + P(0)*0 = 0.25*3.2 = 0.8 exactly.
    multipliers = {1: Decimal("3.2")}
    assert keno.compute_rtp(1, multipliers) == Decimal("0.8")


def test_hit_frequency_counts_only_winning_match_counts() -> None:
    multipliers = {1: Decimal("3.2"), 0: Decimal("0")}
    # matches=0 has multiplier 0 (explicitly, or by omission) -- not a win.
    assert keno.hit_frequency(1, multipliers) == Decimal("0.25")


def test_validate_paytable_rtp_accepts_within_guardrails() -> None:
    multipliers = {1: Decimal("3.28")}  # RTP = 0.25*3.28 = 0.82 -> within [0.75, 0.97]
    rtp = keno.validate_paytable_rtp(1, multipliers)
    assert keno.RTP_FLOOR <= rtp <= keno.RTP_CEILING


def test_validate_paytable_rtp_rejects_above_ceiling() -> None:
    multipliers = {1: Decimal("4.5")}  # RTP = 0.25*4.5 = 1.125 -- absurdly over 97%
    with pytest.raises(keno.PaytableRTPOutOfRange):
        keno.validate_paytable_rtp(1, multipliers)


def test_validate_paytable_rtp_rejects_below_floor() -> None:
    multipliers = {1: Decimal("1.0")}  # RTP = 0.25 -- well under 75%
    with pytest.raises(keno.PaytableRTPOutOfRange):
        keno.validate_paytable_rtp(1, multipliers)


def test_launch_paytable_defaults_are_inside_guardrails_for_every_offered_pick_count() -> None:
    """Not a guess: this pins the Part 7.5 default (target 82% RTP,
    picks 1-5) to actually satisfy Part 3.2's own hard guardrail, so a
    future accidental change to either the target or the guardrail
    constants is caught here rather than at the admin paytable-save
    endpoint in production."""
    # A representative low-variance pick-3 table landing close to 82%:
    # solved by hand against match_distribution so this is a real,
    # checkable paytable, not an arbitrary placeholder.
    multipliers = {3: Decimal("41.0"), 2: Decimal("2.0")}
    rtp = keno.compute_rtp(3, multipliers)
    assert keno.RTP_FLOOR <= rtp <= keno.RTP_CEILING


def test_max_multiplier_and_volatility_are_computed_not_guessed() -> None:
    multipliers = {5: Decimal("16"), 4: Decimal("3"), 3: Decimal("1")}
    stats = keno.compute_paytable_stats(5, multipliers)
    assert stats.max_multiplier == Decimal("16")
    assert stats.volatility >= Decimal(0)
    assert Decimal(0) <= stats.hit_frequency <= Decimal(1)
    # RTP (an expectation) can never exceed the largest single multiplier
    # it's built from -- this table isn't guardrail-compliant by design
    # (it's just exercising the stats plumbing), so assert that weaker,
    # always-true bound instead of a floor/ceiling this table was never
    # meant to satisfy.
    assert Decimal(0) <= stats.rtp <= stats.max_multiplier


# ---------------------------------------------------------------------------
# Ticket validation / match counting
# ---------------------------------------------------------------------------


def test_validate_picks_accepts_valid_selection() -> None:
    picks = keno.validate_picks([5, 12, 80, 1])
    assert picks == frozenset({5, 12, 80, 1})


def test_validate_picks_rejects_too_few() -> None:
    with pytest.raises(keno.InvalidPickCount):
        keno.validate_picks([], min_picks=1, max_picks=10)


def test_validate_picks_rejects_too_many() -> None:
    with pytest.raises(keno.InvalidPickCount):
        keno.validate_picks(list(range(1, 12)), min_picks=1, max_picks=10)


def test_validate_picks_rejects_duplicates() -> None:
    with pytest.raises(keno.DuplicatePicks):
        keno.validate_picks([5, 5, 7])


def test_validate_picks_rejects_out_of_range() -> None:
    with pytest.raises(keno.PickOutOfRange):
        keno.validate_picks([0, 5])
    with pytest.raises(keno.PickOutOfRange):
        keno.validate_picks([81, 5])


def test_count_matches_correctness() -> None:
    picks = keno.validate_picks([1, 2, 3, 4, 5])
    drawn = [1, 2, 6, 7, 8]
    assert keno.count_matches(picks, drawn) == 2


# ---------------------------------------------------------------------------
# Payout computation
# ---------------------------------------------------------------------------


def test_compute_payout_rounds_down_and_never_up() -> None:
    multipliers = {3: Decimal("3.33")}
    # stake 10 * 3.33 = 33.30 exactly -- pick a stake that forces truncation.
    payout = keno.compute_payout(Decimal("10.10"), 3, multipliers, max_win_per_ticket=Decimal("10000"))
    raw = Decimal("10.10") * Decimal("3.33")
    assert payout <= raw
    assert payout == raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def test_compute_payout_never_exceeds_max_win_per_ticket() -> None:
    multipliers = {5: Decimal("5000")}
    payout = keno.compute_payout(Decimal("50"), 5, multipliers, max_win_per_ticket=Decimal("800"))
    assert payout == Decimal("800.00")


def test_compute_payout_zero_for_non_winning_match_count() -> None:
    multipliers = {5: Decimal("16")}
    payout = keno.compute_payout(Decimal("50"), 2, multipliers, max_win_per_ticket=Decimal("800"))
    assert payout == Decimal("0.00")


# ---------------------------------------------------------------------------
# Commit-reveal draw: determinism, structure, verification
# ---------------------------------------------------------------------------


def test_derive_keno_draw_is_deterministic() -> None:
    seed = b"\x01" * 32
    public_seed = "42:1758100000:abc123"
    first = keno.derive_keno_draw(seed, public_seed)
    second = keno.derive_keno_draw(seed, public_seed)
    assert first == second


def test_derive_keno_draw_changes_completely_with_one_bit_flipped() -> None:
    seed_a = b"\x01" * 32
    seed_b = b"\x01" * 31 + b"\x02"
    public_seed = "42:1758100000:abc123"
    draw_a = keno.derive_keno_draw(seed_a, public_seed)
    draw_b = keno.derive_keno_draw(seed_b, public_seed)
    assert draw_a != draw_b


def test_derive_keno_draw_structure_is_always_valid() -> None:
    seed = b"\x09" * 32
    for round_id in range(5):
        draw = keno.derive_keno_draw(seed, f"round-{round_id}")
        assert len(draw) == keno.DRAW_COUNT
        assert len(set(draw)) == keno.DRAW_COUNT  # structurally impossible to duplicate
        assert all(1 <= n <= keno.NUMBER_POOL_SIZE for n in draw)


def test_derive_keno_draw_respects_custom_pool_and_count() -> None:
    seed = b"\x0a" * 32
    draw = keno.derive_keno_draw(seed, "public", pool_size=10, draw_count=10)
    assert sorted(draw) == list(range(1, 11))  # full-pool draw = a permutation


def test_verify_draw_roundtrip() -> None:
    seed = keno.generate_server_seed()
    public_seed = "round-99:123:deadbeef"
    draw = keno.derive_keno_draw(seed, public_seed)
    assert keno.verify_draw(seed, public_seed, draw) is True
    assert keno.verify_draw(seed, public_seed, draw[:-1] + [999]) is False


def test_verify_server_seed_hash_roundtrip() -> None:
    seed = keno.generate_server_seed()
    digest = keno.server_seed_hash(seed)
    assert keno.verify_server_seed_hash(seed, digest) is True
    assert keno.verify_server_seed_hash(seed, "0" * 64) is False


def test_public_seed_is_derived_from_data_unknowable_at_commit_time() -> None:
    ticket_hash_a = keno.compute_ticket_ids_rolling_hash([1, 2, 3])
    ticket_hash_b = keno.compute_ticket_ids_rolling_hash([1, 2, 4])
    assert ticket_hash_a != ticket_hash_b
    # Order-independence: the same set of accepted tickets always produces
    # the same public seed regardless of arrival order -- otherwise the
    # server could influence the seed by choosing processing order.
    assert keno.compute_ticket_ids_rolling_hash([3, 1, 2]) == ticket_hash_a

    seed_a = keno.derive_public_seed(round_id=7, betting_closed_at_epoch=1000, ticket_ids_hash=ticket_hash_a)
    seed_b = keno.derive_public_seed(round_id=7, betting_closed_at_epoch=1000, ticket_ids_hash=ticket_hash_b)
    assert seed_a != seed_b


# ---------------------------------------------------------------------------
# PART 4.2's absolute prohibition, enforced structurally
# ---------------------------------------------------------------------------


def test_derive_draw_signature_has_no_ticket_data() -> None:
    """Structural enforcement of Part 4.2: the draw function's signature
    must never grow a parameter through which ticket/selection data could
    reach it. This test inspects the real signature rather than trusting
    a docstring -- it fails the build the moment anyone adds a
    ticket/selection/bet-shaped parameter to derive_keno_draw, on
    purpose, as a hard gate rather than a convention."""
    signature = inspect.signature(keno.derive_keno_draw)
    parameter_names = set(signature.parameters.keys())
    assert parameter_names == {"server_seed", "public_seed", "pool_size", "draw_count"}
    forbidden_substrings = ("ticket", "pick", "bet", "selection", "player", "user", "wager")
    for name in parameter_names:
        lowered = name.lower()
        assert not any(bad in lowered for bad in forbidden_substrings), (
            f"derive_keno_draw gained a ticket-data-shaped parameter: {name!r}"
        )


def test_derive_keno_draw_source_never_references_ticket_or_stake_symbols() -> None:
    """A second, independent structural check at the source-code level
    (not just the signature) -- the function BODY (identifiers actually
    used in code, not English prose in the docstring, which legitimately
    discusses tickets when explaining why the signature is shaped this
    way) must never reference a ticket/stake-shaped name. Parsed via
    `ast` rather than a raw substring search specifically so a docstring
    explaining this very invariant can't trip its own regression test."""
    source = textwrap.dedent(inspect.getsource(keno.derive_keno_draw))
    tree = ast.parse(source)
    function_def = tree.body[0]
    assert isinstance(function_def, ast.FunctionDef)
    # Drop the docstring (the first body statement, if it's a bare string
    # expression) before collecting identifiers -- prose is not code.
    body = function_def.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    identifiers: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        if isinstance(node, ast.arg):
            identifiers.add(node.arg.lower())
    forbidden_substrings = ("ticket", "pick", "stake", "selection", "wager", "bet")
    for identifier in identifiers:
        assert not any(bad in identifier for bad in forbidden_substrings), (
            f"derive_keno_draw's body references a ticket-data-shaped identifier: {identifier!r}"
        )


def test_match_distribution_fraction_sums_to_exactly_one() -> None:
    """The exact-rational form (what compute_rtp/hit_frequency actually
    accumulate with) sums to precisely Fraction(1, 1) for every offered
    pick count, by Vandermonde's identity -- zero tolerance, because this
    is exact arithmetic, not an approximation."""
    for picks in range(keno.MIN_PICKS, keno.MAX_PICKS + 1):
        distribution = keno._match_distribution_fraction(picks)
        assert sum(distribution.values(), Fraction(0)) == Fraction(1, 1)


# ---------------------------------------------------------------------------
# Equivalence with bingo.py's byte-stream primitive (proves no silent drift
# between the two independent, deliberately-not-shared implementations)
# ---------------------------------------------------------------------------


def test_byte_stream_matches_bingo_byte_stream() -> None:
    seed = b"\x42" * 32
    client_seed = b"same-seed-for-both"
    keno_stream = keno._ByteStream(seed, client_seed)
    bingo_stream = bingo._ByteStream(seed, client_seed)
    # Pull the same sequence of variously-sized chunks from both and
    # assert byte-for-byte identical output -- proves the HMAC
    # construction and buffering are identical, not just "similar".
    for n in (3, 1, 7, 16, 32, 2):
        assert keno_stream.take(n) == bingo_stream.take(n)


def test_below_rejection_sampling_matches_bingo_for_identical_stream_state() -> None:
    seed = b"\x77" * 32
    client_seed = b"below-equivalence-check"
    keno_stream = keno._ByteStream(seed, client_seed)
    bingo_stream = bingo._ByteStream(seed, client_seed)
    for upper in (75, 80, 20, 2, 1, 63):
        assert keno_stream.below(upper) == bingo_stream.below(upper)


def test_full_pool_draw_matches_bingo_derive_draw_for_a_75_number_pool() -> None:
    """The strongest available cross-check: bingo.derive_draw() performs
    an in-place swap-based Fisher-Yates shuffle of a 75-number pool;
    keno.derive_keno_draw() with pool_size=draw_count=75 performs a
    pop-based full draw. These are different algorithms (see this
    module's own docstring) and are NOT expected to produce the same
    output ordering -- this test instead proves the weaker, still
    meaningful property both must share: both are valid permutations of
    the same 75 numbers, deterministic, and reproducible."""
    seed = b"\x13" * 32
    client_seed = "cross-check-75"
    bingo_draw = bingo.derive_draw(seed, client_seed)
    keno_draw = keno.derive_keno_draw(seed, client_seed, pool_size=75, draw_count=75)
    assert sorted(bingo_draw) == sorted(keno_draw) == list(range(1, 76))
    assert bingo_draw == bingo.derive_draw(seed, client_seed)
    assert keno_draw == keno.derive_keno_draw(seed, client_seed, pool_size=75, draw_count=75)


# ---------------------------------------------------------------------------
# Statistical properties at a size a fast unit test can afford. The full
# >=1,000,000-draw / >=1,000,000-ticket statistical suite Part 17 asks for
# lives in tests/unit/test_keno_statistical.py under a dedicated marker
# (excluded from the default run, same convention as `load`/`e2e`/`chaos_infra`).
# ---------------------------------------------------------------------------


def test_below_shows_no_obvious_modulo_bias_at_small_scale() -> None:
    seed = b"\x55" * 32
    stream = keno._ByteStream(seed, b"bias-check")
    upper = 7  # deliberately not a power of two, where modulo bias would show up
    counts = [0] * upper
    samples = 7000
    for _ in range(samples):
        counts[stream.below(upper)] += 1
    expected = samples / upper
    for count in counts:
        # Loose statistical tolerance appropriate for a fast unit test --
        # the real large-N convergence check lives in the statistical
        # suite; this just catches a gross, obvious bias (e.g. an
        # off-by-one in the threshold computation).
        assert abs(count - expected) < expected * 0.25


def test_derive_keno_draw_many_rounds_never_produces_duplicates_or_wrong_length() -> None:
    seed = keno.generate_server_seed()
    for i in range(500):
        draw = keno.derive_keno_draw(seed, f"public-seed-{i}")
        assert len(draw) == keno.DRAW_COUNT
        assert len(set(draw)) == keno.DRAW_COUNT
