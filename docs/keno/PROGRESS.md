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
- ~~Two internal "provably-fair" wording lapses~~ **fixed same day** —
  see below.

**Next step (as of the audit, now superseded by the entry below):**
operator to choose sequencing for the "missing Part 7 items" list.
Operator chose: start the three no-decision items immediately, come back
for the design-heavy ones after.

---

## 2026-09-21 (later same day) — The three no-decision Part 7 items closed; UX research delivered; operator paused deploy-source work

**Done, all proven live against the real dev database, not just by
test** (full detail + every code citation in DECISIONS.md's own entry
right below the audit one):
- `server_seed`/`drawn_numbers` no longer leak pre-settlement
  (`round_detail()` now gates both on terminal status). New test proves
  it against a round seeded in `'drawing'` status specifically.
- Settlement batched into one transaction per round (was one per
  ticket). New test: 5 users, 1 round, all settle together correctly.
- `services/engine/keno_worker.py` created — the real, previously
  -missing production entrypoint. **Ran it live** against the dev DB:
  113 real rounds created and completed, `ledger.reconcile()` clean
  afterward.
- That live run caught **two more real bugs**, both fixed and
  reverified live: (1) `SIGTERM` didn't actually stop the engine (only
  an outer polling loop), needed `SIGKILL`, left the Redis lock held —
  fixed, now exits in ~1.4s and releases the lock cleanly; (2)
  `recover_on_startup()` only ever ran once at boot, so a round orphaned
  in `'settling'` mid-session (a swallowed exception in its detached
  settlement task) was never revisited until a full process restart,
  contradicting that code's own comment — fixed with a periodic sweep
  scoped specifically to `'settling'` (the only status that can
  legitimately exist outside the one round the engine is actively,
  synchronously driving at any moment). Also found and fixed a *third*
  bug while writing that fix's own tests: the pre-existing age-based
  recovery path would have refunded an old stuck `'settling'` round
  instead of retrying it, contradicting spec 5.3's own text and
  potentially shortchanging a real winner.
- Two internal "provably fair" wording lapses fixed (`keno.js`, a test
  comment) to spec 4.3's own "verifiable commit-reveal" language.
  Player-facing UI strings needed no change — already correct.
- Full suite reran clean: **1556 passed** (5 more than the prior entry,
  exactly the new tests above), 2 failed, both reconfirmed as
  pre-existing full-suite-load flakiness (passed 3/3 in isolation each),
  unrelated to this work.

**Deep research on international/real-world Keno UX and advanced
features delivered** (operator asked for this, separately from the Part
7 work) — full findings relayed in-conversation, not yet written to a
docs file. Top-prioritized, ranked by player-experience impact vs. build
cost:
1. Autoplay with stop-on-win/stop-on-loss conditions + a round counter
   — the single biggest gap vs. real products; **needs backend**
   (server-enforced stop conditions, round-queueing).
2. Overdue-numbers stat + frequency heatmap added to the existing
   hot/cold screen — cheap, pure frontend.
3. Variable draw-reveal pacing (fast early, dramatic on the final
   numbers near a match) + per-match sound stings — pure frontend.
4. Favorite/saved number sets — frontend + light backend (per-user
   saved-set storage).
5. Colorblind-safe state encoding (shape+color, not color alone) for
   selected/drawn/matched — pure frontend; the board already partly
   does this (checkmark/star badges), worth auditing against WCAG
   4.5:1 contrast specifically.
6. Contextual live paytable (updates inline as numbers are picked,
   collapses to a modal for the full table) — pure frontend.
7. Jackpot ticker + "last winner" banner — pure frontend once the
   backend exposes a jackpot-value/winner-event feed (mostly exists).
8. Server-enforced loss limits + a reality-check modal with session
   round-count — **overlaps directly with the still-open
   `responsible_gaming` gap** from the compliance audit; treat as one
   piece of work, not two.
9. Multi-race (buy N future rounds at once) — **needs backend**
   (round-queueing/ledger changes); bundle with #1, same subsystem.
10. Provably-fair-verification UI polish (one-click copy of seed/hash,
    a clear step-by-step reveal) — pure frontend, leverages what's
    already built.

Niche/skip for now (real but not worth building): Way/Combo ticket
math, number-pair correlation stats, Cleopatra-style bonus-round
mechanics.

**Operator's most recent instruction: deprioritize the production
deploy-source investigation for now** ("not now for deploy") in favor of
this UX work. That item (DECISIONS.md's earlier same-day entry —
circumstantial case that `github.com/Nebyudejenie/game` is/was the
source, not directly confirmed, no SSH access from this environment)
stays open, untouched, no further action pending.

**Next step**: operator has not yet said which of the 10 prioritized UX
items to actually build, or whether to return to the remaining
design-heavy Part 7 items (reserve deposit/withdraw, tier automation,
circuit breaker, standard paytable profile, risk-of-ruin simulator)
first. Both are open; ask before starting either at scale, since several
UX items need backend/economics decisions (autoplay stop-conditions,
multi-race purchasing, loss-limit enforcement) that shouldn't be built
blind.

**Keno remains fully kill-switched / unreachable in production** — per
the audit's top-line finding, this was true by construction (nothing
ran it); it now runs for real in dev via `services/engine/keno_worker.py`,
but nothing wires that into `docker-compose.prod.yml` yet (deliberately
— that's Part 18's own staged-deploy work, still gated behind the rest
of this list).
