# Keno — Load and Chaos Test Report

Real runs, executed on 2026-09-23 against this repository at commit
`897dd7f`, against real Postgres, real Redis, and (for the Redis
outage test) a real `docker compose stop`/`start` of the actual Redis
container — nothing here is simulated infrastructure.

## Current results

| Test | Marker | Result | Notes |
|---|---|---|---|
| `test_crashed_engine_with_40_staked_players_recovers_to_a_correct_settlement` | `load` | **PASS** (4/4 recent runs) | 40 real staked players, engine `task.cancel()`ed mid-betting with no graceful shutdown, fresh engine resumes to a correct completed settlement. |
| `test_300_players_rush_one_round_with_a_tight_exposure_cap` | `load` | **PASS** (4/4 recent runs) | 300 concurrent `place_ticket()` calls, tight exposure cap. Latest run: `elapsed=3.17s accepted=4 rejected=296 reasons={'RoundCapacityReached': 296}` — never oversold, ledger reconciled exactly. |
| `test_redis_restart_mid_round_recovers_cleanly` | `chaos_infra` | **PASS** (2/2 recent runs) | Real Redis stopped for 16.2s, engine correctly lost the lock and a fresh engine resumed. `1 passed in 44.55s` (real wall-clock cost of a genuine Docker stop/start, not padding). |

Full `-m load` batch (all load-marked tests platform-wide, not just
Keno's): **2 pre-existing, unrelated Bingo latency flakes** remain
(`test_gateway_fanout.py`, `test_load_multiroom.py` — both a p99
call-to-render budget of 300ms occasionally measuring ~310–352ms under
this environment's real timing variance). Confirmed pre-existing and
unrelated to Keno by reproducing them in isolation with none of this
project's Keno code in the execution path at all — the identical
failure occurs with or without any Keno change present.

## Why the Redis test needed a real methodology fix, not just a pass

An early version of `test_redis_restart_mid_round_recovers_cleanly`
used a plain `docker compose restart redis` — which completes in
well under a second on this stack. `KenoRoundLock`'s own refresh loop
(every 5s, ~10s retry-with-backoff margin before giving up) silently
rode out a restart that fast without the lock ever actually being
lost — the test's own assertions were satisfied either way, so it
reported "passing" while not exercising the recovery path it claimed
to test at all. This was caught by directly inspecting real log output
(exactly one "retrying" warning, then nothing — the lock never
genuinely flipped to unheld) rather than trusting a green checkmark.
Fixed by switching to a real `docker compose stop` → sleep 15–16s →
`start` outage, comfortably past the lock's own give-up margin — the
log output above (`keno_round_lock_refresh_failed_retrying` at 5s,
`keno_round_lock_refresh_failed` once the margin is exceeded, then a
real `ConnectionRefusedError` while Redis is actually down) is what a
*genuine* lock loss and recovery looks like, and is now what this test
actually proves.

## A real bug found only by running two test files together

Running `test_keno_chaos_engine_crash.py` and
`test_keno_load_concurrent_tickets.py` individually, each passed
reliably every time. Running them **back to back in the same `-m load`
batch** was flaky — roughly 2 failures in 3 attempts during
investigation, with the load test's own reserve-balance assertion
failing (an actual balance lower than the pre-test baseline, meaning
money left the shared `keno_reserve` account during the load test's
own execution window).

Root cause, found by reading the actual failure rather than assuming
it was environmental flakiness: the chaos test placed 40 concurrent
tickets against a `betting_seconds=3` window. Under real database/pool
contention, that window was occasionally too tight — some placements
were still in flight when the engine's own timer closed betting,
raising `RoundNotAcceptingBets`. Because the test had **no
`try`/`finally`** around the doomed engine's task at that point in the
code, the test function raised and returned *before* ever reaching its
own deliberate `task.cancel()` a few lines later — orphaning that
engine. Left running unsupervised, it kept creating and settling its
own real rounds against the shared `keno_reserve` for the rest of the
pytest process, corrupting whatever ran next.

Fixed with a proper `try`/`finally` safety net guaranteeing the doomed
engine is always cancelled and drained regardless of what fails above
it, plus a more generous `betting_seconds=10` to make the underlying
race far less likely to trigger at all. Verified clean over 4
consecutive paired runs immediately after the fix, and clean again in
the fresh runs quoted in the results table above.

## What these tests prove that unit/integration tests alone can't

- **Crash recovery under real concurrent load**, not a mocked engine —
  the exact scenario a production incident would look like.
- **Real infrastructure failure** (an actual Redis process stopping),
  not a simulated exception — proves the lock's TTL/refresh timing
  assumptions hold against real Docker networking behavior, not just
  against code that assumes they do.
- **Serialization correctness under genuine contention** — 300
  simultaneous requests racing the same row lock is a fundamentally
  different test than 300 sequential calls, and only the concurrent
  version could have caught a TOCTOU-style race in the exposure check.

## Known limitations

- These tests run against this project's shared local dev database,
  not an isolated ephemeral one — `keno_reserve` is a genuine global
  singleton with no per-test isolation. Both load tests account for
  this explicitly (documented in their own file headers) rather than
  assuming a clean starting balance.
- No sustained-throughput load test exists yet (e.g., "N tickets/second
  for M minutes") — the current load test is a single concurrent burst,
  which proves correctness under contention but not sustained capacity
  under real production traffic volume. Not built in this pass; noted
  as a gap rather than implied to be covered.
