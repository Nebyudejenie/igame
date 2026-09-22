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

---

## 2026-09-21 (later still) — Responsible gaming + autoplay/multi-race: backend done, UI not started

Operator answered the UX-scope question from the entry above: walk
through the backend/economics decisions for autoplay/multi-race and
loss limits *before* any UI work, build it in one pass once decided.
Decisions made and approved: daily loss cap combined across Bingo+Keno
(not per-game); autoplay-with-stop-conditions and multi-race are one
mechanism (`keno_autoplay_sessions`), debited per-round, not escrowed
upfront. Full detail + every citation in DECISIONS.md's own entry
directly below the Part 7 audit one.

**Done, all proven with real tests against the real dev database:**
- `keno_tickets.py::_place_ticket()` now checks `responsible_gaming.
  check_stake_allowed()` (closes the spec 3.3 gap the same day's earlier
  audit found) — same advisory-lock pattern `round_engine.py::join()`
  already uses, for the same cross-game-race reason.
  `today_net_loss()` now sums both games' stake/payout kinds together.
- `packages/core/keno_autoplay.py` — the full autoplay/multi-race
  mechanism, wired into `keno_round_engine.py`'s own `_open_betting()`/
  `_settle_tickets()`. Three new REST endpoints (`POST`/`DELETE`/
  `GET /api/keno/autoplay`).
