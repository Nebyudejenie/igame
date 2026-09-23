# Keno — Data Model

All tables live in the same Postgres database as Bingo, with no shared
foreign keys into Bingo's `rooms`/`rounds`/`round_entries` (see
`00-discovery.md` §C). Every table is created across three migrations:
`a3f7c2e91b04` (core schema), `d1f4b7c92a5e` (autoplay), `d7a2f5c8e1b4`
(tier-change audit) plus one column addition in `e9c3b7f2a5d8` (reserve
withdrawal floor). `c4e8f1a9b6d3` seeds the real production launch
data (config, tier ladder, paytables) rather than adding schema.

## Versioning convention

`keno_configs`, `keno_risk_tiers`, and `keno_paytables` are all
**insert-only versioned**: a "change" is a new row with a later
`effective_from`, never an `UPDATE`. Readers always take the row with
the latest `effective_from <= now()`. Critically, `version` is scoped
**independently per key** — per `tier_number` for tiers, per
`pick_count` for paytables — not a shared ladder-wide generation. (A
real bug this project's own tests caught: code that filtered "the rest
of the ladder" by `WHERE version = current_tier.version` would
silently stop seeing every other tier the moment any single tier was
edited. Correct pattern: `DISTINCT ON (key) ... ORDER BY key,
effective_from DESC`.)

`keno_rounds.config_id`/`tier_id` are pinned once at round creation and
never re-read — an admin changing a live config or tier can never alter
an in-flight round's rules.

## Configuration

### `keno_configs`
One row per config version. Pool shape (`number_pool_size=80`,
`draw_count=20`), round timing (`round_cycle_seconds`, `betting_seconds`,
`draw_seconds`, `result_seconds`), betting limits
(`min_picks`/`max_picks`, `max_tickets_per_user_per_round`,
`per_user_round_capacity_share_bps`), the jackpot cut
(`jackpot_diversion_bps`), the hard RTP guardrail
(`rtp_floor_bps`/`rtp_ceiling_bps`, 7500–9700 by default — enforced in
code, not just the admin UI), the circuit-breaker multiple
(`daily_payout_circuit_breaker_multiple`), the reserve-withdrawal floor
(`reserve_withdrawal_floor`, added by `e9c3b7f2a5d8`, default 0), and
the master `keno_enabled` flag that gates the whole game.

### `keno_paytables`
One row per `(pick_count, version)` — note: **not** per `(pick_count,
profile, version)`; two profiles sharing a pick count must use
different version numbers, a real constraint this project's own
production seed migration hit and fixed. Stores the multiplier table
itself (`multipliers` jsonb, match-count → payout multiplier) plus a
snapshot of `packages.core.keno.compute_paytable_stats()` at save time
(`computed_rtp_bps`, `hit_frequency_bps`, `max_multiplier`,
`volatility`) — snapshotted so a historical round's pinned paytable can
be audited later even if the guardrail code's constants ever change.

### `keno_risk_tiers`
One row per `(tier_number, version)`. Each tier defines: the minimum
reserve balance to qualify (`min_reserve`), the maximum pick count and
top multiplier allowed at this tier (`max_pick_count`,
`max_top_multiplier`), the allowed stake ladder (`stake_options`, an
array), the per-ticket win cap (`max_win_per_ticket`), the round
exposure ceiling as a fraction of reserve (`max_round_exposure_pct`,
`numeric(5,4)` — only 4 decimal digits of precision, which matters at
very large reserve balances), and which paytable profile this tier
uses (`paytable_profile`, `low_variance` or `standard`).

