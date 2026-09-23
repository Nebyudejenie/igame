# Keno — Economics and Bankroll Protection

> Business context (`keno.md`'s own framing): the operator is launching
> with limited capital (under 50,000 ETB reserve), expects high player
> volume, and typical stakes around 50 ETB. Everything in this document
> is a survival requirement, not decoration.

## The reserve

`keno_reserve` is a first-class, persisted, auditable ledger account —
not a number in a config file. It is funded only by explicit operator
deposit (`POST /keno/reserve/deposit`), grows with net GGR (stakes in,
minus payouts and jackpot diversion, both flowing through the same
ledger transaction as the ticket itself), and shrinks with payouts,
refunds, and explicit operator withdrawals. It reconciles exactly:

```
reserve = deposits + Σ stakes − Σ payouts − Σ refunds − Σ withdrawals
```

`ledger.reconcile()` — the same reconciliation check used everywhere
else in the platform — is asserted in every Keno integration test that
touches money, including under real concurrent load
(`tests/integration/test_keno_load_concurrent_tickets.py`) and across
a real crash-recovery boundary
(`tests/integration/test_keno_chaos_engine_crash.py`).

**The reserve and player liability are structurally different numbers
and must never be conflated** — the platform's own explicit warning,
repeated because operators die of this more often than of bad luck.
`GET /keno/dashboard` (see `03-api.md`) returns them as two separate
fields; `keno_reserve_balance` and `keno_player_liability` are two
separate Prometheus gauges, not one.

A withdrawal floor (`keno_configs.reserve_withdrawal_floor`) blocks and
audits any withdrawal that would drop the reserve below it — enforced
server-side (`409`, not silently allowed and not just a UI guard). At
launch this floor is `0` (the schema default) — the spec calls it
"configurable," not a specific number, and a real non-zero floor is a
business-risk-tolerance decision reserved for the operator, not
invented here.

## The risk tier ladder

Offered bets scale automatically with the reserve — the tiers
themselves are config rows (`keno_risk_tiers`), never code:

| Tier | Min. reserve (ETB) | Max picks | Top multiplier | Max stake | Max win/ticket |
|---|---|---|---|---|---|
| 1 | 0 | 5 | 16× | 50 | 800 |
| 2 | 200,000 | 6 | 60× | 50 | 3,000 |
| 3 | 1,000,000 | 8 | 500× | 100 | 50,000 |
| 4 | 5,000,000 | 10 | 5,000× | 200 | 250,000 |

These are `keno.md`'s own literal launch figures, seeded verbatim by
`c4e8f1a9b6d3`. **Why the 16× cap at Tier 1 matters**: with a reserve
in the tens of thousands of ETB and 50 ETB stakes, a low-variance table
keeps the daily swing near the daily expected profit and the reserve
compounds safely. A jackpot-scale multiplier at this reserve size would
push the daily swing an order of magnitude above what the reserve can
absorb — one hit and the operator cannot pay. This is why the top
multiplier is not something an admin can freely raise at launch without
also being blocked by the RTP guardrail and, structurally, by the tier
ladder itself.

Promotion up a tier requires the reserve to hold above the next tier's
threshold for **7 consecutive days** — deliberately, to prevent
flapping on a single lucky day (`keno_tier_state.candidate_tier_id`/
`candidate_since`, reset to `NULL` on any dip). Demotion is immediate,
and — like every tier change — never affects an in-flight round or an
already-accepted ticket, since `keno_rounds.tier_id` is pinned at
creation. See `01-architecture.md` for the automation mechanics and
`02-data-model.md` for `keno_tier_changes`, the full audit trail.

## Round-level exposure: why CLT, not worst-case

Before accepting a ticket, the platform checks whether doing so would
push the round's *estimated 99.9th-percentile aggregate payout* past
`reserve × max_round_exposure_pct` (10% for every tier at launch —
operator-approved; see `packages/core/keno_exposure.py`'s own module
docstring for the full derivation).

An earlier design summed each ticket's own *absolute worst-case*
payout. That's wrong, not just conservative: for every pick count
offered at launch, the probability of hitting the top prize already
exceeds the 0.1% tail budget (pick=1 matches ~25% of the time; even
pick=3 matches all 3 about 1.4% of the time) — so "worst case" and
"99.9th percentile" collapse to the same number, and summing worst
-cases across many tickets is exactly the "absolute worst case ...
unusable at this reserve size" the spec explicitly says to avoid.

The statistically correct tool is the **Central Limit Theorem**: track
the round's running expected payout (`Σ stake × RTP`) and running
variance (`Σ stake² × volatility²`), then estimate the 99.9th
-percentile aggregate as

```
expected + 3.09 × √variance
```

(3.09 being the standard normal distribution's 99.9th-percentile
z-score) — the same approach an insurer uses to size capital against a
book of many individually-small policies, and one that correctly
captures the diversification benefit of many tickets: `√variance`
grows with the square root of ticket count for roughly-independent
tickets, not linearly. Proven to serialize correctly under real
concurrent contention (300 simultaneous `place_ticket()` calls, tight
cap, never oversold) in `tests/integration/test_keno_load_concurrent_
tickets.py`.

One real approximation, documented rather than hidden: tickets within
the same round share one draw, so they're not *perfectly* independent,
and treating their variances as additive slightly understates the true
aggregate variance. In practice the correlation between two different
tickets' match counts is small unless their pick sets overlap heavily,
and this module's whole job is a fast, real-time, per-bet-feasible
*estimate* gating acceptance — the round's actual settlement always
uses the real draw and exact payout math regardless of what this
estimate said beforehand.

The identical mechanism, applied to just one user's own tickets this
round, enforces the per-user round-capacity share (default 20% of a
round's remaining capacity, `per_user_round_capacity_share_bps`) — with
one real bug an earlier version had, caught by the test suite: it must
be measured against the round's fixed exposure *ceiling*, not against
other players' stakes, or the very first ticket placed in any round
(which is, by definition, 100% of the stake so far) would make it
structurally impossible for anyone to ever place a round's first bet.

## Risk-of-ruin simulation

`packages/core/keno_risk_simulator.py` (admin-only, `POST
/keno/risk-of-ruin`, `keno:manage`, pure computation, nothing
persisted) runs a Monte Carlo across both simulation runs and days:
each day's own aggregate payout is drawn from the same CLT-derived
distribution the exposure check uses, scaled by `daily_handle /
avg_stake` tickets per day. Reports the distribution of ending reserve
(`p10`/`p50`/`p90`/mean), worst drawdown (mean and max), and the
probability of breaching a given floor. Verified directly: realistic
launch-scale inputs show ~0% ruin probability (correct — strong CLT
diversification against a genuine 18% house edge); a deliberately thin,
low-volume scenario shows real, nonzero ruin probability and drawdown,
proving the mechanism actually responds to its inputs rather than
always reporting zero.

**Known, flagged gap**: the simulator does not cover the rest of spec
7.4 — median session length, or handle-per-100-ETB-deposited compared
across candidate paytables (the spec's stated goal: "select the
paytable that maximizes handle per deposit, not the one with the
lowest RTP"). Neither figure can be grounded in anything real yet — no
Keno player-behavior data exists pre-launch — and this codebase's own
standing discipline is to flag a gap like this rather than invent a
number to fill it (the same discipline already applied to SantimPay/
ArifPay integration assumptions elsewhere in the platform).

## Target RTP and house edge

Both paytable profiles are solved to land at **82% RTP (18% house
edge)**, hard-guardrailed to never activate outside `[75%, 97%]` — see
`05-paytable-and-rtp.md` for the exact multiplier tables and the math
that produced them.

## The jackpot: player-funded, zero operator liability

A configurable slice of every stake — 1.5% at launch
(`jackpot_diversion_bps = 150`) — diverts into a progressive pool held
as its own separate ledger account (`keno_jackpot_pool`). This comes
**out of the payout budget, not on top of player cost**: base-game RTP
drops by the same 1.5% and returns through the jackpot, so total RTP
stays honest and disclosed rather than the jackpot being an extra hidden
cost.

The jackpot pays only from its own pool balance and structurally cannot
exceed it — `_pay_jackpot()` locks the pool row (`FOR UPDATE`), reads
the ledger balance fresh, and pays out exactly that balance, in the
same transaction as the win itself. Operator liability from the jackpot
is therefore zero by construction, not by policy.

**A real gap found and fixed while writing this document**: the
`keno_jackpot_pool` singleton metadata row (`cap_amount`,
`trigger_description`, `seed_amount` — distinct from the ledger
account holding the real balance) had never been seeded by any prior
migration; a direct check of the production database confirmed zero
rows. `_pay_jackpot()` handles a missing row gracefully (returns 0,
never crashes), so nothing was visibly broken — but that also meant
the jackpot could structurally never be paid out, even after real
contributions had grown the ledger balance, until this row existed.
Contributions weren't blocked by the missing row (they post through
the ledger account directly, independent of this table); only payout
was — a silent trap where the pool visibly grows and the first 5-for-5
winner gets nothing. Fixed by migration `f1a9d4c7e2b5`, seeding only
`account_id` (required) and leaving `cap_amount` uncapped and
`seed_amount` at 0 — both real money/product decisions left untouched,
not invented here, same reasoning as the reserve withdrawal floor's own
default. Verified with a real up/down/up migration cycle before
committing. **This fix has been committed and pushed to `igame` but not
yet deployed to the production server** — it should ship alongside (or
before) the `keno_enabled` go-live flip, not after.

## Revenue-model metrics — an honest status check

Spec Part 8's profit-optimization formula:

```
Monthly GGR = DAU × sessions/day × rounds/session × tickets/round × avg stake × house edge
```

All six terms have a corresponding Prometheus gauge declared in
`packages/core/metrics.py` (`keno_dau`, `keno_sessions_per_user`,
`keno_rounds_per_session`, `keno_tickets_per_round`, `keno_avg_stake`,
`keno_hold_pct`), alongside the rest of Part 8's required business
metrics (`keno_arpdau`, `keno_d1/d7/d30_retention`, `keno_player_ltv`,
`keno_deposit_conversion_rate`, `keno_session_duration_seconds`).

**Checked directly while writing this document — none of them are
currently populated anywhere outside their own declaration.** No
scheduled job, admin endpoint, or engine code path sets any of these
eleven gauges; every one of them would currently read as Prometheus's
default (effectively zero), not a real trailing figure. The gauges
exist; the analytics computation that should feed them does not yet.
This is a real, open gap against the spec's Definition of Done ("All
six revenue-model terms exposed as metrics") — flagged here rather
than left to be discovered later, and **not fixed in this documentation
pass**: building the actual cohort/retention/session-tracking
computation is a real engineering task (deciding what counts as a
"session," where D1/D7/D30 cohorts are tracked from, how LTV is
windowed), not something to invent defaults for while writing docs.
