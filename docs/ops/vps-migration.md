# Migrating off the Proxmox box — what's known, what isn't

Written 2026-09-23, before the outage investigation has run, so the
sizing numbers below are deliberately absent — see the note at the top
of "What this doc does NOT cover." Everything else here is derived
directly from this repository (`deploy/docker-compose.prod.yml`,
`deploy/cloudflared/config.yml.example`, `deploy/.env.prod.example`,
`.github/workflows/cd.yml`, `deploy/systemd/`, `docs/DISASTER_RECOVERY.md`,
`docs/PRODUCTION_HOST_VERIFICATION.md`), not assumed.

## Current hosting, as best this repo's own words confirm it

`deploy/docker-compose.prod.yml`'s own header comment, and `README.md`'s
CI/CD section, both say this directly: **a Proxmox VM (or LXC — the CD
setup notes say "VM/LXC" without committing to which) with no public IP
of its own.** That's why `cloudflared` runs as one of this stack's own
containers rather than binding a port directly — there's no public
address to bind. This is corroborated, not just repeated: the box is
reachable only at a private address (`192.168.1.x`, RFC 1918 — no cloud
or datacenter provider ever assigns one of those to an internet-facing
server), and nothing in this session's own network routing shows a VPN
that would make a remote machine appear on a private-looking address.

**A real, previously-unnoticed discrepancy found while writing this
doc**: five older docs (`docs/FINAL_HUMAN_ACTIONS.md`, `docs/
LAUNCH_BLOCKERS.md`, `docs/PRODUCTION_READINESS.md`, `docs/
PRODUCTION_HOST_VERIFICATION.md`, `docs/keno/00-discovery.md`) cite
`192.168.1.173` as the production host — but every one of those
citations says in its own text that `.173` was never actually confirmed
reachable from any session. `192.168.1.115` is the address a 2026-09-23
session used successfully, repeatedly, for real SSH access, real
deploys, and real migrations, all day (see `docs/keno/PROGRESS.md`'s
entries from that date). `deploy/systemd/README.md` has been corrected
to `.115`; the other five have not — treat every one of them as
carrying a stale address until individually fixed. This is exactly the
kind of silent doc/infrastructure drift the current outage investigation
exists to catch more of.

Definitive confirmation (`systemd-detect-virt`, `uname -a` — bare metal
vs. VM, and which hypervisor) is part of tomorrow's outage-investigation
pass, not asserted here.

## What this doc does NOT cover

**Sizing (CPU/RAM/disk) for the new box.** That needs measuring the
current box's real usage (`docker stats`, `free -h`, `df -h`) — explicitly
deferred to tomorrow's on-box investigation, per instruction. Everything
below is sequencing, inventory, and verification — the parts that don't
need the box to write.

## The full cutover sequence

Assumes the new VPS is already provisioned (OS installed, Docker +
Docker Compose installed) and a decision on sizing has already been
made from tomorrow's real numbers.

1. **On the new box**: clone this repo to the same path convention
   (`/home/<user>/game`, matching what every `deploy/systemd/*.service`
   file's `WorkingDirectory=` currently hardcodes — either keep the path
   identical to avoid editing those units, or update all four service
   files together if the path changes).
2. **Create `deploy/.env`** on the new box from `deploy/.env.prod.example`
   — every variable listed in the inventory below, with real production
   values copied from the old box's own `deploy/.env` (this file is
   gitignored; it has to be copied directly, not regenerated — see the
   inventory's own note on `POSTGRES_PASSWORD` and `PHONE_ENCRYPTION_KEY`
   specifically, since a *new* value for either of those would make
   existing data unreadable, not just require a new login).
3. **Restore the database** — do not attempt a live volume copy between
   hosts. Use the tooling that already exists and is tested
   (`docs/DISASTER_RECOVERY.md`):
   - Take a fresh `deploy/backup.sh` dump on the *old* box (or use the
     most recent scheduled one, once `deploy/systemd/jobingo-backup.timer`
     is confirmed actually running — see the inventory below).
   - Copy that dump file to the new box (`scp`, or via whatever transfer
     path is available between them).
   - Bring up just `postgres` + `redis` on the new box
     (`docker compose -f docker-compose.prod.yml up -d postgres redis`),
     then `deploy/restore.sh <dump-file> jobingo` (the *real* database
     name this time, not the default `jobingo_restore_drill` — this is
     the one place the drill-safe default must be deliberately
     overridden).
   - Run `docker compose -f docker-compose.prod.yml run --rm migrate` to
     confirm the schema is current (should be a no-op if the dump was
     taken from the same `HEAD` this checkout is on).
