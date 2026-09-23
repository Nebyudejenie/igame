# Keno — Compliance

This document lists what is technically true about Keno's fairness,
risk controls, and record-keeping, as of 2026-09-23. It is **not**
a legal opinion and does not establish that operating Keno is lawful
in any jurisdiction — see the flag at the bottom.

## Shipped RTP

Target RTP: **82%** (18% house edge), for both paytable profiles
offered at launch (`low_variance`, Tiers 1–2; `standard`, Tiers 3–4).
Every individual pick count's actual computed RTP is between 81.74%
and 82.25% — see `05-paytable-and-rtp.md` for the exact per-pick-count
table. A hard, server-side guardrail (`packages.core.
keno.validate_paytable_rtp`) refuses to activate any paytable whose
computed RTP falls outside **[75%, 97%]**, regardless of who requests
it or through which interface — this is enforced in the code path
itself, not only in an admin UI that could be bypassed by calling the
API directly.

RTP is computed exactly — hypergeometric probability in arbitrary
-precision rational (`Fraction`) arithmetic, never floating point, only
converted to `Decimal` once at the very end — not estimated by
simulation or approximation. See `05-paytable-and-rtp.md` for the full
method.

## Risk limits

- **Per-round exposure cap**: 10% of the current reserve balance
  (`max_round_exposure_pct`), checked before every ticket acceptance
  against a Central-Limit-Theorem estimate of the round's own
  99.9th-percentile aggregate payout — not an absolute worst-case sum,
  which the spec itself identifies as unusable at this reserve size.
  See `07-economics-and-bankroll.md`.
- **Per-user round-share cap**: a single user may consume at most 20%
  of a round's remaining exposure capacity (`per_user_round_capacity_
  share_bps`).
- **Per-ticket win cap**: `max_win_per_ticket`, ranging from 800 ETB at
  Tier 1 to 250,000 ETB at Tier 4 — enforced in `compute_payout()`
  itself, applied after the paytable multiplier and before any money
  moves.
- **Risk-tier ladder**: offered pick counts, top multipliers, and
  stakes all scale automatically with the reserve balance (4 tiers,
  `keno_risk_tiers`), never manually and never overridable by a
  paytable edit alone. Promotion requires 7 consecutive days above a
  tier's threshold (preventing flapping on a single lucky day);
  demotion is immediate.
- **Daily payout circuit breaker**: automatically demotes one tier if
  trailing-24h actual payouts exceed a configured multiple of expected
  payout (`daily_payout_circuit_breaker_multiple`, 3.0× at launch).
- **Reserve withdrawal floor**: an admin cannot withdraw the reserve
  below a configured floor (`0` at launch — see `07-economics-and
  -bankroll.md` for why that specific default was chosen and what it
  means). Blocked attempts are refused (`409`) and still audited.
- **Jackpot**: player-funded (1.5% stake diversion), pays only from its
  own ledger-backed pool balance, and cannot structurally exceed it —
  operator liability from the jackpot is zero by construction. See
  `07-economics-and-bankroll.md` for a real gap found and fixed in this
  mechanism's own seed data.
- **Kill switch**: a single superadmin-only toggle
  (`POST /keno/kill-switch`) instantly blocks new ticket acceptance
  platform-wide.

## Fairness and independent verifiability

Every draw is CSPRNG-derived, seed-committed before a single ticket is
accepted, persisted immutably (enforced by a database trigger, not
just application discipline), and independently verifiable by anyone
— including a player — using only data the platform already publishes,
via either the API's own verification payload or the standalone,
dependency-free `scripts/verify-round.ts`. Full detail in
`06-fairness-and-verification.md`.

## Audit trail

Three separate, append-only logs, none of them optional or
best-effort:

- **`keno_round_events`** — every round status transition, timestamped,
  with the worker that performed it.
- **`keno_tier_changes`** — every tier change, automated or manual,
  with its trigger, reason, and the reserve balance at the time.
- **`admin_audit_log`** (the platform's existing, shared admin audit
  table) — every admin config change, paytable edit, tier override,
  reserve deposit/withdrawal, and kill-switch flip, with the admin's
  identity, a required human-readable reason, and the request's IP
  address.

Every real-money-moving admin action (reserve deposit, reserve
withdrawal) is audited **even when refused** — a withdrawal blocked by
the floor still writes an audit row recording the attempt, not only
successful transfers. See `08-runbook.md` for how to read each log.

## Data retention

**No automated data-retention or purge policy currently exists**,
for Keno or the platform generally — checked directly against the
codebase, not assumed. Every round, ticket, ledger transaction, and
audit-log row is retained indefinitely by default; there is no
scheduled job or admin tool that deletes or archives old Keno records.

This may be entirely appropriate (financial/gaming audit trails are
frequently required to be kept, not purged) or may need a defined
policy depending on applicable data-protection law (e.g., a
requirement to anonymize or delete personal data after some period
regardless of financial-record retention needs) — this is a legal
question this document does not attempt to answer, flagged here as an
open item rather than silently assumed to be fine either way.

## Regulatory licensing — explicitly flagged, not assumed

**Whether operating Keno (a real-money numbers game) requires a
license from the Ethiopian National Lottery Administration or any
other applicable regulatory authority is a business and legal
question, entirely outside the scope of this codebase and this
engineering work.** Nothing in this project — the code, these
documents, or the fact that the technical Definition of Done has been
met — constitutes legal advice, a compliance determination, or
confirmation that launching Keno is lawful. This is the operator's own
responsibility to determine, with qualified legal counsel, before
`keno_enabled` is ever set to `true` for real players. As of
2026-09-23, `keno_enabled` remains `false` in production specifically
because this decision, among others, has not been made — see
`docs/keno/PROGRESS.md`'s own "Still open" section for the complete,
current list of what remains before go-live.
