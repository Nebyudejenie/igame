# Keno — Paytable and RTP

## The math

`packages/core/keno.py` computes everything from first principles —
nothing here is a lookup table or an approximation. A ticket picks `n`
numbers from an 80-number pool; the round draws 20. The number of
matches `k` follows the **hypergeometric distribution**:

```
P(k matches | n picks) = C(20, k) · C(60, n−k) / C(80, n)
```

computed exactly via Python's arbitrary-precision `math.comb` and
`Fraction` — never floating point, and never Decimal until the very
final step (`_fraction_to_decimal`, 40 significant digits). This
matters because RTP is a *sum* of up to 11 such terms
(`compute_rtp` = `Σ P(k) · multiplier(k)`), and the guardrail that
decides whether a paytable is allowed to go live reads that sum
directly — per-term Decimal rounding error compounding across 11 terms
is exactly the kind of thing that must never silently nudge a
real-money guardrail decision, so the whole computation happens in
exact rational (`Fraction`) space and is converted to `Decimal` exactly
once, at the end.

`compute_paytable_stats(picks, multipliers)` returns:

- **`rtp`** — expected return per unit staked.
- **`hit_frequency`** — `P(this ticket wins anything at all)`, summed
  over every match count whose multiplier is `> 0`.
- **`max_multiplier`** — the top prize.
- **`volatility`** — standard deviation of the per-unit-stake payout,
  also computed in exact `Fraction` space (variance is just another
  weighted sum) even though it's a display-only figure for the admin
  paytable editor.

Payout itself: `compute_payout()` rounds **down** to the cent
(`ROUND_DOWN`, never up — any sub-cent remainder favors the house,
matching `services/engine/settlement.py`'s own `compute_derash()`
convention exactly) and caps at the tier's `max_win_per_ticket`.

## The guardrail

`validate_paytable_rtp(picks, multipliers, floor=0.75, ceiling=0.97)`
is the one enforcement point: a paytable whose computed RTP falls
outside `[75%, 97%]` cannot be activated. This runs server-side inside
`POST /keno/paytables` (see `03-api.md`) — it is **not** just a UI
constraint an admin could route around by calling the API directly.

## The launch paytables (as seeded)

Seeded by migration `c4e8f1a9b6d3`, every table's multipliers were
solved to land at the spec's **82% target RTP**, verified inside the
guardrail, for every pick count 1–10 in both profiles offered at
launch. The four figures in **bold** are `keno.md`'s own literal
"do not raise at launch" anchors — every other multiplier was solved
around them, not chosen freely, specifically so the ladder is
**monotonically increasing in top prize by pick count** within each
profile (a first design pass wasn't: an unconstrained solve let
picks=4's top multiplier exceed picks=5's fixed anchor, and picks=9
exceed picks=10's; fixed by anchoring every pick count's top multiplier
explicitly before solving the rest, then verifying monotonicity
programmatically).

### `low_variance` (Tiers 1–2, picks 1–6)

| Picks | Multipliers (match → ×stake) | RTP | Hit freq. | Top prize |
|---|---|---|---|---|
| 1 | 1→3.28 | 82.00% | 25.00% | 3.28× |
| 2 | 1→1.21, 2→6.00 | 82.03% | 43.99% | 6.00× |
| 3 | 1→0.50, 2→3.35, 3→10.00 | 81.90% | 58.35% | 10.00× |
| 4 | 1→0.34, 2→1.62, 3→6.73, 4→13.00 | 82.25% | 69.17% | 13.00× |
| 5 | 1→0.19, 2→0.97, 3→3.89, 4→11.67, 5→**16.00** | 81.74% | 77.28% | **16.00×** |
| 6 | 1→0.15, 2→0.60, 3→2.25, 4→7.51, 5→21.04, 6→**60.00** | 81.88% | 83.34% | **60.00×** |

Hit frequency clears the spec's "50%+ at 4–5 picks" claim comfortably
(69% / 77%).

### `standard` (Tiers 3–4, picks 1–10)

| Picks | Multipliers (match → ×stake) | RTP | Hit freq. | Top prize |
|---|---|---|---|---|
| 1 | 1→3.28 | 82.00% | 25.00% | 3.28× |
| 2 | 2→13.64 | 82.01% | 6.01% | 13.64× |
| 3 | 2→2.95, 3→29.55 | 81.93% | 15.26% | 29.55× |
| 4 | 3→9.19, 4→137.89 | 81.99% | 4.63% | 137.89× |
| 5 | 3→4.20, 4→25.21, 5→252.07 | 81.99% | 9.67% | 252.07× |
| 6 | 3→2.13, 4→10.66, 5→63.94, 6→319.71 | 81.99% | 16.16% | 319.71× |
| 7 | 3→0.78, 4→5.20, 5→31.18, 6→181.91, 7→400.00 | 82.02% | 23.66% | 400.00× |
| 8 | 3→0.56, 4→2.78, 5→13.89, 6→69.45, 7→333.36, 8→**500.00** | 82.11% | 31.71% | **500.00×** |
| 9 | 4→1.11, 5→7.40, 6→44.38, 7→258.90, 8→1331.47, 9→2500.00 | 82.01% | 15.31% | 2500.00× |
| 10 | 4→0.60, 5→3.60, 6→21.00, 7→120.01, 8→660.07, 9→3600.41, 10→**5000.00** | 81.99% | 21.20% | **5000.00×** |

`standard` trades hit frequency for volatility as pick count grows
past 6 — by design: fewer, bigger wins at the higher tiers, which only
unlock once the reserve is large enough to absorb them (see
`07-economics-and-bankroll.md` for the tier ladder's own reserve
thresholds).

## Which tier offers which profile

| Tier | Min. reserve | Max picks | Profile | Max win/ticket |
|---|---|---|---|---|
| 1 | 0 | 5 | `low_variance` | 800 |
| 2 | 200,000 | 6 | `low_variance` | 3,000 |
| 3 | 1,000,000 | 8 | `standard` | 50,000 |
| 4 | 5,000,000 | 10 | `standard` | 250,000 |

A round's tier — and therefore its paytable, pick range, and stake
ladder — is **pinned at round creation** and never re-read; an admin
promoting/demoting the live tier, or editing a paytable, can never
retroactively change what an in-flight round already offered.

## Editing a paytable later

Both `POST /keno/paytables/preview` (compute-only, no persistence,
`keno:manage`) and `POST /keno/paytables` (persists a new version,
`keno:configure`) run the identical guardrail before activation. A
paytable that would need to price above the historical anchors above,
or drop RTP outside `[75%, 97%]`, is rejected server-side — the
guardrail exists so a mis-typed multiplier can't accidentally launch a
paytable that bankrupts the reserve or gives the house an unreasonably
large edge.