4. **Verify data landed correctly** before touching any traffic —
   spot-check row counts on a few real tables (`users`, `ledger_
   transactions`, `keno_rounds`) against what the old box shows for the
   same tables, and confirm `SELECT * FROM alembic_version` matches.
5. **Bring up the full stack** on the new box (`docker compose -f
   docker-compose.prod.yml up -d`) — every app service, `keno-worker`
   included. Confirm all containers report healthy/running, zero
   restarts, the same verification pattern this session already used
   for every prior deploy (`docker inspect ... --format 'restarts={{.
   RestartCount}} status={{.State.Status}}'`).
6. **Move the Cloudflare Tunnel** — see its own section below. This is
   the actual cutover moment: traffic starts reaching the new box the
   instant this step completes.
7. **Confirm public traffic is really landing on the new box**, not a
   cached/stale route — `curl -sI https://arada.click/healthz` from
   outside, repeated a few times over a couple of minutes (DNS/tunnel
   routing can take a short moment to fully propagate even though
   Cloudflare Tunnel is generally fast), watching the new box's own
   `gateway` container logs for the request actually arriving.
8. **Install the systemd timers** on the new box (`deploy/systemd/`'s own
   README) — backups, basebackups, WAL pruning, ledger reconciliation.
   None of this exists on a fresh box until explicitly installed; it
   will not silently carry over.
9. **Re-register the GitHub Actions self-hosted runner** on the new box
   (`.github/workflows/cd.yml`'s own setup notes — Settings → Actions →
   Runners → New self-hosted runner). Until this is done, the automated
   CD pipeline has nowhere to deploy to; every future `git push` to
   `main` would build and push an image with no self-hosted runner to
   receive it. Manual `ssh` + `docker build`/`docker compose` deploys
   (this session's own actual practice today) still work regardless.
10. **Old box**: once the new box is confirmed fully healthy and serving
    real traffic for a reasonable soak period (this doc doesn't prescribe
    an exact duration — that's a judgment call once real traffic is
    flowing), stop the old box's containers (don't delete anything yet)
    and keep it as a cold rollback target for at least as long as the
    rollback window below assumes.

## How the Cloudflare Tunnel moves, concretely

The tunnel is bound to Cloudflare's own DNS records, not to a specific
machine — moving it is a credentials-and-config move, not a DNS change,
which is what makes this fast:

1. **Copy, don't recreate, the tunnel credentials.** `deploy/cloudflared/
   tunnel-credentials.json` and `deploy/cloudflared/config.yml` are both
   gitignored real secrets — they have to be copied directly from the
   old box to the new one (same `scp`-style transfer as the database
   dump). Do **not** run `cloudflared tunnel create` again on the new
   box — that would create a *second*, different tunnel with a different
   ID, requiring all-new DNS records rather than reusing the existing
   ones.
2. Confirm `config.yml`'s `ingress:` rules still point at the right
   Compose service names (`gateway:8000`, `admin:8001`, etc.) — these
   don't change unless the compose file's own service names change,
   which this migration doesn't touch.
3. Bring up the `cloudflared` container on the **new** box
   (`docker compose -f docker-compose.prod.yml up -d cloudflared`).
   Cloudflare's edge will now see the same tunnel ID advertising a new
   set of active connections — this is what actually redirects traffic,
   with no DNS record needing to change at all, since every hostname
   already points at the tunnel by its ID, not at any IP.
4. **Stop `cloudflared` on the old box** at the same moment (or
   immediately after confirming the new one is connected) — a tunnel
   with connections advertised from two boxes at once would let
   Cloudflare route a given request to either one unpredictably, exactly
   the kind of split-brain state to avoid during a cutover.
5. Verify: `cloudflared tunnel info <tunnel-name>` (run from either box,
   or via the Cloudflare dashboard) should show active connections
   coming from the new box only.

This means the **actual traffic cutover is steps 3-4 above** — fast
(seconds, not a DNS propagation wait) and immediately reversible by
reversing the same two steps.

## Rollback

Because the old box is left running (stopped, not destroyed) through the
soak period, rollback is the tunnel-move steps in reverse:

1. `docker compose -f docker-compose.prod.yml up -d cloudflared` on the
   **old** box.
2. `docker compose -f docker-compose.prod.yml stop cloudflared` on the
   **new** box.
3. If the new box's database diverged at all during its soak period
   (any real writes happened there — deposits, tickets, rounds), that
   data needs to be exported and merged back, or accepted as lost,
   before trusting the old box's data as current again. This is the one
   part of rollback that isn't just "flip the tunnel back" — plan the
   soak period with this in mind (the shorter it is, the less there is
   to reconcile if a rollback becomes necessary).

**Time to roll back**: steps 1-2 alone take under a minute (the same
container start/stop this session has done all day). The real bound on
"how long rollback takes" is whichever of the following is slower: how
long it takes to notice something's wrong, or how much data-divergence
cleanup step 3 requires — which grows with however long the new box was
live and accepting real writes.

## Complete inventory: what lives only on the old box, not in git

Everything below either has to be copied to the new box directly, or
re-created there as a fresh one-time setup step — nothing here migrates
automatically just because the git repo does.

### Environment variables (names only — real values live in the
gitignored `deploy/.env`, copy the file directly, never reconstruct
values by hand)

From `deploy/.env.prod.example`, the full list this deployment needs:

```
POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, TELEGRAM_BOT_USERNAME
MINIAPP_URL
CHAPA_API_KEY, SANTIMPAY_API_KEY, ARIFPAY_API_KEY
PUBLIC_BASE_URL, PAYMENTS_PUBLIC_BASE_URL, AGENT_PORTAL_BASE_URL
MIN_DEPOSIT_ETB, DAILY_DEPOSIT_CAP_ETB, MIN_WITHDRAW_ETB,
  AUTO_APPROVE_WITHDRAW_ETB, KYC_REQUIRED_ABOVE_ETB,
  WITHDRAW_CHARGEBACK_WINDOW_MINUTES, MAX_WITHDRAWALS_PER_DAY
