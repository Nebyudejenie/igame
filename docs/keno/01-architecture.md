# Keno — Architecture

## Position in the platform

Keno is a second, independent game bolted onto the existing Zemen Game
(formerly Arada Bingo) platform, purely additively:

- No existing Bingo table, route, or process is modified to make Keno
  work. Keno has its own `keno_*` table set with no foreign keys into
  `rooms`/`rounds`/`round_entries` (see `00-discovery.md` §C).
- Keno reuses three pieces of shared platform infrastructure rather
  than rebuilding them: the ledger (`packages/core/ledger.py`), the
  gateway's websocket fanout hub, and the admin service's RBAC/audit
  log. Everything else — round lifecycle, draw math, risk tiering — is
  Keno-specific.
- Bingo has one round *per room*, many rooms running concurrently.
  Keno has exactly **one continuous global round stream** — there is
  no "room" concept at all. This single difference shapes almost every
  other architectural decision below.

## Services

| Service | Role in Keno |
|---|---|
| `gateway` | Public HTTP API (`/api/keno/*`) and the single `/ws` websocket every authenticated client holds open. Every connection is unconditionally subscribed to the Keno broadcast channel the instant it authenticates (`services/gateway/connection.py`) — unlike Bingo rooms, which are joined explicitly, the Keno screen must reflect the live round the moment a socket is up. |
| `keno-worker` (`services/engine/keno_worker.py`) | The process that actually runs the round cycle. Wraps a single `KenoRoundEngine` in a retry-with-backoff outer loop, starts a Prometheus metrics server on port 8008, and handles `SIGTERM`/`SIGINT` for a clean stop. This is the process a spec-compliance audit found *entirely missing* until this project's Part 17 phase — `KenoRoundEngine.run_forever()` previously only ever ran from test code. |
| `admin` | Admin-only config, tier, paytable, reserve, and reporting endpoints (`services/admin/keno_queries.py`, `services/admin/app.py`). Every mutating endpoint goes through the platform's existing RBAC (`keno:configure`, `keno:manage` permissions) and writes to `admin_audit_log`. |
| `postgres` | Single source of truth for everything: round state machine, tickets, ledger, config, tier ladder, paytables. Keno's own crash-recovery model (below) depends on this being true — nothing Keno needs to recover from a crash lives only in memory or only in Redis. |
| `redis` | Two roles only: (1) the single global round-advancement lock (`services/engine/keno_lock.py`), (2) pub/sub transport for realtime events (`keno:live` channel plus the existing per-user `user:{id}` channel). Redis holds no authoritative state — `packages/core/redis_conn.py`'s own documented invariant ("if Redis is wiped, the platform must recover fully from Postgres") applies to Keno exactly as it does to Bingo. |

## The round engine

`services/engine/keno_round_engine.py` — `KenoRoundEngine` — runs one
round at a time through a fixed state machine:

```
scheduled → betting_open → betting_closed → drawing → draw_complete → settling → completed
                                                                              ↘ failed / voided
```

`run_forever()`'s own loop:

```python
while not self._stop_requested and self._lock.is_held():
    await self._run_one_round()
```

and `_run_one_round()` is: recover stuck settlements → create round →
open betting → run betting phase → close betting → run draw → **spawn
settlement as a background task and return immediately**. That last
step is deliberate — settlement (computing every ticket's result,
posting ledger entries, updating the round's own totals) overlaps with
the *next* round's own betting phase rather than blocking it, so the
round cycle has zero dead time between rounds even though settlement
of many tickets takes real time. `_drain_settlement_tasks()` is
awaited before `run_forever()` itself ever returns, so a clean shutdown
still waits for every in-flight settlement to finish — nothing is
dropped, only overlapped.

### Single-owner enforcement

Exactly one worker may ever advance the round (spec 5.2). Enforced by
`KenoRoundLock` (`services/engine/keno_lock.py`): a single Redis key
(`keno:round:lock`), TTL 15s, refreshed every 5s. A second `keno-worker`
replica calling `run_forever()` gets `acquired=False` back immediately
and sits in a retry loop, ready to take over the instant the current
owner releases the lock — a clean stop, or the TTL simply expiring
after a crash. There is no leader-election protocol beyond this lock;
it doesn't need one, because there is only ever one round stream to own.

### Crash recovery — deliberately different from Bingo's

Bingo's `RoundEngine` always voids and refunds a crashed round. Keno's
`recover_on_startup()` instead **resumes** a crashed round, as long as
it's younger than `STUCK_ROUND_THRESHOLD_SECONDS` (300s). This is not
an oversight or an inconsistency — it follows from a real difference in
what a "round" *is* in each game:

- In Bingo, a round's outcome depends on runtime state (who joined,
  what numbers were called, in what order) that a crash can genuinely
  leave ambiguous.
