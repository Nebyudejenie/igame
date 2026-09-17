"""Keno: pure game math and the commit-reveal draw.

Deliberately independent of packages/core/bingo.py. Bingo's own provably
-fair draw algorithm (_ByteStream, derive_draw) is fairness-critical for
every historical Bingo round already settled in production -- touching
that file is not required to build Keno, so per the project's own
"purely additive" rule it is not touched. This module reimplements the
identical HMAC-SHA256 counter-mode byte stream and rejection-sampling
`below()` primitive independently, byte-for-byte
(tests/unit/test_keno.py::test_byte_stream_matches_bingo_byte_stream
proves the two agree on identical inputs, so the duplication cannot
silently drift). The *outer* draw algorithm is legitimately different,
not a drift: Bingo's derive_draw() shuffles its entire 75-number pool
in place (Fisher-Yates swap, every number is "drawn" eventually), while
Keno only ever needs a subset (20 of 80) -- this module draws by
popping a uniformly-chosen element out of a shrinking pool (spec Part
4.1's own words: "popping each selected number so duplicates are
structurally impossible"), which is the correct algorithm for a partial
draw and is not equivalent output-for-output to a full shuffle even
when both consume the same underlying random stream.

Everything here is a pure function of its inputs: no I/O, no database, no
unseeded randomness (except generate_server_seed(), which is the one
function whose entire job IS to produce fresh entropy). This is what
makes it auditable and unit-testable without any infrastructure, and it
is what makes the "the draw is independent of ticket data" guarantee
possible to prove in the type system: derive_draw()'s signature is
(server_seed, public_seed, pool_size, draw_count) -- there is no
parameter through which ticket/selection data could reach it. See
tests/unit/test_keno.py::test_derive_draw_signature_has_no_ticket_data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from fractions import Fraction
from math import comb

# ---------------------------------------------------------------------------
# Configuration constants (pool shape). Everything else -- picks offered,
# stakes, multipliers -- is admin/DB configuration, not a code constant.
# ---------------------------------------------------------------------------

NUMBER_POOL_SIZE = 80
DRAW_COUNT = 20

MIN_PICKS = 1
MAX_PICKS = 10

# Hard guardrails enforced in code, never only in the admin UI (spec 3.2).
RTP_FLOOR = Decimal("0.75")
RTP_CEILING = Decimal("0.97")

_MONEY_QUANT = Decimal("0.01")
_PROB_QUANT = Decimal("0.00000001")  # 8dp -- enough headroom for pick=10


class KenoError(Exception):
    """Base for every typed Keno domain exception."""


class InvalidPickCount(KenoError):
    def __init__(self, pick_count: int) -> None:
        self.pick_count = pick_count
        super().__init__(
            f"pick_count {pick_count} is outside [{MIN_PICKS}, {MAX_PICKS}]"
        )


class DuplicatePicks(KenoError):
    def __init__(self, picks: list[int]) -> None:
        self.picks = picks
        super().__init__(f"picks contain duplicates: {picks}")


class PickOutOfRange(KenoError):
    def __init__(self, value: int) -> None:
        self.value = value
        super().__init__(f"pick {value} is outside [1, {NUMBER_POOL_SIZE}]")


class PaytableRTPOutOfRange(KenoError):
    """Raised when a candidate paytable's computed RTP falls outside the
    configured [floor, ceiling] guardrail. Never bypassable by the admin
    UI alone -- this is the enforcement point every caller must go
    through before a paytable can be activated."""

    def __init__(self, pick_count: int, rtp: Decimal, floor: Decimal, ceiling: Decimal) -> None:
        self.pick_count = pick_count
        self.rtp = rtp
        self.floor = floor
        self.ceiling = ceiling
        super().__init__(
            f"pick_count={pick_count} RTP {rtp} outside [{floor}, {ceiling}]"
        )


# ---------------------------------------------------------------------------
# Ticket validation
# ---------------------------------------------------------------------------


def validate_picks(
    picks: list[int], *, min_picks: int = MIN_PICKS, max_picks: int = MAX_PICKS
) -> frozenset[int]:
    """Validate a player's chosen numbers. Returns a frozenset (order never
    matters, duplicates are structurally impossible in the return type).
    Raises a typed exception on any invalid input -- never silently
    truncates or de-dupes what the caller submitted."""
    count = len(picks)
    if not (min_picks <= count <= max_picks):
        raise InvalidPickCount(count)
    seen: set[int] = set()
    for value in picks:
        if not (1 <= value <= NUMBER_POOL_SIZE):
            raise PickOutOfRange(value)
        if value in seen:
            raise DuplicatePicks(picks)
        seen.add(value)
    return frozenset(seen)


def count_matches(picks: frozenset[int], drawn_numbers: list[int]) -> int:
    """How many of the player's picks appear in the drawn numbers. Pure,
    no knowledge of stakes/payouts -- settlement composes this with the
    paytable separately, so match-counting can never accidentally leak
    into the draw-generation code path."""
    return len(picks & set(drawn_numbers))


# ---------------------------------------------------------------------------
# Hypergeometric probability and RTP
# ---------------------------------------------------------------------------


def probability_exact_matches(
    picks: int,
    matches: int,
    *,
    pool_size: int = NUMBER_POOL_SIZE,
    draw_count: int = DRAW_COUNT,
) -> Decimal:
    """P(exactly `matches` of `picks` chosen numbers are among the
    `draw_count` numbers drawn from a pool of `pool_size`) -- the
    hypergeometric distribution:

        P(k | n) = C(draw_count, k) * C(pool_size - draw_count, n - k) / C(pool_size, n)

    Converted from the exact `Fraction` computation below to a Decimal
    for display/API convenience -- see `_probability_fraction()` for the
    exact form every money-critical computation (compute_rtp,
    validate_paytable_rtp) actually accumulates with, so per-value
    Decimal rounding here can never compound into a real guardrail
    decision.
    """
    return _fraction_to_decimal(_probability_fraction(picks, matches, pool_size=pool_size, draw_count=draw_count))


def _probability_fraction(
    picks: int,
    matches: int,
    *,
    pool_size: int = NUMBER_POOL_SIZE,
    draw_count: int = DRAW_COUNT,
) -> Fraction:
    """The exact rational hypergeometric probability -- Python's
    arbitrary-precision `math.comb` plus `Fraction`, zero rounding error
    at any stage. This is what compute_rtp/hit_frequency actually sum,
    so `sum(_match_distribution_fraction(n).values()) == Fraction(1)`
    holds exactly (by Vandermonde's identity), not just approximately."""
    if not (0 <= matches <= picks):
        return Fraction(0)
    non_drawn = pool_size - draw_count
    misses = picks - matches
    if misses > non_drawn or matches > draw_count:
        return Fraction(0)
    numerator = comb(draw_count, matches) * comb(non_drawn, misses)
    denominator = comb(pool_size, picks)
    return Fraction(numerator, denominator)


def _fraction_to_decimal(value: Fraction, *, precision: int = 40) -> Decimal:
    """Convert an exact Fraction to Decimal at generous precision (40
    significant digits -- far beyond ETB-cent precision at any realistic
    stake/multiplier) for display or a single final guardrail comparison.
    Money-critical accumulation must happen in Fraction space *before*
    calling this, not after -- see compute_rtp/hit_frequency."""
    if value == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = precision
        return Decimal(value.numerator) / Decimal(value.denominator)


def match_distribution(
    picks: int, *, pool_size: int = NUMBER_POOL_SIZE, draw_count: int = DRAW_COUNT
) -> dict[int, Decimal]:
    """P(k matches) for every possible k in [0, picks], as Decimal for
    display. See _match_distribution_fraction() for the exact form used
    by every guardrail computation."""
    return {
        k: _fraction_to_decimal(fraction)
        for k, fraction in _match_distribution_fraction(picks, pool_size=pool_size, draw_count=draw_count).items()
    }


def _match_distribution_fraction(
    picks: int, *, pool_size: int = NUMBER_POOL_SIZE, draw_count: int = DRAW_COUNT
) -> dict[int, Fraction]:
    upper = min(picks, draw_count)
    return {
        k: _probability_fraction(picks, k, pool_size=pool_size, draw_count=draw_count) for k in range(0, upper + 1)
    }


def compute_rtp(picks: int, multipliers: dict[int, Decimal]) -> Decimal:
    """RTP(n) = sum over k of P(k | n) * multiplier(n, k), accumulated in
    exact Fraction space and converted to Decimal exactly once at the
    end -- the guardrail this feeds (validate_paytable_rtp) must never be
    affected by accumulated Decimal rounding error across up to 11 summed
    terms.

    `multipliers` maps match-count -> payout multiplier (stake units); a
    missing key is treated as multiplier 0 (a non-winning match count).
    """
    return _fraction_to_decimal(_fraction_sum_rtp(picks, multipliers))


def hit_frequency(picks: int, multipliers: dict[int, Decimal]) -> Decimal:
    """P(this ticket wins anything at all) -- exact sum of P(k) over
    every k whose multiplier is > 0."""
    distribution = _match_distribution_fraction(picks)
    total = Fraction(0)
    for matches, probability in distribution.items():
        if multipliers.get(matches, Decimal(0)) > 0:
            total += probability
    return _fraction_to_decimal(total)


def max_multiplier(multipliers: dict[int, Decimal]) -> Decimal:
    if not multipliers:
        return Decimal(0)
    return max(multipliers.values())


def volatility(picks: int, multipliers: dict[int, Decimal]) -> Decimal:
    """Standard deviation of the per-unit-stake payout random variable.
    Display-only statistic for the admin paytable editor (Part 3.2), not
    a guardrail input -- but still accumulated as exact Fraction
    arithmetic (variance is just another weighted sum) for the same
    reason compute_rtp is: no reason to accept avoidable rounding error
    even on a display-only figure derived from real-money multipliers."""
    exact_rtp = _fraction_sum_rtp(picks, multipliers)
    distribution = _match_distribution_fraction(picks)
    variance = Fraction(0)
    for matches, probability in distribution.items():
        payout = Fraction(multipliers.get(matches, Decimal(0)))
        variance += probability * (payout - exact_rtp) ** 2
    return _decimal_sqrt(_fraction_to_decimal(variance))


def _fraction_sum_rtp(picks: int, multipliers: dict[int, Decimal]) -> Fraction:
    distribution = _match_distribution_fraction(picks)
    total = Fraction(0)
    for matches, probability in distribution.items():
        total += probability * Fraction(multipliers.get(matches, Decimal(0)))
    return total


def _decimal_sqrt(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 40
        return value.sqrt()


@dataclass(frozen=True)
class PaytableStats:
    picks: int
    rtp: Decimal
    hit_frequency: Decimal
    max_multiplier: Decimal
    volatility: Decimal


def compute_paytable_stats(picks: int, multipliers: dict[int, Decimal]) -> PaytableStats:
    return PaytableStats(
        picks=picks,
        rtp=compute_rtp(picks, multipliers).quantize(Decimal("0.0001")),
        hit_frequency=hit_frequency(picks, multipliers).quantize(Decimal("0.0001")),
        max_multiplier=max_multiplier(multipliers),
        volatility=volatility(picks, multipliers).quantize(Decimal("0.0001")),
    )


def validate_paytable_rtp(
    picks: int,
    multipliers: dict[int, Decimal],
    *,
    floor: Decimal = RTP_FLOOR,
    ceiling: Decimal = RTP_CEILING,
) -> Decimal:
    """The hard guardrail: refuses to let a paytable activate if its RTP
    for this pick count falls outside [floor, ceiling]. Callers (the admin
    paytable-save endpoint) must call this before persisting an 'active'
    paytable -- this is the enforcement point, not the UI."""
    rtp = compute_rtp(picks, multipliers)
    if not (floor <= rtp <= ceiling):
        raise PaytableRTPOutOfRange(picks, rtp, floor, ceiling)
    return rtp


def compute_payout(
    stake: Decimal,
    matches: int,
    multipliers: dict[int, Decimal],
    *,
    max_win_per_ticket: Decimal,
) -> Decimal:
    """stake * multiplier(matches), rounded down to the cent, capped at
    max_win_per_ticket. Rounding down (never up) means any sub-cent
    remainder favors the house, mirroring services/engine/settlement.py's
    own compute_derash()/split_derash() convention exactly."""
    multiplier = multipliers.get(matches, Decimal(0))
    raw = (stake * multiplier).quantize(_MONEY_QUANT, rounding=ROUND_DOWN)
    return min(raw, max_win_per_ticket)


# ---------------------------------------------------------------------------
# Commit-reveal draw
# ---------------------------------------------------------------------------


def generate_server_seed() -> bytes:
    """CSPRNG entropy, 32 bytes. The one function in this module whose job
    is to be non-deterministic -- every other function here is a pure
    function of its inputs."""
    return secrets.token_bytes(32)


def server_seed_hash(server_seed: bytes) -> str:
    """Published at round creation, before the server has seen a single
    ticket -- the commitment half of commit-reveal."""
    return hashlib.sha256(server_seed).hexdigest()


def compute_ticket_ids_rolling_hash(ticket_ids: list[int]) -> str:
    """A deterministic hash of every accepted ticket id, computed at
    betting close and folded into the public seed (see derive_public_seed).
    Sorted first so ticket arrival order -- which the server could
    otherwise influence -- can never change the resulting hash."""
    digest = hashlib.sha256()
    for ticket_id in sorted(ticket_ids):
        digest.update(str(ticket_id).encode("ascii"))
        digest.update(b":")
    return digest.hexdigest()


def derive_public_seed(round_id: int, betting_closed_at_epoch: int, ticket_ids_hash: str) -> str:
    """Built entirely from data that does not exist until betting closes
    (the round id, the real close timestamp, and a hash of every ticket
    accepted before close) -- the server commits to server_seed_hash
    *before* any of this exists, so it cannot choose a seed after seeing
    bets even in principle. See docs/keno/06-fairness-and-verification.md."""
    return f"{round_id}:{betting_closed_at_epoch}:{ticket_ids_hash}"


class _ByteStream:
    """HMAC-SHA256 counter-mode deterministic byte stream, buffered.
    The byte-construction and rejection-sampling logic here is
    byte-for-byte identical to packages/core/bingo.py's own _ByteStream
    (same HMAC(server_seed, client_seed || counter.to_bytes(4, "big"))
    construction, same below() rejection sampling) -- proven by
    tests/unit/test_keno.py::test_byte_stream_matches_bingo_byte_stream.
    Kept as an independent class (not a shared import) per this module's
    own docstring: Bingo's fairness-critical file is never touched by
    Keno work."""

    def __init__(self, server_seed: bytes, client_seed: bytes) -> None:
        self._server_seed = server_seed
        self._client_seed = client_seed
        self._counter = 0
        self._buffer = b""

    def take(self, n: int) -> bytes:
        while len(self._buffer) < n:
            block = hmac.new(
                self._server_seed,
                self._client_seed + self._counter.to_bytes(4, "big"),
                hashlib.sha256,
            ).digest()
            self._buffer += block
            self._counter += 1
        chunk, self._buffer = self._buffer[:n], self._buffer[n:]
        return chunk

    def below(self, upper_exclusive: int) -> int:
        """Unbiased random int in [0, upper_exclusive) via rejection
        sampling -- never modulo, which would bias low values whenever
        upper_exclusive doesn't evenly divide 256**num_bytes."""
        if upper_exclusive <= 0:
            raise ValueError("upper_exclusive must be positive")
        num_bytes = max(1, (upper_exclusive - 1).bit_length() // 8 + 1)
        limit = 256**num_bytes
        threshold = limit - (limit % upper_exclusive)
        while True:
            value = int.from_bytes(self.take(num_bytes), "big")
            if value < threshold:
                return value % upper_exclusive


def derive_keno_draw(
    server_seed: bytes,
    public_seed: str,
    *,
    pool_size: int = NUMBER_POOL_SIZE,
    draw_count: int = DRAW_COUNT,
) -> list[int]:
    """THE DRAW. Signature is exactly (server_seed, public_seed, pool_size,
    draw_count) -- no parameter through which ticket/selection data could
    reach this function. Do not add one. See PART 4.2 of the build spec
    and tests/unit/test_keno.py::test_derive_draw_signature_has_no_ticket_data,
    which fails the build if this signature ever grows a ticket-shaped
    parameter.

    Fisher-Yates draw via rejection-sampled pops from a shrinking pool --
    duplicates are structurally impossible (each number is removed from
    the pool the instant it's drawn), not merely avoided by a dedup step.
    Deterministic: identical (server_seed, public_seed, pool_size,
    draw_count) always reproduces the identical draw, which is what makes
    both independent verification and crash-recovery re-derivation exact.
    """
    stream = _ByteStream(server_seed, public_seed.encode("utf-8"))
    pool = list(range(1, pool_size + 1))
    drawn: list[int] = []
    for _ in range(draw_count):
        index = stream.below(len(pool))
        drawn.append(pool.pop(index))
    return drawn


def verify_draw(server_seed: bytes, public_seed: str, claimed_draw: list[int]) -> bool:
    """Independent verification: re-derive the draw from the revealed
    seeds and compare. This is the exact function both the API's
    /keno/rounds/{id}/verify route and scripts/verify-round.ts's logic
    (ported 1:1) must reproduce."""
    return derive_keno_draw(server_seed, public_seed, pool_size=NUMBER_POOL_SIZE, draw_count=len(claimed_draw)) == claimed_draw


def verify_server_seed_hash(server_seed: bytes, claimed_hash: str) -> bool:
    return hmac.compare_digest(server_seed_hash(server_seed), claimed_hash)