ADMIN_IP_ALLOWLIST
PHONE_ENCRYPTION_KEY
PUSHGATEWAY_URL, OTEL_EXPORTER_ENDPOINT
ENVIRONMENT, LOG_LEVEL
```

Two of these are **not safe to regenerate** on the new box — they must
be the exact same values as the old box, copied, not freshly generated:
`POSTGRES_PASSWORD` (if regenerated, the restored database's own role
password won't match until reset, a recoverable but avoidable extra
step) and `PHONE_ENCRYPTION_KEY` (this one is *not* recoverable — it's
the encryption key for `users.phone_e164_encrypted`; a new key makes
every already-encrypted phone number on the restored database
permanently unreadable, not just requiring a new one going forward).

### Cloudflare Tunnel credentials

- `deploy/cloudflared/tunnel-credentials.json` (gitignored)
- `deploy/cloudflared/config.yml` (gitignored, though its content is
  fully reconstructable from `config.yml.example` plus the real tunnel
  ID from the credentials file — the credentials file is the one truly
  irreplaceable secret here, since it's what Cloudflare's own CLI
  generated at `tunnel create` time)

### SSH access

- Whatever key(s) are currently authorized in the old box's own
  `~/.ssh/authorized_keys` for the `cosmic` user need a decision: reuse
  the same keypair (copy the private key to wherever will SSH into the
  new box) or generate a fresh one and authorize it on the new box
  before decommissioning the old one's access path. **Only confirmable
  by looking at the server** — this doc can't enumerate which keys are
  currently authorized from here.

### GitHub Actions self-hosted runner

- The runner registration itself (`Settings → Actions → Runners`) is
  tied to the specific machine it was set up on — it does not follow a
  git checkout. Re-registering on the new box is a real, one-time manual
  step (see the cutover sequence above). **Only confirmable by looking
  at the server**: whether a runner is even currently registered and
  active is unverified as of this doc — this session has only ever
  deployed via manual SSH + `docker build`/`docker compose`, never
  observed this pipeline actually fire.

### systemd timers

- `deploy/systemd/jobingo-{backup,basebackup,reconcile,prune-wal}.
  {service,timer}` — real, committed unit files, but installing them
  (`systemctl enable --now`) is a server-side step that has to be
  redone on the new box; the unit files existing in git doesn't mean
  they're active anywhere. **Only confirmable by looking at the
  server**: whether these are even installed and running on the *old*
  box today is itself unconfirmed (`docs/DISASTER_RECOVERY.md`'s own
  "Is this actually running?" checklist says exactly this) — worth
  checking `systemctl list-timers | grep jobingo` on the old box before
  assuming there's anything real to carry over.

### Cron jobs

- No cron jobs are referenced anywhere in this repo — the equivalent
  scheduling need is entirely covered by the systemd timers above.
  **Only confirmable by looking at the server**: whether anything
  outside this repo's own knowledge was ever added directly (`crontab
  -l` for every relevant user).

### Backup destinations

- `deploy/backup.sh`/`basebackup.sh` write to `backups/` and
  `backups/basebackups/` **relative to the repo checkout, on local
  disk, on the same box being backed up** — not off-box, not to any
  external storage. This is a real, standing gap independent of this
  migration: if the box itself suffers a disk failure, the backups meant
  to protect against data loss are lost with it. Worth deciding, as part
  of (or right after) this migration, whether backups should start
  going somewhere off-box (an object storage bucket, a second machine)
  rather than just re-establishing the identical local-only pattern on
  the new box.
- WAL archive (`backups/wal_archive/`) — same local-disk caveat, same
  continuous-archiving-only-protects-against-non-disk-failure gap.

### Docker named volumes

- `jobingo_prod_postgres_data`, `jobingo_prod_redis_data` — these don't
  need migrating directly; the cutover sequence above restores Postgres
  from a logical dump instead (cleaner, verifiable, matches this
  project's own tested restore path) and lets Redis start empty (it
  holds only the Keno round lock, pub/sub, and rate-limit state — all
  legitimately disposable, matching `packages/core/redis_conn.py`'s own
  documented "if Redis is wiped, the platform must recover fully from
  Postgres" invariant).

### A related, separate gap this research surfaced

- `docker-compose.prod.yml` has **no Prometheus, Grafana, or Pushgateway
  service at all** — those three only exist in the local-dev compose
  file (`deploy/docker-compose.yml`). Every Prometheus alert rule this
  project has (the original 8, plus the 2 this session is about to add
  for the stuck-round/engine-heartbeat gap) is real, promtool-validated
  YAML — but with no Prometheus instance in production to load and
  evaluate it, none of it can actually fire today. `docs/
  LAUNCH_BLOCKERS.md`'s LB-B5 already flags "alerting has rules but no
  confirmed real receiver" as a P0 — this finding is one layer more
  fundamental than that: it's not just an unconfirmed *receiver*, there
  may be no evaluator running at all. Worth deciding whether standing up
  the observability stack in production is part of this migration or a
  separate, following piece of work — not resolved here, flagged here.

## Verification checklist: proving the new box is equivalent

- [ ] `alembic_version` on the new box matches the old box's, at the
      moment of cutover.
- [ ] All 9 app containers + postgres + redis + cloudflared report
      `running`, zero restarts, on the new box (same check this session
      used after every deploy today).
- [ ] `curl -sI https://arada.click/healthz` returns `200` with a
      `server: cloudflare` header, from a machine with real outside
      internet access (not from the new box itself — that only proves
      the origin works, not that the public path through Cloudflare
      does).
