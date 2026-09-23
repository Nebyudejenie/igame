# Keno — Test Report

All numbers below are from real runs executed on 2026-09-23 against
this repository at commit `c1990da` (real Postgres + Redis via
`docker-compose.yml`, real Chromium via Playwright for the one e2e
test) — not estimated, not carried over from an earlier session without
re-verification.

## Summary

| Category | Files | Tests | Result |
|---|---|---|---|
| Unit | `test_keno.py`, `test_keno_exposure.py`, `test_keno_risk_simulator.py` | — | all passing (part of the 153 below) |
| Integration | `test_keno_admin.py`, `test_keno_autoplay.py`, `test_keno_gateway_rest.py`, `test_keno_gateway_ws.py`, `test_keno_round_engine.py`, `test_keno_tickets.py`, `test_keno_tier_automation.py` | — | all passing (part of the 153 below) |
| **Default suite subtotal** | 10 files above | **153** | **153 passed, 0 failed** |
| E2E (real Chromium) | `test_keno_miniapp_e2e.py` | 1 | 1 passed |
| Statistical (large-N) | `test_keno_statistical.py` | 6 | 6 passed |
| Load | `test_keno_chaos_engine_crash.py`, `test_keno_load_concurrent_tickets.py` | 2 | 2 passed (see `11-load-test-report.md`) |
| Chaos infra | `test_keno_chaos_redis_restart.py` | 1 | 1 passed (see `11-load-test-report.md`) |
| **Grand total** | 14 files | **163** | **163 passed, 0 failed** |

`mypy --strict` over the project's real configured scope
(`packages`, `services`, `migrations` — test files are intentionally
outside this scope, see `pyproject.toml`'s own `[tool.mypy]` section):
**0 issues across 160 source files.**

## Coverage against the spec's own required categories

Spec Part 17 (`keno.md`) names specific things unit and integration
tests must cover. Checked directly against real test function names,
not assumed:

**Unit** (`tests/unit/test_keno.py`, `test_keno_exposure.py`):
hypergeometric probabilities against hand-computed values
(`test_pick_two_probabilities_match_hand_computed_fractions`) ✓ · RTP
calculation ✓ · paytable guardrails, both directions
(`test_validate_paytable_rtp_rejects_{above_ceiling,below_floor}`) ✓ ·
match counting ✓ · payout + cap logic, including the round-down-never
-up rule ✓ · draw determinism
(`test_derive_keno_draw_is_deterministic`,
`..._changes_completely_with_one_bit_flipped`) ✓ · rejection sampling
free of modulo bias
(`test_below_shows_no_obvious_modulo_bias_at_small_scale`) ✓ · ticket
validation (all five rejection paths individually tested) ✓ · tier
evaluation (`tests/integration/test_keno_tier_automation.py`, 8 tests
including the real version-scoping regression — see below) ✓ ·
exposure calculation (`tests/unit/test_keno_exposure.py`) ✓ · jackpot
pool accounting (`tests/integration/test_keno_round_engine.py`,
"jackpot cannot exceed pool" case) ✓.

**Integration**: full round lifecycle (bet → deduct → draw → settle →
credit) ✓ · idempotent re-bet and re-settlement (both use the same
idempotency-key mechanism, both tested) ✓ · betting-after-close
rejected ✓ · insufficient balance rejected ✓ · exposure cap rejection
✓ · per-user share cap ✓ (including the "first ticket in a round must
never be structurally impossible" regression — see `07-economics-and-
bankroll.md`) · config change mid-round doesn't affect the running
round ✓ (pinned `config_id`/`tier_id`, directly tested) · void + refund
✓ (`_fail_and_refund_round`, both automatic-recovery and direct paths)
· bonus playthrough enforcement — covered by the platform's existing
shared bonus-ledger tests, not Keno-specific (Keno posts through the
same ledger, doesn't reimplement bonus logic) · jackpot cannot exceed
pool ✓.

## Real bugs this test suite has actually caught

Not hypothetical — each of these was a genuine defect the tests found
*before* it reached production data, fixed as a direct result:

1. **Tier version-scoping bug** (`test_keno_tier_automation.py`) —
   `keno_risk_tiers.version` is scoped per `tier_number` independently,
   not shared across the whole ladder. Code that filtered "the rest of
   the ladder" by `WHERE version = current_tier.version` would have
   silently stopped seeing every other tier the moment any single tier
   was edited. See `02-data-model.md`.
2. **Withdrawal-audit-rolled-back-with-its-own-rejection bug**
   (`test_keno_admin.py`) — a floor-breach rejection's own audit row
   was inside the same transaction as the exception signaling it,
   so raising rolled the audit insert back too — "attempts are
   audited" was silently false. See `07-economics-and-bankroll.md`.
3. **TOCTOU race** introduced by the fix for #2 — closed with a
   fresh row-locked re-check immediately before the actual withdrawal.
4. **Paytable monotonicity bug** — an unconstrained RTP solve let a
   smaller pick count's freely-solved top multiplier exceed a larger
   pick count's spec-fixed anchor. Caught before the production seed
   migration was written, not after. See `05-paytable-and-rtp.md`.
5. **Per-user round-share cap measured against the wrong baseline** —
   an earlier version compared a user's exposure against other
   players' stakes, making it structurally impossible to ever place a
   round's first bet. See `07-economics-and-bankroll.md`.
6. **A test-methodology bug, not a product bug, but real**: an
   early version of the Redis chaos test used a fast `docker compose
   restart` (sub-second), which let `KenoRoundLock`'s own retry margin
   silently ride it out without ever exercising recovery at all — the
   test would have reported "passing" while checking nothing. Caught
   by comparing real log output against what genuine lock loss looks
   like, not by the test's own assertions (which were satisfied either
   way). See `11-load-test-report.md`.
7. **An orphaned-background-task bug found by running two test files
   together**, not either alone — see `11-load-test-report.md` for the
   full story.

## Known gaps

- No `pytest-cov` line-coverage figure is generated by this project's
  test configuration — this report is pass/fail and category-coverage
  based, not a percentage-coverage claim.
- `bonus playthrough enforcement` is exercised through the platform's
  existing shared ledger/bonus tests, not a Keno-specific test file —
  correct, since Keno doesn't reimplement bonus logic, but noted here
  since it's not literally inside a file named `test_keno_*`.
