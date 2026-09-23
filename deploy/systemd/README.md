# Backup / reconciliation scheduling — systemd units

Real, ready-to-install systemd timer units closing
`docs/LAUNCH_BLOCKERS.md`'s LB-B2 (no backup schedule wired anywhere) and,
as of the `jobingo-reconcile` pair, the ledger-reconciliation scheduling
gap surfaced by a real incident during Keno development (2026-09-17, see
DECISIONS.md and `packages/core/reconcile_job.py`'s own long-standing
"never actually scheduled anywhere" caveat). These call the existing,
tested scripts/jobs (`deploy/backup.sh`, `deploy/basebackup.sh`,
`deploy/prune_wal_archive.sh`, the `reconcile-job` compose service) — no
new mechanism, just the missing scheduler around what already works.

## What's here

| Unit pair | Runs | Cadence |
|---|---|---|
| `jobingo-backup.{service,timer}` | `deploy/backup.sh` (logical `pg_dump`) | Daily, 02:00 |
| `jobingo-basebackup.{service,timer}` | `deploy/basebackup.sh` (physical base backup for PITR) | Weekly, Sunday 03:00 |
| `jobingo-reconcile.{service,timer}` | `docker compose ... run --rm reconcile-job` (`packages/core/reconcile_job.py` — ledger cache-vs-entries check) | Daily, 03:30 |
| `jobingo-prune-wal.{service,timer}` | `deploy/prune_wal_archive.sh 30` (30-day retention) | Daily, 04:00 |

## Before installing: one placeholder to fix

Every `.service` file has `WorkingDirectory=/home/cosmic/game` and a
matching path in `Environment=COMPOSE_FILE=...` (or, for
`jobingo-reconcile.service`, an inlined `-f /home/cosmic/game/deploy/
docker-compose.prod.yml` in its `ExecStart=`) — this is this repo's own
production deploy path, `cosmic@/home/cosmic/game`. **Confirm this is
still correct on the real host before installing** — if the checkout ever
moves, update all four `.service` files to match, the same as any other
deploy-path-dependent script in this repo.

The host address itself: this file, and several older docs (`docs/
FINAL_HUMAN_ACTIONS.md`, `docs/LAUNCH_BLOCKERS.md`, `docs/
PRODUCTION_READINESS.md`, `docs/PRODUCTION_HOST_VERIFICATION.md`, `docs/
keno/00-discovery.md`), previously cited `192.168.1.173` -- but every one
of those citations says in its own text that `.173` was never actually
confirmed reachable from any session. `192.168.1.115` is the address a
2026-09-23 session used successfully, repeatedly, all day (SSH, real
deploys, real migrations) -- see `docs/keno/PROGRESS.md`'s entries from
that date and `docs/ops/vps-migration.md`. Treat `.173` across every doc
above as stale until each one is individually corrected; this file now
is.

## Install (on the production host, as a user with sudo)

```bash
sudo cp deploy/systemd/jobingo-*.service deploy/systemd/jobingo-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobingo-backup.timer
sudo systemctl enable --now jobingo-basebackup.timer
sudo systemctl enable --now jobingo-reconcile.timer
sudo systemctl enable --now jobingo-prune-wal.timer
```

## Verify

```bash
systemctl list-timers | grep jobingo
# Expect four rows with a real NEXT time, not "n/a".

# Run one manually right now rather than waiting for the schedule, to
# confirm it actually works end to end before trusting the timer alone:
sudo systemctl start jobingo-backup.service
systemctl status jobingo-backup.service   # should show "Succeeded"
ls -la backups/   # a new dump file should exist with a fresh timestamp

# Same idea for reconciliation -- a real mismatch surfaces as a failed
# unit (exit code 1, see packages/core/reconcile_job.py's own main()),
# never a silent pass:
sudo systemctl start jobingo-reconcile.service
systemctl status jobingo-reconcile.service   # should show "Succeeded"
journalctl -u jobingo-reconcile.service -n 20   # look for ledger_reconciliation_ok
```

## Alerting on a mismatch

`jobingo-reconcile.service`'s failed-unit state
(`systemctl is-failed jobingo-reconcile`) is one real signal to page on.
The other is Prometheus-native: `reconcile_job.py` pushes the mismatch
count to Pushgateway when `PUSHGATEWAY_URL` is set (see
`deploy/docker-compose.yml`'s `pushgateway` service and README.md's own
setup steps), which is exactly what `deploy/prometheus/alerts.yml`'s
`ledger_reconciliation_mismatch_count` alert already watches — wire
`PUSHGATEWAY_URL` into `deploy/.env` and this timer's every run reports
into the same alerting path every other spec-section-10.4 alert already
uses, rather than needing a second, systemd-specific alerting mechanism.

This is exactly `docs/LAUNCH_BLOCKERS.md` LB-B2's own verification
command (`systemctl list-timers | grep jobingo-backup`) — running it
after this install is what actually closes that item, not just having
these files exist in the repo.

## After this is running

`docs/DISASTER_RECOVERY.md`'s "Is this actually running?" checklist and
`docs/DISASTER_RECOVERY_DRILL.md`'s scenario 1 (DB corruption) both
currently say RPO/RTO are unconfirmed pending a real schedule — once
these timers have run for a few days, re-check that checklist for real,
and perform a real restore drill (LB-B3) against one of the backups these
now produce.
