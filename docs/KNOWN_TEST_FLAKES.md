# Known test flakes and shared-database hazards

Check here before investigating an intermittent integration-test failure.
Every entry says what was actually verified and what wasn't.

The integration tests share one long-lived dev Postgres, which is never
truncated between runs. Almost every flake recorded here comes from rows
one test leaves behind that another test's query then picks up.

## Fixed

### `test_keno_round_engine.py::test_full_round_lifecycle_bet_to_settle` fails after `test_keno_tickets.py`

- **Symptom**: `AssertionError: condition not met within 20.0s` waiting for
  the round to complete. It passes when run alone.
- **Reproduced (2026-09-25)**: `pytest tests/integration/test_keno_tickets.py
  tests/integration/test_keno_round_engine.py` failed 2 of 2 times, and
  failed the same way with that session's code changes stashed.
- **Root cause**: the fast-config helpers seeded their config, tier and
  paytable with `effective_from = now() - interval '1 second'`. The engine
  uses the row with the newest `effective_from <= now()`. A slow config
  (25s betting window) inserted by an earlier test less than a second
  before therefore stayed the active one, and the round couldn't finish
  inside the test's timeout. Confirmed by reading the active config at the
  point of failure: `betting_seconds = 25`.
- **Fix**: the helpers in `test_keno_round_engine.py`,
  `test_keno_miniapp_e2e.py` and `test_keno_load_concurrent_tickets.py`
  now seed `effective_from` one microsecond after the newest effective row
  (never later than `now()`), so a freshly seeded fast config always wins.
  After the fix the same two-file command passed 2 of 2 times.

### `test_keno_miniapp_e2e.py::test_keno_button_stays_cleanly_hidden_for_a_non_allowlisted_user` fails after `test_keno_admin.py`

- **Symptom**: `assert config_row["keno_enabled"] is True` fails.
- **Root cause**: `test_keno_admin.py` deliberately inserts a config
  effective *tomorrow*, with Keno disabled, to test "active config vs a
  newer future row". This test read "the config" with `ORDER BY
  effective_from DESC` and no `effective_from <= now()` filter, so it read
  that future row instead of the active one.
- **Fix**: the query now filters `effective_from <= now()`, the same rule
  the engine applies.

## Open

### `test_emergency_room_stop.py` (several tests) under full-suite load

- Seen failing with `not_joinable` during long full-suite runs. The same
  tests passed on isolated reruns. Suspected environmental (full-suite
  load on the shared DB and Redis), but **the root cause was not
  investigated**.

## Hazards for anyone writing a new test

- **Future-dated config rows exist** in the dev DB. Always read Keno
  configs, tiers and paytables with `effective_from <= now()`.
- **Active autoplay sessions are platform-wide.**
  `place_for_active_sessions()` walks every active session, so any
  session a test leaves active gets a real ticket in the next test's
  round. Every autoplay test module has an autouse fixture that stops all
  active sessions before and after each test. Copy it.
- **`keno_rounds` in `betting_open` leak between tests.** Seed helpers
  close every non-terminal round first. A test that creates rounds itself
  should too.
- **The Keno engine lock (`keno:round:lock` in Redis) is global.** A test
  that starts `KenoRoundEngine.run_forever()` must delete it first, or it
  waits for a lock a dead engine from an earlier test still holds.