- [ ] `cloudflared tunnel info <tunnel-name>` shows active connections
      from the new box only, none from the old one.
- [ ] Keno rounds are cycling on the new box (`SELECT id, status,
      betting_opened_at FROM keno_rounds ORDER BY id DESC LIMIT 5` —
      querying the database directly, not trusting logs, matching this
      session's own established verification discipline).
- [ ] A real Bingo room opens, a real join succeeds, and `ledger.
      reconcile()` returns clean on the new box (the exact check this
      session was mid-way through against the old box when it went
      unreachable — finish it here as the new box's own proof, not just
      the old box's).
- [ ] Row counts for `users`, `ledger_transactions`, `keno_rounds`,
      `rounds` on the new box match the old box's counts as of the
      restore point (confirms the restore captured everything, not a
      partial dump).
- [ ] `keno_enabled` is still `false` post-migration unless a deliberate
      decision was made to change it — a migration must never
      accidentally change a real product/risk setting.
- [ ] The systemd timers are installed and show a real `NEXT` time
      (`systemctl list-timers | grep jobingo`), not `n/a`.
- [ ] The GitHub Actions self-hosted runner (if re-registered) shows
      `Idle`/`Online` in the repo's Settings → Actions → Runners page,
      not `Offline`.
- [ ] The old box's `cloudflared` container is confirmed stopped (not
      just the new one confirmed started) — both running at once is the
      split-brain state the tunnel section above warns about.