- In Keno, the draw is `derive_keno_draw(server_seed, public_seed, ...)`
  — a **pure, deterministic function** of data already committed to
  Postgres *before betting even closes* (the server seed hash is
  published at round open; the public seed is fixed the moment betting
  closes, from the round id, close timestamp, and a hash of every
  accepted ticket id). A crash after that point changes nothing about
  what the draw *will be* — re-deriving it on a fresh worker produces
  the byte-identical result. Refunding those tickets would incorrectly
  hand stakes back to players who may have actually won.

Recovery is staged by exactly where the crash landed
(`keno_rounds.status` tells the recovering worker which phase to
resume from), down to resuming a partially-broadcast draw reveal from
`reveal_index` rather than re-revealing from zero. A round older than
the stuck-round threshold is presumed unrecoverable and is marked
`failed` with a refund path instead (see `08-runbook.md`).

Proven under a real crash (not simulated) in
`tests/integration/test_keno_chaos_engine_crash.py`: 40 real staked
players, the engine task killed mid-betting-phase with no graceful
shutdown, a fresh engine resumes the round to a correct, fully-settled
completion with the ledger reconciling exactly.

## Betting, risk, and the ledger

`packages/core/keno_tickets.py::place_ticket()` is the one write path
for every ticket, manual or autoplay-driven. Per spec Part 6.1, in one
transaction: validate the ticket → re-read the round with a row lock,
assert `betting_open` → validate risk tier / per-user limits / round
exposure capacity → lock the user's wallet row → debit stake via the
existing ledger → insert the ticket. A `pg_advisory_xact_lock(user_id)`
is taken for the whole transaction — the same lock Bingo's own `join()`
takes, keyed only on `user_id`, so a Bingo join and a Keno ticket
racing for the same user's responsible-gaming daily-loss cap correctly
serialize against each other even though they're two different games.

Round-level exposure (spec 7.3) is tracked with a running
Central-Limit-Theorem approximation, not a hard worst-case sum — see
`packages/core/keno_exposure.py`'s own module docstring and
`07-economics-and-bankroll.md` for the full reasoning. Proven to
serialize correctly under real concurrency (300 concurrent
`place_ticket()` calls against one round, tight cap, never oversold) in
`tests/integration/test_keno_load_concurrent_tickets.py`.

Every Keno money movement — stake, payout, jackpot contribution/payout,
reserve deposit/withdrawal — posts through the platform's *existing*
`packages/core/ledger.py`, not a second wallet system. `keno_reserve`
and `keno_jackpot_pool` are ordinary ledger accounts (new `accounts.kind`
values added by a backward-compatible CHECK-constraint widening — every
pre-existing account kind still works unchanged).

## Risk tiers and configuration

Both `keno_configs` and `keno_risk_tiers` (plus `keno_paytables`) are
**insert-only versioned**: a "change" is a new row with a later
`effective_from`, never an `UPDATE` to an existing row. The engine
always reads the row with the latest `effective_from <= now()` and
pins its id onto the round at creation (`keno_rounds.config_id`/
`tier_id`) — an admin editing a live tier or paytable can never alter
an in-flight round's rules, and "effective from the next round only"
falls out of the shape for free rather than needing a flag-flip race
to get right.

Tier promotion/demotion is automated (`packages/core/
keno_tier_automation.py`), evaluated once per round creation:
promotion needs the reserve above a tier's threshold for 7 consecutive
days (tracked via `keno_tier_state.candidate_tier_id`/`candidate_since`,
reset on any dip); demotion, and the daily payout circuit breaker, are
immediate. Every change — automated or an admin override — is recorded
in `keno_tier_changes`, a dedicated audit table (`admin_audit_log.
admin_id` is `NOT NULL`, structurally admin-actor-only, so a
system-driven change needed its own home).

## Realtime transport

See `04-realtime-events.md` for the full event catalog. In short: the
engine publishes public round-lifecycle events to a single Redis
pub/sub channel (`keno:live`), which the gateway fans out to every
subscribed websocket connection — no per-room targeting, since there's
only one round stream. Per-user outcomes (a ticket settling) publish to
the same `user:{id}` private channel Bingo already uses for balance
updates, so a client's existing per-user subscription picks them up
with no new subscription mechanism needed.

## What's deliberately *not* built

- No per-Keno-round FK into the shared `rounds`/`ledger_transactions.
  round_id` columns — Keno rounds are referenced from ledger entries
  by their own `keno_rounds.id`/`keno_tickets.id`, kept structurally
  separate from Bingo's round accounting (see `00-discovery.md` §C for
  the full reasoning).
- No second wallet, no second admin RBAC system, no second websocket
  transport — all three are the platform's existing ones, reused as-is.