- Two real, pre-existing bugs found and fixed along the way (not
  regressions from this work, but newly triggered by it): an
  insufficient-balance rejection had never actually carried its own
  error code (silently fell back to the generic one, both over HTTP and
  now in autoplay's own stop-reason bookkeeping) since the exception
  that raises it was never given a proper `.code`; and two *other*,
  older exposure-cap tests turned out to be silently broken by this
  session's own unusually heavy dev-database usage (a `numeric(5,4)`
  column precision floor these tests' own pct-derivation technique
  didn't account for) — found because my own new tests share the same
  shared reserve account and needed the identical fix to pass reliably.
- 150 tests passing across the full affected surface (Keno tickets/
  gateway/admin/round-engine/autoplay, both responsible-gaming files,
  Keno unit tests), mypy clean, the real-browser Keno e2e test
  unaffected. Migration up/down both verified live.

**Next step**: the Mini App frontend for autoplay (setup panel, live
session status display, MainButton-as-stop) — nothing built yet, backend
is ready and fully tested underneath it. After that: the remaining
pure-frontend UX items from the original research (overdue-numbers/
frequency heatmap, variable draw-reveal pacing + sound, favorite/saved
number sets, a colorblind-safe-encoding audit, contextual live paytable,
jackpot ticker, fairness-verification UI polish) — none of these need
another backend decision. The design-heavy Part 7 items (reserve
deposit/withdraw, tier automation, circuit breaker, standard paytable
profile, risk-of-ruin simulator) are still fully open and untouched.

---

## 2026-09-22 — Two frontend passes, then most of the remaining Part 7 production gate closed

Rebrand session (Arada Bingo → Zemen Game, arada.click deployment) ran
first this same day — unrelated to Keno, see DECISIONS.md/git history if
needed. Keno work resumed after, in two phases.

**Phase 1 — frontend redesign, from a real competitor reference.**
Operator provided a screen recording of a live competitor Keno platform
(megachance.club's Fast Keno). Three UX patterns from it were already on
this file's own backlog above but unbuilt — now shipped, frontend only,
Keno still hidden/off in production throughout:
- Hot/cold numbers marked directly on the live betting grid (small dots
  on the cells themselves, not a separate stats tab) — `/api/keno/
  rounds/hot-cold`, already existed, just never called from the main
  game screen.
- A big ball-reveal animation during the draw (one large glowing "hero
  ball" per number, ringed like a real lottery draw, plus a growing
  trail underneath) replacing the old flat ball-strip-only reveal.
- A live Match→Pays panel above the grid, recalculated on every pick
  from the paytable data already sent with the round — no modal needed.

Caught and fixed a real bug the visual verification pass found (not
inspection): a new empty-picks hint stayed on screen through the entire
draw phase, since nothing re-evaluated it once betting closed — only
pick changes did. Fixed by re-checking it on the `keno.betting.closed`/
`keno.draw.started` WS handlers too.

**Phase 1b — the Keno FAB itself was still unconditionally visible.**
Separate small fix, same day: `web/miniapp/index.html`'s Keno nav button
was always shown and clickable regardless of whether Keno had ever been
configured on a given deployment — on arada.click specifically (zero
`keno_configs` rows at the time), tapping it dropped a player onto a
game center with no round data, `refreshState()` silently no-op'ing on
the resulting 503. Now starts hidden; `checkKenoAvailability()` reveals
it only once `/api/keno/state` confirms a real round/config exists.

**Phase 2 — the Keno autoplay/multi-race Mini App frontend.** The
backend (`packages/core/keno_autoplay.py`, three REST endpoints) had
been fully built and tested since the 2026-09-21 entry above with *zero*
UI on top of it — the single biggest already-paid-for gap on this file's
own backlog. Built: a setup sheet (reuses the already-selected picks/
stake exactly like a normal ticket — rounds-total chips, optional stop
-on-win/stop-on-loss amounts), a live status bar while a session runs
(round progress, running net position, a Stop button), and MainButton
-as-stop. No dedicated WS event exists for session updates (deliberate
backend choice), so the frontend re-fetches `GET /api/keno/autoplay` on
the two round-lifecycle events that already fire regardless
(`keno.betting.open`, `keno.round.completed`) — genuinely frontend-only,
no backend touched for this phase. Two real bugs the visual verification
pass caught: Clear/Lucky Pick/Repeat all mutate `selectedPicks` directly
and weren't disabled while a session runs (would have corrupted the
locked session's own picks display on tap); `toggleNumber()`'s board
lock is CSS `pointer-events` only, which doesn't stop a keyboard
-focused cell's Enter/Space from still reaching the handler.

**Phase 3 — most of the actual Part 7 production gate, operator
-directed ("continue to make it ready to production keno").** Distinct
from the frontend work above: this is the gate itself, the thing that
has to close before `keno_enabled` can ever legitimately flip to true.
Two genuine financial/risk-design decisions were required and explicitly
raised to the operator before writing any of this rather than invented
unilaterally — both approved, full reasoning in the migration's own
docstring (`migrations/versions/c4e8f1a9b6d3_keno_production_launch_
seed.py`):
- `max_round_exposure_pct = 10%` for every tier (the spec states the
  mechanism, never an actual number; 10% chosen over the far looser 50%
  sitting in test fixtures).
- Real, RTP-validated paytable multiplier tables for **both** profiles,
  every pick count 1-10, computed with `packages.core.keno`'s own exact
  hypergeometric math (not guessed) to land at the spec's 82% target —
  closing a gap bigger than the original audit found: every previous
  paytable in this codebase, including every `low_variance` table, was
  ad-hoc test-fixture data hand-picked to exercise settlement math, never
  a real production table, and Tiers 2-4 had never existed anywhere at
  all, even in a test. A first design pass wasn't actually monotonic
  (picks=4's freely-solved top exceeded picks=5's spec-fixed 16x anchor,
  picks=9 exceeded picks=10's fixed 5000x) — caught by an explicit
  monotonicity check before it ever reached the migration, fixed by
  anchoring every pick count's top multiplier explicitly rather than
  letting the solver pick it.

**Done, all real, all verified live (up/down/up migrations, real
promtool, real test runs) — see each item's own commit for full detail:**
1. `services/engine/keno_worker.py` (the real engine process, proven
   live since 2026-09-21) finally wired into `docker-compose.prod.yml`.
   Running the container ≠ Keno being live — `keno_enabled` still gates
   every ticket, and the Mini App button stays hidden regardless.
2. The three dead Prometheus gauges (`keno_round_exposure_ratio`,
   `keno_reserve_balance`, `keno_player_liability`) actually `.set()`
   now — exposure per ticket placement, the other two once per round.
3. The two missing alert rules (`KenoRoundExposureHigh` >80%,
   `KenoReserveDepleted` <=0) — validated with a real `promtool` via
   Docker, not just YAML syntax.
4. Production launch seed: real `keno_configs`/`keno_risk_tiers` (all 4
   tiers)/`keno_paytables` (16 rows, both profiles) rows. `keno_enabled`
   stays false — this makes Keno *configurable* for real, not live.
5. Tier promotion (7-day hold, reset on any dip)/demotion (immediate)
   and the daily payout circuit breaker, both wired into
   `KenoRoundEngine._create_round()` so a change only ever affects the
   *next* round, never the in-flight one. New `keno_tier_changes` table
   — `admin_audit_log.admin_id` is `NOT NULL REFERENCES admin_users`,
   structurally admin-actor-only, so a system-driven change had no home
   to record itself; this is the one durable place with full tier
   history regardless of actor. A real design bug caught by this
   phase's own new tests before it ran anywhere real: tier `version` is
   scoped per `tier_number` independently, not a shared ladder-wide
   generation — an early draft would have silently stopped seeing the
   rest of the ladder the moment any single tier was ever edited.

**Still open on the Part 7 gate, unstarted:**
- Part 15 admin tooling proper: a reserve deposit/withdraw *endpoint*
  (the ledger transaction kinds and the withdrawal-floor rule both
  already exist in schema/spec, nothing calls them), a risk-of-ruin
  simulator (spec 7.4: N simulated days given reserve/handle/paytable,
  report ending-reserve distribution, worst drawdown, floor-breach
  probability — genuinely new, not wired to anything existing).
- Part 17: chaos/load testing, entirely unstarted.
- `scripts/verify-round.ts` and 11 of 12 required `docs/keno/*.md` files
  (only this one and `00-discovery.md` exist).
- The one real remaining decision: how much actual capital to seed
  `keno_reserve` with. Not touched — real money, needs the operator's
  own number, shown and confirmed before anything is posted, same
  standing rule as every other real-money action this session.
- The actual go-live moment (flipping `keno_enabled`, un-hiding the Mini
  App button) — a separate, explicit decision regardless of how ready
  everything else is, not something that follows automatically from
  finishing the checklist above.

**Next step**: operator to choose between the remaining admin-tooling
work (reserve deposit/withdraw + risk-of-ruin simulator — the last
pieces with real engineering content), Part 17 chaos/load testing, or
the docs backlog — all three are open and none blocks the others.
