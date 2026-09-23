# Keno — Runbook

Written for the person on call at 3am. Assumes no prior context beyond
this document, `01-architecture.md`, and `02-data-model.md`.

## First, orient yourself

```sql
-- Is there a round in progress, and what state is it in?
SELECT id, status, scheduled_at, betting_opened_at, betting_closed_at,
       draw_started_at, completed_at, worker_id, failure_reason
FROM keno_rounds ORDER BY id DESC LIMIT 5;

-- The full transition history for one round -- always start here for
-- "what actually happened to round N."
SELECT from_status, to_status, worker_id, reason, created_at
FROM keno_round_events WHERE round_id = <id> ORDER BY created_at;
```

```bash
# Is the engine process even running?
docker compose -f deploy/docker-compose.prod.yml ps keno-worker
docker logs jobingo-prod-keno-worker-1 --tail 100
```

A healthy engine's logs are **mostly silent** — `_run_one_round()` only
logs on exceptional events (recovery, settlement draining, failures),
not on routine round creation/betting/draw. Silence is normal; the
database (`keno_rounds`, `keno_round_events`) is the real source of
truth, not the log stream. See `01-architecture.md` for why.

## Scenario: a round looks stuck (status hasn't advanced in a while)

**Two alerts now cover this** — `KenoRoundStuck` (fires once a round has
been non-terminal for over 5 minutes) and `KenoEngineHeartbeatMissing`
(fires once 5 minutes pass with no new round created at all) — see
`deploy/prometheus/alerts.yml`. Both still depend on a real Prometheus
instance actually evaluating them in production, which as of 2026-09-23
does not exist (see "Known gaps" below) — until that's stood up, or as a
second check even after it is, the direct queries below remain the
ground truth:

```sql
SELECT id, status, now() - scheduled_at AS age
FROM keno_rounds WHERE status NOT IN ('completed', 'failed', 'voided');
```

If a round's age is large relative to `round_cycle_seconds` (45s at
launch) and the `keno-worker` container is running, something is
wedged. First check the container is actually alive and holding its
lock:

```bash
docker logs jobingo-prod-keno-worker-1 --tail 200 | grep -i error
docker exec jobingo-redis redis-cli GET keno:round:lock
```

**Usually, no manual action is needed.** `KenoRoundLock` has a 15s TTL,
refreshed every 5s — if the process genuinely died, the lock expires
within ~10s of the last refresh and any running `keno-worker` replica
(or a fresh one you start) picks it back up automatically via
`recover_on_startup()`. If there's more than one `keno-worker`
container running for some reason, stop the extras — exactly one must
own the lock at a time.

If the process is alive but visibly wedged (holding the lock, not
progressing, no useful log output), restart the container:

```bash
docker compose -f deploy/docker-compose.prod.yml restart keno-worker
```

The old process's lock will expire (or was already released by a
clean SIGTERM — `keno_worker.py`'s signal handler calls `engine.stop()`
before exiting, letting the in-flight round finish its current phase
rather than abandoning it mid-transaction). The new process runs
`recover_on_startup()` immediately.

## Scenario: recovery itself — what actually happens on restart

This is normally **automatic**, not something you do by hand. Understand
it so you can tell it's working correctly rather than intervene
unnecessarily:

- **Round in `settling`**: always re-run, regardless of age. The draw
  is already deterministically persisted and immutable by this point —
  refunding instead of re-settling would hand every ticket its stake
  back even if it actually won. Settlement is idempotent (every ledger
  post uses `idempotency_key=f"keno:settle:{round_id}:{ticket_id}"`),
  so re-running it is always safe. This also runs on a periodic sweep
  inside every normal round cycle (`_recover_stuck_settling_rounds()`),
  not just at process boot — a settlement task that died mid-flight
  from an unrelated exception gets picked back up within one round
  cycle, not left stuck until the next full restart.
- **Any other non-terminal round, age ≤ 300s
  (`STUCK_ROUND_THRESHOLD_SECONDS`)**: **resumed**, not refunded — see
  `01-architecture.md` for the full reasoning (the draw is a
  deterministic function of already-committed data, so resuming
  reproduces the exact same result a healthy process would have).
  Proven under a real crash with 40 real staked players in
  `tests/integration/test_keno_chaos_engine_crash.py`.