### `keno_tier_state`
Singleton row (`id` fixed at 1). Tracks the promotion/demotion state
machine: `current_tier_id`, plus `candidate_tier_id`/`candidate_since`
for the 7-consecutive-day promotion hold (reset to `NULL` the moment
the reserve dips back below the candidate tier's threshold).

### `keno_tier_changes`
Append-only audit trail for **every** tier change, automated or
manual — `admin_audit_log` can't hold this because its `admin_id` is
`NOT NULL`, structurally admin-actor-only, and an automated
promotion/demotion/circuit-breaker trip has no admin to attribute it
to. `trigger` is one of `automated_promotion`, `automated_demotion`,
`circuit_breaker`, `admin_override`; `from_tier_id`/`to_tier_id`,
`reason`, and `reserve_balance_at_change` are captured on every row.
An admin override also still writes its own `admin_audit_log` row —
this table is the single place to see full tier history either way.

## Round lifecycle

### `keno_rounds`
The state machine (`status`: `scheduled → betting_open → betting_closed
→ drawing → draw_complete → settling → completed`, or `failed`/`voided`).
A partial unique index (`ux_keno_rounds_single_betting_open`) enforces
at the database level that **at most one round may be `betting_open` at
a time** — not just an operational assumption resting on the engine's
own lock discipline.

Fairness columns: `server_seed` (bytea, revealed only after the draw),
`server_seed_hash` (published at round open — the commitment),
`public_seed` (text, fixed the instant betting closes), `ticket_ids_hash`
(feeds into `public_seed`), `drawn_numbers` (smallint[], **immutable
once set** — enforced by a real trigger,
`trg_keno_rounds_drawn_numbers_immutable`, not just application
discipline), `reveal_index` (how many of `drawn_numbers` have been
broadcast so far, letting a crashed reveal resume mid-animation rather
than restart).

Exposure accounting: `total_expected_payout` and `total_payout_variance`
are running Central-Limit-Theorem accumulators (sum of each accepted
ticket's own contribution); `projected_exposure` is the derived display
value (`expected + 3.09·√variance`) at the time of the last accepted
ticket. See `07-economics-and-bankroll.md`.

### `keno_round_events`
Append-only per-round audit trail of every status transition
(`from_status`, `to_status`, `worker_id`, `reason`, `created_at`) —
Keno's own domain-event log, distinct from `admin_audit_log` (which
covers admin actions only).

## Tickets

### `keno_tickets`
One row per ticket. `paytable_id` pins the exact paytable version this
ticket was priced against (never re-derived later even if the paytable
changes). `expected_payout_contribution`/`payout_variance_contribution`
are this ticket's own snapshot into the round's exposure accumulators.
`status` (`pending → won|lost|refunded`), `matches`, `payout` (base-game
only), `jackpot_payout` (recorded separately so the ticket row alone —
not just the ledger — shows the full outcome, per the platform's "a
completed round must be fully reconstructable from the database alone"
requirement). `idempotency_key` is `UNIQUE NOT NULL` — the actual
enforcement mechanism for "a retry returns the original ticket, never
double-charges," not just an application-level check. `stake_txn_id`/
`payout_txn_id` point at the actual `ledger_transactions` rows.
`autoplay_session_id` (nullable, added by `d1f4b7c92a5e`) records which
autoplay session placed this ticket, if any — purely informational,
since autoplay reuses the exact same `place_ticket()` path as a manual
bet.

### `keno_ticket_selections`
One row per `(ticket_id, number)` — the player's actual picks.
Duplicate picks within one ticket are structurally impossible (this is
the table's own primary key), not merely rejected by application
validation.

## Jackpot

### `keno_jackpot_pool`
Singleton row. The real money balance is **not** duplicated here — it
lives in the ledger's own `account_balances` row for the
`keno_jackpot_pool` system account (`account_id` on this row points at
it). This table only holds jackpot-specific configuration the ledger
has no natural column for: `seed_amount`, `cap_amount` (nullable — no
cap if unset), `trigger_description`, `overflow_pool_balance` (for
contributions that would push the pool past its cap).

## Autoplay / multi-race

### `keno_autoplay_sessions`
One server-driven mechanism covers both "autoplay with stop-on-win/
stop-on-loss" and "multi-race" (buy N future rounds up front) — the
same underlying need: a session with `picks`/`stake` plus an optional
`rounds_total` (multi-race: a fixed count; `NULL`: run until stopped or
a threshold fires) that auto-places a real ticket via the *same*
`place_ticket()` path every time a round opens. Debited per-round as
each ticket actually places — no escrow, no refund logic needed for
rounds that never happen if the balance runs out partway through.
`stop_on_win_amount`/`stop_on_loss_amount` are checked against
`net_position` once a placed ticket *settles*, not at placement time.
A partial unique index (`ux_keno_autoplay_one_active_per_user`)
enforces at most one active session per user. `status`: `active →
stopped|exhausted|failed`, with `stop_reason` recording exactly which
condition ended it (including, for a rejection, the specific
`TicketRejected` subclass's own error code).

## Ledger integration

No second wallet. Keno posts through the platform's existing
`packages/core/ledger.py`, via two backward-compatible
CHECK-constraint widenings on tables that already existed:

- `accounts.kind` gained `keno_reserve` and `keno_jackpot_pool`
  (both ordinary ledger accounts — the reserve is not a special table,
  it's a balance like any other).
- `ledger_transactions.kind` gained `keno_stake`, `keno_payout`,
  `keno_refund`, `keno_jackpot_contribution`, `keno_jackpot_payout`,
  `keno_reserve_deposit`, `keno_reserve_withdrawal`.

Every pre-existing value in both constraints is preserved verbatim —
proven round-trip-safe by `tests/integration/test_keno_migration.py`,
which inserts one row of every pre-existing kind before and after the
migration (and again after downgrade) and confirms nothing pre-existing
ever stops working.
