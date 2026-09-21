# Keno build — progress / handoff

Read this first in any new session before touching Keno. Each entry:
what's done, the evidence it's done, and the concrete next step.

## 2026-09-21 — Repo consolidation + full spec compliance audit (Parts 3/4/6/7)

**Done:**
- Confirmed `~/AradaBingo` (github.com/Nebyudejenie/game) has nothing left
  to merge — `git log 05e531a..aradabingo/main` is empty, working tree
  clean. `igame` (github.com/Nebyudejenie/igame) is now the sole active
  repo; `~/AradaBingo` is archived, not deleted. See README.md's own
  banner and DECISIONS.md's 2026-09-21 "Single source of truth" entry.
- Recreated the dev `jobingo-postgres`/`jobingo-redis` containers from
  `~/game`'s own `deploy/docker-compose.yml` (named volumes preserved,
  data verified identical before/after). `test_wal_archiving_supports_
  point_in_time_recovery` now genuinely passes — was failing before
  purely because the old containers' WAL archive bind-mounted to
  `~/AradaBingo/backups/wal_archive` instead.
- Full regression suite reran clean against the recreated database:
  **1551 passed**, 2 failed — both confirmed non-deterministic under
  full-suite concurrent load (SMS racy job-fetch test, one round-engine
  test), each passes 3-4/4 in isolation. Not caused by the container
  recreation or any Keno work.
- Simulated players money-flow fully traced with code citations
  (relayed to operator in-session, not yet written to a doc file —
  **next step**: promote that explanation into `docs/keno/
  simulated_players.md` or fold into `docs/keno/compliance.md` once
  that file exists). Headline: a simulated player CAN win a real shared
  Bingo pot (no special-casing anywhere in round_engine.py); its stake
  is house_float-funded but fully commingled with real players' stakes
  in the same `pot_escrow` account; its presence is NOT disclosed to
  real players anywhere in the client. Keno is structurally blocked for
  simulated players (`SimulatedPlayerCannotPlaceKenoTicket`, single
  enforcement point, no alternate insert path).
- **Full compliance audit of Parts 3, 4, 6, 7 complete** — see
  DECISIONS.md's 2026-09-21 audit entry for the full checklist. Ran
  every relevant test file for real (`pytest ... -q`), not assumed.

**Top-line finding — launch-blocking on its own:** the Keno round engine
is never wired into any deployed/running process.
`deploy/docker-compose.prod.yml` has zero Keno services;
`services/engine/worker.py` (the real `engine-worker` entrypoint) only
claims Bingo rooms and never imports `KenoRoundEngine`. Every "PASS" in
the audit describes code that's genuinely solid but only ever exercised
by tests manually driving it — nothing creates a Keno round outside a
test process today.

**Other confirmed gaps** (full detail + file:line evidence in
DECISIONS.md):
- `server_seed` is returned by `GET /api/keno/rounds/{id}` for **any**
  round status, not gated to terminal rounds like `verified` is — a
  real commit-reveal integrity leak (code-review finding, not yet
  reproduced live).
- Settlement is literally N+1 transactions (one `pool.acquire()` +
  `conn.transaction()` per ticket) — the spec names this exact
  anti-pattern as something to avoid.
- No admin-facing way to deposit into or withdraw from `keno_reserve`
  at all (`keno_reserve_deposit`/`keno_reserve_withdrawal` transaction
  kinds exist in the schema, never posted anywhere outside a test).
  Withdrawal floor: nothing to enforce, since withdrawal doesn't exist.
- Automated tier promotion (7-consecutive-day threshold) / demotion and
  the daily payout circuit breaker are unbuilt — `keno_tier_state`'s own
  `candidate_tier_id`/`candidate_since` columns exist for this and are
  never written by anything except a manual admin override, which
  always resets them to NULL.
- `keno_round_exposure_ratio`, `keno_player_liability`,
  `keno_reserve_balance` metrics are declared but never `.set()`
  anywhere. `deploy/prometheus/alerts.yml` has zero Keno rules.
- Only the `low_variance` paytable profile exists anywhere (code or
  tests); `standard` (required for Tier 3+) has never been created.
- No risk-of-ruin simulator exists in `services/admin/` at all.
- No production seed/bootstrap for the Part 7.5 launch config
  (Tier 1 + low_variance paytable + configs) — only present in test
  fixtures. An operator would have to hand-enter every value via the
  admin API before Keno could run anywhere, and it still wouldn't run
  (see top-line finding).
- `scripts/verify-round.ts` and 11 of 12 required `docs/keno/*.md` files
  (only `00-discovery.md` exists) don't exist.
- Keno never checks `packages/core/responsible_gaming.py` at all — a
  self-excluded or over-limit player can place Keno tickets freely
  today. This is Part 3.3's own "mandatory" per-user daily/loss limit,
  not just a Part 14 nice-to-have — it's also the literal top item Part
  14 needs to close before real money.
- Two internal (non-UI, non-doc) "provably-fair" wording lapses found:
  `web/miniapp/js/keno.js:577`'s comment and
  `tests/integration/test_keno_miniapp_e2e.py:201`'s comment. Player
  -facing UI strings (`locales/*.json`'s `fairness.*` keys) are already
  correctly worded ("verifiable commit-reveal," never "certified" or
  "provably fair") — no UI fix needed, only these two comments plus
  commit-message discipline going forward.

**Next step:** operator to choose sequencing for the (large) "missing
Part 7 items" list above before Part 14 starts — see the
in-conversation report for the proposed priority order (wire the engine
into a real process + fix the server_seed leak first, as the two
items with no real design decision attached; the rest — reserve
deposit/withdraw, tier automation, circuit breaker, standard profile,
risk-of-ruin simulator — need the operator's own input on shape/values
before building). Also still open: confirming production's actual
deploy source (no SSH access to the production host from this
environment — see DECISIONS.md's same-day entry for the circumstantial
case that it's `github.com/Nebyudejenie/game`, not yet confirmed
directly).

**Keno remains fully kill-switched / unreachable in production** — per
the top-line finding, this is currently true by construction (nothing
runs it), not merely by configuration.