- **Any other non-terminal round, age > 300s**: marked `failed`, every
  ticket refunded (`_fail_and_refund_round()` → `keno_refund` ledger
  entries, one per ticket, idempotent). This is the **only** automatic
  refund path — it exists for a round so old that resuming it would be
  operationally strange (the engine was down for minutes, not a normal
  crash-and-restart blip), not because resuming would be *incorrect*.

You should essentially never need to manually void or refund a round —
if you find yourself wanting to, first check whether
`recover_on_startup()` already handled it correctly (check
`keno_round_events` for the round). A round already `failed` with
every ticket `refunded` needs no further action from you.

## Scenario: settlement is failing repeatedly

```sql
-- Any round stuck in 'settling' with no ticket progress?
SELECT id, status, ticket_count FROM keno_rounds WHERE status = 'settling';
SELECT status, count(*) FROM keno_tickets WHERE round_id = <id> GROUP BY status;
```

Check `keno_settlement_errors_total` (Prometheus counter — incremented
on any exception during a settlement pass) and the engine's own logs
for the actual exception. `_settle_and_complete()`'s own try/except
logs and swallows any exception by design (a bad settlement must never
take down the whole round loop) — the periodic sweep described above
will retry it. If it's failing on every retry, the cause is almost
certainly a data problem specific to that round (a malformed paytable
row, a ledger constraint violation) rather than a transient blip — read
the actual exception in the logs before assuming a restart will help.

## Scenario: you need to stop everything right now

```
POST /keno/kill-switch  {"enabled": false, "reason": "..."}
```

Superadmin-only (`keno:configure`). Instantly blocks new tickets
(`place_ticket()` checks `config["keno_enabled"]` on every call) and
prevents new rounds from being offered to players. **It does not stop
the currently-running round's engine process** — an in-flight round
still completes and settles normally; the kill switch only stops the
*next* round's tickets from being accepted (in practice, since
`config["keno_enabled"]` is checked per-ticket, it also blocks any
further tickets in the current round the instant it's flipped). This
is deliberate: an in-flight round with real money already staked should
always be allowed to finish and settle correctly, not be aborted
mid-flight by an emergency stop.

## Reading the audit trail

Three separate logs, each for a different question:

- **`keno_round_events`** — "what happened to this specific round, and
  when." Every status transition, with `worker_id` (which process did
  it) and `reason`.
- **`keno_tier_changes`** — "why is the tier what it currently is."
  Every promotion, demotion, circuit-breaker trip, and admin override,
  with `trigger`, `reason`, and the reserve balance at the time —
  regardless of whether an admin or the automation caused it.
- **`admin_audit_log`** — "which admin did what." Every config change,
  paytable edit, tier override, reserve deposit/withdrawal, and
  kill-switch flip, with `admin_id`, `reason`, and `ip_address`. This
  is the platform's existing admin audit table, reused as-is — Keno
  does not have a separate one for admin actions (only for its own
  domain events, `keno_round_events`, and tier history,
  `keno_tier_changes`, neither of which has a mandatory admin actor).

## Known gaps (as of 2026-09-24)

Honest, not exhaustive — see each linked doc for more:

- **Stuck round and engine heartbeat are now covered** —
  `KenoRoundStuck`/`KenoEngineHeartbeatMissing`
  (`deploy/prometheus/alerts.yml`), backed by a real gauge
  (`keno_oldest_nonterminal_round_age_seconds`, set every round cycle
  by `services/engine/keno_round_engine.py::
  _update_oldest_nonterminal_round_gauge()`) and the existing
  `keno_rounds_created_total` counter. Still no configured alert for
  settlement errors, RTP divergence, unsettled tickets in a completed
  round, reserve-near-floor, liabilities-near-available-cash, or
  deposit-failure-spike. Detection for those remains manual, via the
  queries elsewhere in this runbook.
- **A more fundamental gap found while closing the above**: production
  (`deploy/docker-compose.prod.yml`) runs no Prometheus, Grafana, or
  Pushgateway at all — those three exist only in the local-dev compose
  file. Every alert rule this project has (the original 8, these 2, and
  `LedgerReconciliationMismatch`) is real, `promtool`-validated YAML,
  but with no Prometheus instance in production to load and evaluate
  it, none of them can actually fire today. See `docs/ops/
  vps-migration.md` for the full note — worth resolving as its own
  piece of work, not assumed fixed by these rules existing.
- Revenue/cohort analytics metrics are declared but not populated —
  see `07-economics-and-bankroll.md`'s own section on this. Not
  relevant to incident response, but relevant if you're asked "what's
  today's Keno hold %" and the dashboard shows zero.
