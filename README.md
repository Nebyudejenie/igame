# Zemen Game

**github.com/Nebyudejenie/igame is the only active repository as of
2026-09-21.** `~/AradaBingo` (github.com/Nebyudejenie/game) is archived,
not deleted — two checkouts sharing one dev Postgres container had
already caused a real migration-bookkeeping incident and a real broken
backup-restore test (both in `DECISIONS.md`'s 2026-09-21 entries). All
work happens in this checkout from now on; every commit pushes to
`igame`.

Real-money multiplayer bingo on Telegram for the Ethiopian market. The full
product/architecture spec lives in [`idea.md`](idea.md) (see especially the
"Jo Bingo" sections starting at line 4776 — that's the authoritative
blueprint this repo follows; see [`DECISIONS.md`](DECISIONS.md) for why).

This repo is being built phase by phase. **Phase 0 (foundations + ledger)
through Phase 7 (admin/risk/responsible-gaming) are done** — the full
player-facing loop (registration through a settled round), deposits and
withdrawals against Chapa, and the admin console (API + a real web
frontend: dashboard, users, payments, rounds, rooms, reports, risk, audit
log) all work end to end against real infrastructure, with real tests.
Real, known gaps -- not yet built, not silently missing -- see "What's
actually implemented" below for the honest list: a KYC *document
-verification pipeline* (the `kyc_level` threshold check on withdrawals
now has a real, audited admin action to promote/demote a user past it --
`POST /users/{id}/kyc`, reachable from the console's Users screen -- but
no automated document-collection/verification provider behind that
admin's own judgment call -- see that section) and tax export (zero code
either way). SantimPay/ArifPay adapters are blocked on unreachable docs
(Chapa is the one real provider), and load/chaos testing runs at a
smaller scale than the spec's literal 10k-socket figure, honestly
documented in `DECISIONS.md`. CI/CD (GitHub Actions + a self-hosted
-runner deploy) is also done -- see the CI/CD section further down.

## Repository layout

```
services/bot/          Phase 1: Telegram bot (aiogram 3, webhook mode) -- DONE
services/gateway/      Phase 3: realtime WebSocket gateway (FastAPI) -- DONE
services/engine/       Phase 2: game engine / room state machine workers -- DONE
services/wallet/       Empty placeholder -- wallet logic lives directly in
                        packages/core/ledger.py instead; nothing currently
                        needs a separate wallet-specific service layer
services/payments/     Phase 5-6: deposits + withdrawals against Chapa -- DONE
                        (SantimPay/ArifPay adapters not built, see above)
services/admin/        Phase 7: admin console API -- DONE; no tax export
services/sms/          Enterprise SMS Control Plane -- a genuinely
                        separate product (DECISIONS.md, 2026-09-07),
                        shares admin_users/RBAC/audit with services/admin/
packages/core/         Shared, framework-free domain logic (ledger, bingo, config, logging, redis, telegram auth)
packages/core/sms/     SMS domain logic (campaigns, messages, nodes, audience, compliance, templates)
web/miniapp/           Phase 4: Telegram Mini App (vanilla JS, no framework) -- DONE
web/admin/             Phase 7: admin console frontend (vanilla JS, no
                        framework, no build step -- same approach as
                        web/miniapp/) -- DONE, served at /console by
                        services/admin/app.py
web/sms/               SMS Control Plane console frontend (same vanilla-JS
                        approach), served at /console by services/sms/app.py
migrations/            Alembic migrations (raw SQL, no ORM)
tests/unit/            Pure-function tests, no external dependencies
tests/integration/     Tests against real Postgres + Redis (docker-compose)
deploy/                docker-compose.yml, docker-compose.prod.yml, and friends
```

## Running it locally

Requires Docker, Docker Compose, and Python 3.12.

```bash
# 1. Create a virtualenv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install -e . --no-deps

# 2. Start Postgres and Redis
#    (mapped to host ports 5433 / 6380, not the defaults, in case another
#    project on the same machine is already using 5432 / 6379)
docker compose -f deploy/docker-compose.yml up -d postgres redis
#
#    If you ever reset the stack with `down -v`: also clear
#    backups/wal_archive/ first (a host bind mount, deliberately NOT wiped
#    by `-v` -- see docker-compose.yml's own comment on why). A fresh
#    Postgres volume always restarts its WAL numbering at segment 1, which
#    collides with old segment 1 still sitting in that directory --
#    archive_command's own overwrite guard (test ! -f) then fails forever,
#    and any test/script that waits on archiving (test_backup_restore.py,
#    a real pg_basebackup) hangs indefinitely rather than erroring. This
#    cost a real multi-hour stall the first time it was hit.
rm -rf backups/wal_archive/*

# 3. Apply migrations
.venv/bin/alembic -c migrations/alembic.ini upgrade head

# 4. Type-check and run the tests
.venv/bin/mypy
.venv/bin/pytest tests/ -v

# 5. Real-scale load tests (up to ~1000 concurrent sockets, and a 1000-way
#    concurrent seat-allocation rush), run standalone for a clean reading
#    -- see DECISIONS.md on why this is excluded by default
.venv/bin/pytest tests/ -m load -v -s

# 6. Chaos tests that restart a real docker-compose service mid-test --
#    always run alone, never batched with anything else (see DECISIONS.md)
.venv/bin/pytest tests/ -m chaos_infra -v -s

# 7. Real-browser Mini App tests (Playwright/Chromium) -- also excluded by
#    default; install a browser once, then run explicitly
.venv/bin/playwright install chromium
.venv/bin/pytest tests/ -m e2e -v -s

# 8. Take a real backup, and restore it into a throwaway database to prove
#    it's actually usable (never touches the live "jobingo" database)
./deploy/backup.sh
./deploy/restore.sh backups/jobingo-<timestamp>.dump

# 9. Optional: real Prometheus metrics scraping (spec section 10.4).
#    Gated behind a profile, so step 2's plain `up -d` skips it -- start
#    it explicitly once you have a service running with --port matching
#    deploy/prometheus/prometheus.yml (8000 gateway, 8001 admin, 8002
#    payments, 8003 bot), then browse http://localhost:9091
docker compose -f deploy/docker-compose.yml up -d prometheus

# 10. Optional: let reconcile_job.py push its result to a real Pushgateway
#     (packages/core/reconcile_job.py has no long-running /metrics of its
#     own to scrape). Set PUSHGATEWAY_URL=http://localhost:9092 in .env.
docker compose -f deploy/docker-compose.yml up -d pushgateway

# 11. Optional: Grafana dashboards over the same metrics -- provisioned
#     automatically (deploy/grafana/), no manual setup. Brings up
#     Prometheus too. Browse http://localhost:3001 (anonymous viewer
#     access enabled for local dev; admin/jobingo for editing).
docker compose -f deploy/docker-compose.yml up -d grafana

# 12. Optional: OpenTelemetry traces for the deposit/payout paths. Set
#     OTEL_EXPORTER_ENDPOINT=http://localhost:4318 in .env, then browse
#     http://localhost:16686 to see them.
docker compose -f deploy/docker-compose.yml up -d jaeger
```

To actually play with it: start the gateway (`uvicorn services.gateway.app:app
--reload`, from the repo root) and open `http://localhost:8000/` -- the
gateway serves the Mini App's static files itself. Outside a real Telegram
client `window.Telegram.WebApp` won't exist, so the app has nothing to
authenticate with; the E2E tests stub it (see
`tests/integration/test_miniapp_e2e.py`) the same way a real integration
would supply real `initData`.

The admin console (`web/admin/`, mounted at `/console` by
`services/admin/app.py`) has the same kind of real-browser coverage:
`tests/integration/test_admin_console_e2e.py` drives an actual Chromium
tab through login, the dashboard, a real state-changing action (setting
a user's KYC level end to end, confirmed against the database, not just
the toast), an RBAC-denied screen, and logout -- the permanent
regression coverage that didn't exist when the frontend itself shipped
(only a one-off, uncommitted verification script did at the time).

The SMS Control Plane console (`web/sms/`, mounted at `/console` by
`services/sms/app.py`) is the same pattern again: run `uvicorn
services.sms.app:app --port 8006 --reload` and open
`http://localhost:8006/console/` — it logs in with the exact same
`admin_users` accounts as the Bingo admin console (no separate account to
create). `tests/integration/test_sms_console_e2e.py` has the same
real-Chromium regression coverage: login, a full campaign created through
the UI (contact → node → campaign → validate → start), and the delivery
result reflected back once a (simulated) node reports it.

Copy `.env.example` to `.env` if you want to override any connection
settings; most defaults are baked into `packages/core/config.py`, which
already point at the docker-compose ports above. One exception:
`PHONE_ENCRYPTION_KEY` has no safe empty default (registration can't
function without it) — `.env.example` ships a real, working dev key, but
you do need the actual `.env` file copied over for anything that touches
registration to run outside the test suite (which sets its own fixed key
directly, so `pytest` works with no `.env` at all).

**Dependency lockfiles**: `requirements.lock` (production) and
`requirements-dev.lock` (adds pytest/mypy/playwright) pin the exact
resolved dependency tree, not just `pyproject.toml`'s own loose `>=`
constraints — CI, the production `Dockerfile`, and the quick-start above
all install from one of these two, not from `pyproject.toml` directly, so
what actually runs is always the exact set of versions that was actually
tested. After changing a dependency in `pyproject.toml`, regenerate both
with [`uv`](https://docs.astral.sh/uv/) (a plain `pip install --upgrade
pip-tools`-style compiler works too, since these are ordinary
`requirements.txt`-format files — `uv` is just faster):

```bash
uv pip compile pyproject.toml -o requirements.lock --python-version 3.12
uv pip compile pyproject.toml --extra dev -o requirements-dev.lock --python-version 3.12
```

## CI/CD

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request
against `main`: `mypy --strict`, the default test suite, `-m chaos_infra`,
`-m e2e` (all blocking), `-m load` (informational only — see the job's own
comment for why an absolute latency budget isn't meaningful on a shared
GitHub-hosted runner), and a real `docker build` of the production image.
Needs no setup — it's a normal GitHub Actions workflow using GitHub-hosted
runners.

**CD** (`.github/workflows/cd.yml`) builds and pushes a Docker image to
GHCR, then deploys it — but only after CI genuinely succeeds on `main`
(`workflow_run`, not a plain push trigger; there is no path from a red CI
run to a deploy). The deploy step runs on a **self-hosted runner** on the
actual target server, since GitHub's own cloud runners can't reach a
private/local machine directly. One-time setup before the first deploy
works:

1. Register a self-hosted runner for this repo: **Settings → Actions →
   Runners → New self-hosted runner**, then run the setup script it gives
   you on the server that will actually run the app. No custom label
   needed.
2. On that same server, in this repo's checkout, copy
   `deploy/.env.prod.example` to `deploy/.env` and fill in real values
   (`POSTGRES_PASSWORD`, `PHONE_ENCRYPTION_KEY`, etc.). Never commit it —
   the deploy job's checkout step deliberately skips its usual clean-working
   -tree behavior so this file survives every future deploy instead of
   being wiped.
3. Set up the domain and Cloudflare Tunnel — see the dedicated section
   right below. Do this before filling in `deploy/.env`'s `PUBLIC_BASE_URL`/
   `PAYMENTS_PUBLIC_BASE_URL`/`MINIAPP_URL`, since those need the real
   subdomains this step creates.
4. Optional but recommended for a real-money system: **Settings →
   Environments → New environment** named `production`, with required
   reviewers turned on. This gates the deploy job behind manual approval
   even after CI passes — genuinely worth it before this is handling real
   transactions. With no reviewers configured, deploys run straight
   through once CI is green.

`deploy/docker-compose.prod.yml` is the actual production stack: Postgres,
Redis, a one-shot migration job every other service waits on, all six
deployable units (`Dockerfile` builds one shared image; each service just
picks its own command against it) — gateway/admin/payments/bot on ports
8000-8003, engine-worker/payout-worker's `/metrics` endpoints (the only
HTTP surface those two have) on 8004-8005 — and `cloudflared`, the only
ingress path onto a Proxmox VM with no public IP of its own.

### Domain and Cloudflare Tunnel

**Two separate production deployments exist, on two different servers,
each with its own domain and its own cloudflared architecture. Don't
conflate them.**

#### arada.fun (original deployment — unaffected by the Zemen Game rebrand)

`docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` confirmed directly against the
live host on 2026-09-05: `cloudflared` runs as a **host-level systemd
service** (outside Docker, outside this repo) forwarding into a
**Traefik** instance from a separate, shared stack on the same host (also
outside this repo), which does the real per-hostname routing. The live
hostnames are `arada.fun`, `payments.arada.fun`, `agent.arada.fun`,
`admin.arada.fun`, `finance.arada.fun`, `sms.arada.fun`. Because that
Traefik routing lives entirely outside git, a 2026-09-14 incident found
four of those routers had silently disappeared from the live host with
zero version-controlled copy to recover from (`arada.fun` itself was
unaffected — see `DECISIONS.md`'s entry from that date, and
`deploy/traefik/jobingo-dynamic.yml.example`, a reconstructed reference of
what that config should contain, added specifically so recovering from a
repeat of this doesn't depend on tribal memory). Read
`docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` before touching anything on
that host.

#### arada.click (Zemen Game deployment — this section's own instructions)

This deploys to a genuinely different server, reached through the domain
**arada.click** (Cloudflare-managed DNS, supporting unlimited subdomains)
via a **Cloudflare Tunnel**. Unlike arada.fun, `cloudflared` here runs as
**one of this repo's own Docker containers**
(`deploy/docker-compose.prod.yml`'s `cloudflared` service), routing
straight to each Compose service by its plain service name — no separate,
untracked Traefik stack in the loop. This is deliberate, not an
oversight: it closes the exact "routing config lives outside git" gap
that caused arada.fun's 2026-09-14 incident, for this deployment, from
day one.

The hostname *pattern* mirrors arada.fun's real, live scheme rather than
reinventing one: one apex hostname carries both the Mini App/gateway and
the Telegram webhook (split by path), and payments/admin each answer on
two hostnames apiece for the same underlying container
(`engine-worker`/`payout-worker` are never exposed — their `/metrics`
ports are for internal Prometheus scraping only):

| Hostname | Routes to | Serves |
|---|---|---|
| `arada.click`, `www.arada.click` | `gateway:8000` | Mini App, player REST API, WebSocket |
| `arada.click` + `PathPrefix(/webhook)` | `bot:8003` | Telegram's webhook, `POST /webhook` |
| `payments.arada.click` | `payments:8002` | Chapa's real webhook, `POST /webhooks/chapa` |
| `agent.arada.click` | `payments:8002` | Payment Agent Portal (same container as payments, see `docs/AGENT_DASHBOARD_GUIDE.md`) |
| `admin.arada.click` | `admin:8001` | Admin console (IP-allowlisted at the app layer — `ADMIN_IP_ALLOWLIST` is the real boundary, not the subdomain) |
| `finance.arada.click` | `admin:8001` | Same container as admin, finance-role login (a different set of screens, enforced server-side) |
| `sms.arada.click` | `sms:8006` | SMS Control Plane console + node protocol (DECISIONS.md, 2026-09-07) — a separate product, same admin accounts |

Routing is defined in a committed config file, not clicked together in
the Cloudflare dashboard — see `deploy/cloudflared/config.yml.example`.
One-time setup, on the arada.click server itself:

1. Install the `cloudflared` CLI ([Cloudflare's own instructions](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) — a `.deb`/`.rpm` for the server's OS).
2. `cloudflared tunnel login` — opens a browser to authorize against the
   Cloudflare account arada.click's DNS is managed under.
3. `cloudflared tunnel create zemen-game` — creates the tunnel and writes
   a credentials JSON file (path printed on success) and prints the
   tunnel's id. Copy that credentials file to
   `deploy/cloudflared/tunnel-credentials.json` (gitignored, like
   `deploy/.env`).
4. `cloudflared tunnel route dns zemen-game <hostname>`, once per hostname
   in the table above (`arada.click`, `www.arada.click`,
   `payments.arada.click`, `agent.arada.click`, `admin.arada.click`,
   `finance.arada.click`, `sms.arada.click`) — this is what actually
   creates the DNS records; nothing to add by hand in the Cloudflare
   dashboard, and arada.click's Cloudflare-managed zone means each of
   these is just a new record, no extra domain purchase needed.
5. Copy `deploy/cloudflared/config.yml.example` to
   `deploy/cloudflared/config.yml` (gitignored) and replace `<TUNNEL_ID>`
   with the id step 3 printed. The `ingress:` rules already match the
   table above — no other edits needed unless a subdomain changes.

Once `deploy/.env` has real values for `MINIAPP_URL`
(`https://arada.click`), `PUBLIC_BASE_URL` (`https://arada.click`, the
same apex hostname — the webhook is split by path, not by subdomain),
`PAYMENTS_PUBLIC_BASE_URL` (`https://payments.arada.click`), and
`AGENT_PORTAL_BASE_URL` (`https://agent.arada.click`), `docker compose -f
docker-compose.prod.yml up -d` brings up `cloudflared` alongside
everything else and the tunnel starts routing real traffic — same
lifecycle as every other service in the stack, restarted automatically on
failure (`restart: unless-stopped`), never profile-gated (unlike the dev
compose file's optional observability services, this tunnel *is*
production ingress, not an add-on).

`deploy/traefik/*` describes arada.fun's external Traefik stack and does
not apply to this deployment — arada.click needs no Traefik at all,
cloudflared's own ingress rules do the per-hostname (and per-path)
routing directly.


**Verified against the real repo, not assumed:** a status-audit pass
checked GitHub's own run history (`gh run list`) rather than trusting
this file's earlier claim that CD only needed a runner. It didn't --
`build-and-push` had genuinely failed on every single run so far
(`docker build`'s tag rejected: GHCR requires an all-lowercase
repository name, and `github.repository` preserves this repo's real
mixed-case owner). Fixed (lowercased in the same shell step, see
`DECISIONS.md`). The self-hosted runner is still not registered and no
`production` environment exists yet (`gh api .../actions/runners` and
`.../environments` both currently return zero) -- that part of step 1/3
above is still a real, open action item, now confirmed to be the only
one CI/CD's own automation can't do for you.

## What's actually implemented

**Foundations (Phase 0):**
- **`packages/core/ledger.py`** — the double-entry ledger. Every balance
  change is one or more signed `ledger_entries` rows written inside a
  database transaction; Postgres itself (via a deferred constraint trigger)
  rejects any transaction whose entries don't sum to zero. `post()` is
  idempotent by key, and safe under concurrency via per-account row locks
  taken in a fixed order.
- **`packages/core/bingo.py`** — pure game logic: the deterministic 150-card
  pool, win-pattern detection (rows/columns/diagonals/corners), and the
  provably-fair draw (`HMAC-SHA256`-seeded Fisher-Yates over 1–75).
- **`packages/core/cards_seed.py`** + a migration — seeds the `cards` table
  from the pool above.

**Game engine (Phase 2):**
- **`services/engine/room_lock.py`** — Redis-backed single-owner election
  for a room (`SET ... NX EX`, refreshed on a timer, compare-and-clear Lua
  scripts so a worker can never refresh or release a lock it no longer
  owns).
- **`services/engine/round_engine.py`** — the room state machine itself:
  IDLE → LOBBY → RUNNING → SETTLING → IDLE. Server-authoritative number
  calling with drift-corrected timing, claim validation that always
  recomputes the marked grid from the round's own called-numbers set (never
  from anything a caller supplies), a 50ms tie window for simultaneous
  winners, and atomic ledger-backed settlement.
- **`services/engine/settlement.py`** — pure payout math (derash/house-cut
  split, tie splitting), independently unit-tested.
- **`services/engine/refunds.py`** — the one shared, idempotent full-refund
  path used by an underfilled lobby, a round that exhausts all 75 calls with
  no winner, and crash recovery alike.
- **`services/engine/recovery.py`** + **`worker.py`** — crash safety: on
  startup, any round left in a non-terminal state whose room lock has gone
  stale gets voided and fully refunded, never silently resumed.

**Realtime gateway (Phase 3):**
- **`packages/core/telegram_auth.py`** — Mini App `initData` validation: the
  exact HMAC-SHA256 double-hash algorithm, constant-time comparison, and
  24-hour replay-window rejection. The entire auth boundary; no session
  cookie exists anywhere else.
- **`services/engine/commands.py`** — the gateway↔engine bridge: one Redis
  Stream per room carries `join`/`drop_card`/`claim`/`set_auto` requests to
  whichever engine currently owns that room, replies correlated back over a
  per-request pubsub channel with a timeout.
- **`services/gateway/fanout.py`** — one Redis `psubscribe` per gateway
  process fans out to every locally-connected socket without ever
  re-serializing a message per connection; a bounded per-connection queue
  implements the spec's backpressure rule (drop ticks, queue a fresh
  `state_sync`) so one slow reader can never delay everyone else in the room.
- **`services/gateway/connection.py`** + **`app.py`** — the FastAPI
  WebSocket endpoint: auth handshake, rate limiting (Redis token buckets),
  message dispatch, and `state_sync` served straight from Postgres (the
  durable source of truth) so reconnection works even if the room's engine
  just crashed.

**Telegram bot (Phase 1):**
- **`services/bot/registration.py`** + **`phone.py`** — the registration
  flow: a shared contact only proves phone ownership if its `user_id`
  matches the sender (any mismatch is rejected outright — Telegram lets
  anyone forward anyone's contact card), phone numbers are normalized to
  E.164, typed numbers are never accepted as a substitute for the
  Share-Phone-Number button. A user who opens the Mini App before ever
  messaging the bot (a real path — `services/gateway/queries.py` lazily
  creates a phoneless row for exactly that case) completes registration
  in place by sharing their contact, rather than crashing — a real,
  reachable crash a `/code-review` pass caught and fixed. See
  `DECISIONS.md`.
- **`services/bot/notifier.py`** — the only code path allowed to call
  `bot.send_message`; paces to ~25 msg/s and backs off per-chat on a 429
  without stalling every other chat's queue behind one rate-limited one.
- **`services/bot/dedup.py`** — webhook `update_id` deduplication via
  Redis, so a retried Telegram webhook can't double-process anything.
- **`services/bot/i18n.py`** + **`locales/`** — every user-facing string
  keyed, Amharic default, English complete, `om`/`ti` stubbed with
  fallback — enforced mechanically, not just by convention, by an AST-based
  test that fails on any hardcoded string literal in `handlers.py`.
- **`services/bot/handlers.py`** + **`app.py`** — the aiogram 3 webhook
  app: full command set, referral deep links (credit survives a failed
  first registration attempt, e.g. a mismatched contact — a real bug a
  `/code-review` pass caught, see `DECISIONS.md`), and real reads/writes
  against the same ledger and round tables the engine and gateway use —
  `/balance` and `/history` show actual money and actual games, not
  placeholders. `/deposit`, `/withdraw`, and `/limits` honestly report
  "not available yet" rather than faking Phase 5-7 functionality that
  doesn't exist.

**Mini App (Phase 4):**
- **`web/miniapp/`** — the full player-facing client per the Mini App UI
  spec: room list, card selection (two-tap commit), the live game screen
  (75-cell call board and 5×5 card both rendered once and mutated in
  place, never re-rendered per call), spectate mode, the result screen,
  and a wallet view. Vanilla JS (ES modules), no framework, no build step.
- **`js/ws.js`** — the WebSocket client, matching the gateway's protocol
  exactly: auth handshake, transparent reconnect (re-authenticates and
  re-joins the active room, restoring state from the server's own
  `state_sync` rather than anything remembered client-side), server-time
  offset tracking for a countdown that never trusts the device clock.
- **`js/i18n.js`** + **`locales/`** — Amharic default, English complete,
  same fallback rules as the bot side. Amharic renders via a self-hosted
  `Noto Sans Ethiopic` subset (`fonts/`) — regenerable with
  `fonts/subset.sh` — cut from Google's ~200KB full delivery to ~11.5KB by
  subsetting to the codepoints the current locale files actually use
  (spec budget: 40KB).
- **`services/gateway/app.py`** now also serves the Mini App's static
  files and four REST endpoints (`/api/me`, `/api/history`, `/api/deposit`,
  `/api/withdraw`) for the wallet screen, authenticated the same
  `Authorization: tma <initData>` way as the WebSocket handshake.

**Mini App wallet completion — deposit, withdraw, history, reality check:**
- **Deposit and withdraw tabs** (previously "launching soon" placeholders)
  now work end to end: an amount picker with quick-select chips for
  deposits, an amount/telebirr-number/holder-name form for withdrawals,
  both calling the two new gateway REST routes and showing a real,
  translated outcome — a real checkout link (opened via `tg.openLink`), a
  real approved/review status, or a specific, translated rejection reason
  (below minimum, self-excluded, insufficient balance, ...).
- **History tab** now calls the `/api/history` endpoint that already
  existed since Phase 4 but was never wired to the UI — every settled
  round the player was in, with its outcome.
- **Reality check** (spec section 12: "net position this session, shown
  plainly") — a running total on every results screen, computed entirely
  from WebSocket events the client already receives
  (`web/miniapp/js/state.js`), colored green/red for up/down. Deliberately
  client-side and reset-on-reload, unlike every *enforced*
  responsible-gaming control (those are all server-side) — this one is an
  awareness nudge, not a control that must survive a page reload.
- **Session-time reminders** ("you've been playing 60 minutes") — same
  reasoning, same file: a plain client-side timer against
  `sessionStartedAt`, toasted at 60/120/180 minutes.
- Verified in a real Chromium browser against the real backend (spec
  discipline: UI changes get used in a browser, not just asserted against
  a DOM), including a real fake-provider-backed deposit returning an
  actual checkout URL and a withdrawal actually locking funds — see
  `tests/integration/test_miniapp_wallet_e2e.py`. Three real bugs in the
  new *tests themselves* were caught and fixed by actually running them
  (a wait condition racing an intermediate status message, a screen with
  no route back to the wallet button in the test stub, a mismatched
  Amharic substring check) — see `DECISIONS.md`.

**Provably-fair verification, made real** — spec section 14's definition
of done requires "a player can independently verify any round's draw from
the published seed." The Mini App's "Verify draw" button had existed since
Phase 4 with no click handler attached — clicking it did nothing. Fixed:
a new player-facing `GET /api/rounds/{id}/fairness` route on
`services/gateway/app.py` (reusing `services/admin/queries.
get_round_fairness()` directly — none of that data is admin-restricted),
and the result screen now shows the committed hash, the revealed seed, and
a ✅/❌ verified indicator when clicked. Tested with a genuine independent
check (the test hashes the revealed seed itself and asserts it matches the
pre-committed hash, not just trusting the server's own "verified" field)
and a real Chromium browser playing an actual round to completion and
clicking the actual button. See `DECISIONS.md`.

**Deposits (Phase 5):**
- **`services/payments/provider.py`** — a provider-agnostic
  `PaymentProvider` Protocol (`create_checkout` / `verify_webhook` /
  `fetch_status` / `create_payout`); nothing in the deposit logic imports a
  specific rail by name.
- **`services/payments/chapa.py`** — the real Chapa adapter (Ethiopia's
  primary rail): checkout creation, the two-header HMAC-SHA256 webhook
  signature scheme Chapa's own docs require both of, status polling, and
  payout creation. Endpoint paths and the signature scheme were confirmed
  against Chapa's live developer docs, not reconstructed from memory.
- **`services/payments/deposits.py`** — `create_deposit_intent()` (minimum
  amount, daily cap, self-exclusion all checked before a provider is ever
  called), `handle_webhook()` and `poll_pending_deposits()` sharing one
  crediting path so a late webhook after a successful poll is a structural
  no-op, and a pure `reconcile()` for the hourly settlement-report
  comparison job. Every credit goes through `packages/core/ledger.py` with
  `idempotency_key = our_ref`, the exact same discipline as round
  settlement.
- **`services/payments/app.py`** — the one real inbound HTTP surface:
  `POST /webhooks/chapa`, signature-verified before anything else runs.
  Deposit *creation* is a plain Python call from the bot, the same way the
  bot already reads/writes the ledger directly for `/balance`.
- **`services/bot/handlers.py`**'s `/deposit <amount>` now actually works
  end to end — checkout link, real validation errors, no more "launching
  soon" once `CHAPA_API_KEY`/`PUBLIC_BASE_URL` are configured.
- A deposit's ledger credit publishes to the `user:{id}` Redis channel
  `services/gateway/fanout.py` has subscribed to since Phase 3 (built ahead
  of need, unused until now); `web/miniapp/js/app.js` picks it up live —
  the header balance and an open wallet screen update without a reload.

Only SantimPay/ArifPay were left unbuilt — attempted later in this session
and blocked on a different, more fundamental issue than credentials:
their API documentation was unreachable from this environment entirely
(DNS failures, connection refused). Building either adapter from a
guessed contract was a deliberate refusal, not an oversight — a wrong
webhook signature scheme is a real security hole. Live testing against
Chapa's own sandbox hasn't happened either, for the credentials reason —
see `DECISIONS.md` for exactly what's still open before real money should
move through any of this.

**Withdrawals (Phase 6):**
- **`services/payments/withdrawals.py`** — `request_withdrawal()`: the
  validation gate (minimum amount, KYC level above a threshold, no
  succeeded deposit in the chargeback window), then the spec's own
  "single most important step" — funds move `user_cash → user_locked`
  through the ledger in the *same transaction* that creates the
  `payments` row, so there is no window in which a player can both
  request a withdrawal and stake the same birr. Bonus funds are excluded
  structurally (only `user_cash` is ever debited), not by a conditional
  check. Auto-approves under a configurable limit (account age, amount,
  lifetime deposits ≥ withdrawals); otherwise lands in the admin review
  queue.
- **`services/payments/payout_worker.py`** — a real Redis Streams
  consumer group (the first one in this codebase), so more than one
  worker replica can safely share the payout queue and a crashed
  worker's in-flight job gets redelivered rather than lost. Exactly-once
  *dispatch* is the provider's own job (`our_ref` is passed as Chapa's
  idempotency reference), so it's safe — not just tolerated — for a
  redelivered job to call `create_payout()` again; what this module
  guarantees on its own side is that a payment already in a terminal
  state is never re-settled, via the same `ledger.post()` idempotency-key
  discipline used everywhere else in this codebase. Failure or explicit
  rejection reverses the lock, returning the exact amount to `user_cash`.
- **`services/admin/queries.py`** gained the review queue:
  `list_pending_withdrawals`, `approve_withdrawal_admin` (audit-logged,
  enqueues the real payout job), `reject_withdrawal_admin`
  (ledger-reversed, audit-logged) — both safe no-ops if the payment isn't
  in `review` any more. New RBAC permissions `payments:view` and
  `payments:approve`. `payments.review_reason` (a code-review addition)
  records *which* of the four auto-approve rules actually failed, built
  from the same values `auto_ok` itself is computed from — an admin
  working the queue (`web/admin/`'s Payments screen shows it as its own
  column) no longer has to manually re-derive why a given request landed
  there. See `DECISIONS.md`.
- **`services/bot/handlers.py`**'s `/withdraw <amount> <telebirr number>
  <full name>` works end to end — real validation errors, real
  auto-approve-vs-review outcome reported back to the player.

Two real, open gaps from spec 8.3/8.4's anti-fraud rules, not oversights:
holder-name-matches-account-name can't be meaningfully checked without a
real KYC/identity pipeline (comparing against Telegram's self-reported
display name would be theater, not a control), and the auto-approve rule's
"risk score below threshold" has no risk-scoring system to check against —
none of spec 8.4's collusion/device-fingerprint/velocity flags are built.
See `DECISIONS.md`.

**Bot notification relay** — the one gap Phase 5 and Phase 6 both
explicitly deferred to build once, together: `packages/core/
notifications.py` (producer — `notify_user()`, called after money has
already moved, never able to make that movement fail) and
`services/bot/notification_relay.py` (consumer — a real Redis Streams
consumer group, this codebase's second one, and the only thing outside
`services/bot/handlers.py` allowed to call `Notifier.send()`, keeping that
invariant intact even across process boundaries). Wired into a confirmed
deposit, a payout succeeding or failing, and an admin's explicit
withdrawal rejection (which alone includes the rejection reason — a
provider-side failure's raw internal error text is deliberately never
shown to a player). Its own test suite caught the *third* instance this
session of the same shared-Redis-Stream test-pollution bug
(`payout_worker.py`'s queue had the same issue in Phase 6) — fixed with
the same autouse-cleanup-fixture pattern.

**Responsible gaming (spec section 12):**
- **`packages/core/responsible_gaming.py`** — deposit and loss limits
  (instant decrease, 24-hour delay on any increase — the effective cap is
  computed lazily from a `pending_*`/`pending_*_effective_at` pair, no
  scheduled job involved), cool-off (purely timestamp-driven —
  `cooloff_until` lifts itself the moment it passes, no status flip, no
  cron), and self-exclusion (`users.status = 'self_excluded'`, minimum 180
  days, and — the thing that actually makes it irreversible — there is no
  "undo" function anywhere in this codebase, not a duration check standing
  between a player and reversing it).
- **`RoundEngine.join()`** now gates every stake through
  `check_stake_allowed()`: blocks self-excluded, banned, and
  currently-cooling-off users, and blocks a stake that would push the
  day's realized net loss past a configured cap — one combined SQL query
  in the common case (no limits set), since this is a hot path called on
  every join.
- **`services/payments/deposits.py`**'s `create_deposit_intent()` also
  blocks banned and cooling-off users now (self-exclusion was already
  blocked since Phase 5), and layers an optional tighter per-user deposit
  cap on top of the platform-wide one.
- **`services/bot/handlers.py`**'s `/limits` command: `deposit <amount>`,
  `loss <amount>`, `cooloff <24h|7d|30d>`, `selfexclude confirm` (the
  explicit `confirm` token is deliberate friction for an irreversible
  action, not a UX accident).
- **`marketing_eligible_user_ids()`** — the query spec section 12 asks for
  ("segment your notification queries by users.status at the query level
  so it can't be forgotten"), filtering out self-excluded, banned, and
  currently-cooling-off users. No bulk-notification feature calls it yet
  (none exists in this codebase), but the audience query itself is
  correct and tested now, ready for whenever one is built.

Session-time reminders and the results screen's reality check were
initially deferred as Mini App frontend work (the same reasoning the
deposit-amount picker UI was deferred in Phase 5) and have since been
built — see "Mini App wallet completion" above. A narrower gap has since
been closed: `users.kyc_level` (the field `withdrawals.py`'s own
threshold gate reads) used to have no writer anywhere in the codebase —
an admin action (`services/admin/queries.py::set_kyc_level`, RBAC-scoped
to `finance`/`superadmin`, fully audited, promotions and demotions both
supported) now provides one. What still doesn't exist is any real,
automated document-collection/identity-verification pipeline behind that
action — an admin promotes a user's level only after reviewing their
documents through some out-of-band channel, and which channel that is
remains a genuine, unmade product decision. See `DECISIONS.md`.

Spec section 12's other age-related control -- "18+ declaration at
registration" -- is also now real and separate from the KYC-level
pipeline above: `users.age_confirmed_at`, set the moment registration
first completes (`services/bot/registration.py`), alongside declaration
text now shown in the registration prompt itself
(`register.prompt` in both locale files). This is a self-declaration, not
identity verification -- the same distinction spec 12 itself draws
between "age gate" and "ID verification at KYC level 2." Still open:
dedicated problem-gambling helpline links in Amharic (`/support` only
points to a generic contact today), and a real automated device
-fingerprint collusion signal (no fingerprint is collected anywhere in
the Mini App). See `DECISIONS.md`.

**Admin console (Phase 7):**
- **`services/admin/auth.py`** — a completely separate authentication path
  from players: username + bcrypt password + TOTP 2FA, Redis-backed session
  tokens (server-side revocable on logout, unlike a JWT), and login failures
  that all raise the same generic error regardless of which check failed
  (unknown username, wrong password, wrong code) so the error text itself
  can't be used to enumerate usernames or probe 2FA status — and, since a
  `/code-review` pass caught the gap, the same is now true of response
  *timing*: an unknown username pays the same bcrypt cost a real one does,
  instead of returning instantly. Login attempts are also now rate
  limited (5 per 15 minutes per username) — previously unthrottled
  entirely. See `DECISIONS.md`.
- **`services/admin/rbac.py`** — a single `PERMISSIONS` dict mapping each
  permission to the roles allowed to use it (`support` / `finance` / `ops` /
  `superadmin`), checked through one `has_permission()` function on every
  route — no scattered role checks to get out of sync.
- **`services/admin/queries.py`** — every admin action that touches money
  (`adjust_balance`, `void_round_admin`) goes through
  `packages/core/ledger.py` like any other transaction, never a direct
  balance write; every mutation writes an audit log row with before/after
  state, in the same transaction as the mutation itself — `void_round_admin`
  didn't actually honor that for its own refund until a `/code-review`
  pass caught it (the refund and its audit entry used to be two separate,
  independently-committable transactions). `set_user_status` can suspend
  or lift a suspension, but can never set *or* reverse self-exclusion —
  a real bug the same review pass caught: any ops/finance admin could
  previously undo a legally-mandated self-exclusion through this generic
  endpoint. See `DECISIONS.md`.
- **`services/admin/audit.py`** + a migration — `admin_audit_log` is
  append-only at the database level: a `BEFORE UPDATE OR DELETE` trigger
  raises rather than relying on convention.
- **`services/admin/app.py`** — the FastAPI admin API: dashboard summary,
  user search/detail/ledger history, balance adjustment, user status
  (active/limited/banned — never self-exclusion, see above), KYC level
  promotion/demotion, round list/detail, the fairness-verification
  route (`GET /rounds/{id}/fairness` — reveals the committed `server_seed`
  once a round is terminal and independently re-verifies the draw), admin
  round voiding, room CRUD, reports (daily GGR, player LTV, weekly
  retention cohorts), risk screens (shared payout-account clusters,
  repeat winner/loser room pairings), and the audit log itself
  (restricted to `superadmin`) — plus an optional source-IP allowlist,
  also enforced (via a dedicated middleware, since it has no per-route
  `Depends` of its own) on **`web/admin/`**, the real frontend for all of
  the above mounted at `/console`: plain HTML/CSS/vanilla-JS, no
  framework or build step, same approach as `web/miniapp/`. Bearer
  session token in `localStorage`, one screen per nav item, each
  screen's own fetch calls hitting this same API — verified with a real
  Playwright walkthrough (login, every screen, a real KYC-level action
  end to end through the UI, logout) against the real dev database, not
  just curl. That same middleware also covers FastAPI's own
  auto-generated `/docs`, `/redoc`, and `/openapi.json` — a code-review
  pass that actually enumerated `app.routes` (not just routes anyone had
  written by hand) found these bypassing the allowlist entirely,
  exposing this real-money panel's whole API surface to anyone on the
  network. Same class of gap `/metrics` and `/auth/login` were each
  separately caught with before, just never checked in a place nobody
  writes by hand. See `DECISIONS.md`.
- **Reports (spec section 11):** player LTV (net lifetime deposits minus
  withdrawals, both per-user on the user detail view and as a ranked
  leaderboard) and weekly signup-cohort retention (one set-based SQL
  query, not a per-user loop, since this has to scale with real data
  volume) — the two report types from the spec's list that were
  genuinely computable from data already in the system. Retention's own
  `elapsed` flag (a `/code-review` fix) keeps a cohort's still-in-progress
  weeks from reading as 0% churn — see `DECISIONS.md`. Bonuses/referral
  rewards and tax export were deliberately not attempted: the former
  needs real business parameters (bonus amounts, wagering multipliers)
  this session has no authority to invent; the latter needs a specific
  format the tax authority requires, genuinely unknown here — guessing
  at a compliance export risks something worse than no export at all.
  (The Risk screens above cover two of spec 8.4's anti-fraud rules that
  *were* computable from existing data — see `DECISIONS.md` for why a
  stored `risk_flags` table and device-fingerprint clustering were
  scoped out of that same pass.) See `DECISIONS.md`.

Building the fairness route surfaced a real gap in Phase 2:
`round_engine.py` was committing to `server_seed_hash` up front (correct)
but never actually persisting the revealed `server_seed` anywhere durable
once a round finished — it only ever went out once over an ephemeral Redis
pub/sub message. Fixed so `rounds.server_seed` is written at both terminal
points (settlement and exhausted/no-winner refund), making historical
fairness verification actually possible instead of only "provable" for
whoever happened to be connected at the exact second the round ended. See
`DECISIONS.md`.

**Nightly ledger reconciliation:**
- **`packages/core/reconcile_job.py`** — spec section 14's definition of
  done requires "ledger sum equals balance cache for every account,
  verified nightly, zero drift over 30 days." `ledger.reconcile()` already
  did the comparison; this is the actual runnable job — the first genuine
  `if __name__ == "__main__":` CLI entrypoint in the codebase (every other
  background process, `EngineWorker`/`Notifier`/`payout_worker`/
  `notification_relay`, is a class/function only, with process
  orchestration left to deployment time throughout this session).
  `python -m packages.core.reconcile_job` exits 0 and logs
  `ledger_reconciliation_ok` when every account's cached balance agrees
  with its ledger entries, or exits 1 and logs every mismatched account
  (`ledger_reconciliation_failed`) otherwise — meant to be invoked by a
  real cron/systemd-timer/k8s CronJob that alerts loudly on the non-zero
  exit. If `PUSHGATEWAY_URL` is set, the mismatch count is also pushed to
  a real Prometheus Pushgateway (`job="reconcile_job"`) — a push failure
  is logged but never changes this job's own exit code. See `DECISIONS.md`.
- **In production**, run it via `deploy/docker-compose.prod.yml`'s
  `reconcile-job` service (same shared image, same env, no separate
  setup): `docker compose -f docker-compose.prod.yml run --rm
  reconcile-job`. It's `profiles: ["reconcile"]`-gated so a plain
  `up -d` never runs it — schedule the exact command above from
  whatever the deploy server actually has (a crontab line, e.g.
  `0 3 * * * cd /path/to/deploy && docker compose -f
  docker-compose.prod.yml run --rm reconcile-job`, or a systemd timer)
  once that server's real checkout path is known — the same one-time,
  can't-be-committed-in-advance step the CD workflow's own runner
  registration already requires (see `.github/workflows/cd.yml`).

**Backup and restore:**
- **`deploy/backup.sh`** — real `pg_dump -F custom` against the
  docker-compose Postgres, written to a timestamped file under
  `backups/` (gitignored).
- **`deploy/restore.sh`** — real `pg_restore` into a target database,
  dropping and recreating it first so the result is exactly what's in
  the dump. Defaults to a separate `jobingo_restore_drill` database, never
  the live one, so it can never be an accidental overwrite.
- Verified with a real drill (`tests/integration/test_backup_restore.py`):
  funds a uniquely-valued user, backs up, restores into a throwaway
  database, and confirms the exact balance and the full cards pool
  survived intact — real `pg_dump`/`pg_restore` binaries, not mocked. See
  `DECISIONS.md` for why "performed in the last 30 days" (spec section
  14) can't literally be claimed without a real production deployment,
  and what's provable instead.

**Point-in-time recovery (PITR) — spec section 9.2**: `pg_dump`/`pg_restore`
above is a *logical* backup — portable, but architecturally unable to
replay forward to an arbitrary point in time between two backups. Spec
section 9.2 explicitly asks for PITR, which needs a *physical* base backup
plus continuously archived WAL:
- `deploy/docker-compose.yml`/`docker-compose.prod.yml`'s `postgres`
  service has `archive_mode=on`, archiving every completed WAL segment into
  a bind-mounted `backups/wal_archive/`.
- **One-time setup, before the stack's first start** (mirrors
  `deploy/.env` — Docker auto-creates a missing bind-mount source as
  `root:root`, which the containerized Postgres process can never write
  into, and a plain host user can't fix after the fact):
  ```
  mkdir -p backups/wal_archive && chmod 777 backups/wal_archive
  ```
- **`deploy/basebackup.sh`** — a real `pg_basebackup`, written under
  `backups/basebackups/<timestamp>/base.tar`.
- **`deploy/restore_pitr.sh <base.tar> <target_time> <host_port>`** —
  replays the archived WAL forward to an exact target timestamp in a
  genuinely separate, throwaway `docker run` container (never the live
  `postgres` service), and leaves it running on `<host_port>` for the
  caller to query.
- **`deploy/prune_wal_archive.sh [days]`** — the spec's 30-day retention
  (default 30), deleting archived WAL segments and base backups past the
  window.
- Verified with a real drill (`tests/integration/test_backup_restore.py`,
  `test_wal_archiving_supports_point_in_time_recovery`): funds a row,
  takes a base backup, funds a second row, then restores to the timestamp
  between the two — the recovered instance has the first row and not the
  second. See `DECISIONS.md` for three real gotchas this drill's own first
  draft ran into (archived files need to be readable by a differently-owned
  restoring container; the target timestamp must be captured *after* the
  base backup, not before; and why the test issues every SQL statement and
  every `pg_switch_wal()` call as its own round trip). Same "performed in
  the last 30 days"/"run monthly" honesty split as the logical-backup drill
  above — the mechanism is proven for real right now, the literal
  production cadence isn't something this session can manufacture.

**Observability (spec section 10.4):**
- **`packages/core/metrics.py`** — real Prometheus metrics (not
  placeholders): `gateway_connections`, `engine_rooms_active`,
  `engine_calls_total`, `gateway_command_ack_seconds` (p50/p95/p99 via a
  Histogram), `engine_claim_validation_seconds`,
  `engine_rounds_voided_total`, `ledger_transactions_total{kind}`,
  `deposit_outcomes_total{outcome}` (deposit success rate),
  `payout_queue_depth`, `house_revenue_total` — wired into the real code
  paths that produce each signal (`services/gateway/connection.py`,
  `services/engine/round_engine.py` and `refunds.py`,
  `packages/core/ledger.py`, `services/payments/deposits.py`).
- **`/metrics`** on every service with an HTTP surface (gateway, admin,
  payments, bot), unauthenticated like the existing `/healthz` routes —
  except admin's, which a `/code-review` pass caught bypassing the IP
  allowlist every other admin route goes through (a real gap: it was
  leaking live house revenue and deposit/payout figures to anyone on the
  network). Now gated the same way as everything else in
  `services/admin/app.py`. See `DECISIONS.md`.
- **`deploy/prometheus/prometheus.yml`** (scrape config) +
  **`alerts.yml`** (the spec's own five alert conditions, verbatim) + a
  `prometheus` service in `deploy/docker-compose.yml`, gated behind a
  `profiles: ["observability"]` so it doesn't start by default — see
  step 9 above.
- Verified with a real drill, not just "the config parses": a real
  gateway process, a real Prometheus container, `gateway_connections`
  watched moving `0 → 1 → 0` across real scrapes as a real WebSocket
  client connected and disconnected, queried through Prometheus's own
  API. All five alert rules confirmed loaded with `health: ok` against
  the real metric names. See `DECISIONS.md` for the two interpretation
  calls made explicit ("call-to-ack", "deposit success rate").
- **`ledger_reconciliation_mismatch_count`** — the one alert of the five
  that needed a batch job (`reconcile_job.py`, no long-running `/metrics`
  endpoint of its own) to reach Prometheus at all: pushed to a real
  Pushgateway (`deploy/docker-compose.yml`'s `pushgateway` service) when
  `PUSHGATEWAY_URL` is set, which Prometheus then scrapes. Verified
  against the actual `prom/pushgateway` binary, not just an automated
  test double — see `DECISIONS.md`.
- **Grafana** (`deploy/grafana/`) — a real dashboard, ten panels, one per
  metric above, provisioned as code (datasource + dashboard JSON both
  loaded from files on container startup, never clicked through by hand).
  A `grafana` service in `deploy/docker-compose.yml`, same
  `profiles: ["observability"]` gating. Verified by querying the exact
  PromQL expression the "Concurrent connections" panel uses through
  Grafana's own datasource proxy API and watching it move `0 → 1 → 0`
  across a real WebSocket connect/disconnect — not a hand-typed query
  against Prometheus directly, the literal thing the panel itself runs.
  See `DECISIONS.md`.
- **OpenTelemetry traces** (`packages/core/tracing.py`) — closes spec
  10.4's last item: "deposit and payout paths end to end." Real spans at
  the money-moving choke points — `deposit.create_intent` (with a nested
  `deposit.provider_checkout` child), `deposit.apply_confirmed_status`
  (outcome as an attribute), `withdrawal.request` (status as an
  attribute), `payout.dispatch` (with a nested `payout.provider_call`
  child) — wrapping whole function bodies, so OpenTelemetry's own
  automatic exception recording covers every rejection path too, not just
  success. Opt-in via `OTEL_EXPORTER_ENDPOINT`, same pattern as
  `PUSHGATEWAY_URL`; a `jaeger` service in `deploy/docker-compose.yml`,
  same profile gating. Verified against the real Jaeger binary: ran a
  real deposit, withdrawal, and payout dispatch, then confirmed every
  span landed with the correct name, nesting, and attributes via Jaeger's
  own API. See `DECISIONS.md` — including a genuine (non-regression)
  finding about shared-host load-test contention surfaced by this pass's
  own clean-slate rebuild.

Spec section 10.4 (Observability) is now fully addressed: metrics, alerts,
dashboards, and traces, every one verified with a real drill against the
real binary, not just config review.

**Deposit rate limiting (spec section 9.2):** the last of the four
listed rate limits (`claim` 5/round, `take_card` 10/min, `deposit`
5/hour, WS messages 30/s) that wasn't actually enforced anywhere —
confirmed by grepping every deposit call site and finding no check on
either the bot's `/deposit` command or the Mini App's `/api/deposit`
route. `packages/core/rate_limit.py` (moved from `services/gateway/`,
since it's no longer gateway-specific) gained a `DEPOSIT` bucket; the
check lives inside `deposits.create_deposit_intent()` itself — one
choke point both callers go through, not duplicated logic that could
drift out of sync. Verified through both real call paths: a real
aiogram dispatch test (`test_bot_handlers.py`) and a real HTTP test
(`test_gateway_rest.py`), each driving 5 successful deposits then
confirming a real 6th is rejected. See `DECISIONS.md` — including a
debugging detour where the first test draft asserted the wrong outcome
because of a wrong assumption about the shared test environment's Chapa
credentials, caught by actually introspecting the failure rather than
loosening the assertion.

**`rate_limit.py` itself had zero test coverage** before
`tests/integration/test_rate_limit.py` — no existing test anywhere sent
enough rapid requests to trip any bucket, so the module's actual
behavior was never directly verified. Covers capacity/rejection,
time-based refill, independent buckets per key and per scope, and —
the real point of the Lua script — 50 genuinely concurrent requests
against a capacity-10 bucket letting through exactly 10, never more.
See `DECISIONS.md`.

**Two more zero-coverage modules found the same way** (grepping every
source file against every test file): `packages/core/logging.py`'s
redaction processor (spec section 9.2's "logs must never contain full
[phone] numbers or `initData` strings" — `tests/unit/test_logging.py`
captures real stdout and parses the real JSON a log call produces, not a
mock) and `services/bot/keyboards.py` (pure keyboard builders —
`registration_keyboard`'s share button actually requesting
`request_contact`, and `main_menu_keyboard`'s Play button correctly
omitting `web_app` when `MINIAPP_URL` is empty, per
`tests/unit/test_keyboards.py`). See `DECISIONS.md`.

**Phone numbers encrypted at rest (spec section 9.2):**
`packages/core/phone_crypto.py` stores every phone number as two derived
values instead of plaintext — AES-256-GCM ciphertext
(`encrypt_phone`/`decrypt_phone`) for confidentiality, plus a
deterministic HMAC-SHA256 "blind index" (`phone_lookup_hash`) that alone
carries the UNIQUE constraint and exact-match lookups a random-nonce
ciphertext can't support in SQL. Both keys derive from one required
`PHONE_ENCRYPTION_KEY` via HKDF. A real product tradeoff came with this:
encryption breaks the admin console's substring phone search
structurally, and that choice (exact-match only, vs. keeping a plaintext
last-4-digits fragment, vs. not encrypting) was put to the user rather
than picked unilaterally — exact-match won. Migration
`1d14ec5fac7d_phone_encryption.py` backfills every existing row through
the app's own real encryption functions and was verified both directions
(upgrade and downgrade) against real seeded data, not just "the DDL
runs." See `DECISIONS.md`.

**Load and chaos testing (spec section 10.3):**
- **A real money-safety bug found and fixed by this phase's own load
  test, not by review:** `RoundEngine.join()`'s idle-room bootstrap had no
  synchronization — a burst of players hitting a freshly-idle room at once
  could all try to start the round simultaneously, each attempting to
  `INSERT` the same next round number and crashing with a
  `UniqueViolationError`. Fixed with a dedicated lock
  (`_round_start_lock`) around a double-checked idle-status read. Found
  by `tests/integration/test_load_rush.py`'s 1,000-concurrent-join
  scenario, which now passes reliably: exactly 100 winners across 100
  contested cards, zero double-allocated seats, ~2-4s elapsed.
- **`tests/integration/test_load_multiroom.py`** — fan-out at 100 rooms ×
  10 sockets (1,000 total), p99 ~150-205ms across repeated runs, comfortably
  inside the spec's 300ms budget.
- **`tests/integration/test_chaos_engine_crash.py`** — 80 real concurrent
  players staked in a room, the engine killed mid-round with no graceful
  shutdown; every single player refunded to the exact centavo on recovery.
- **`tests/integration/test_chaos_redis_restart.py`** — the actual Redis
  container restarted mid-round (not simulated): the engine genuinely
  crashes rather than silently reconnecting, and a fresh worker against
  the recovered instance correctly voids and refunds every player. Proves
  `packages/core/redis_conn.py`'s documented promise ("if Redis is wiped,
  the platform must recover fully from Postgres") against a real outage.
  Marked `chaos_infra`, not `load` — restarting a real service breaks
  every session-scoped fixture built on it for the rest of the pytest
  process, a real cross-test pollution bug this session found and fixed by
  giving it its own excluded marker; always run alone (`pytest -m
  chaos_infra`).
- **`tests/integration/test_chaos_gateway_kill.py`** — spec 10.3's "Kill a
  Gateway pod... clients reconnect within 5s with correct state," run for
  real: two genuinely separate `services/gateway/app.py` OS processes (not
  the in-process `uvicorn.Server` the other gateway tests share the test's
  own event loop with, which can't be sent a real kill signal), one holding
  a batch of real authenticated WebSocket connections, `SIGKILL`'d with no
  graceful shutdown. Every reconnecting client lands on the second,
  independently-started process — zero shared memory with the first — and
  gets the correct room state straight from Postgres, proving the
  statelessness `services/gateway/app.py`'s own docstring already claims,
  not just asserting it. Real, honestly-scaled numbers, not the spec's
  literal 8,000 sockets — see `DECISIONS.md` for the exact scale chosen and
  why (dominated by fixed per-run overhead on this shared host, not socket
  count, so more sockets bought no extra confidence, only less margin under
  the 5s budget).

**Honest gap, not hidden:** spec section 10.3 asks for 10,000 concurrent
sockets across 200 rooms on a 30-minute soak. This sandbox is a single
4-core/~8GB dev machine where the load generator and the system under test
share the same process and CPU — real measurements above 1,500 sockets
showed p99 latency exceeding the 300ms budget, with real run-to-run
variance confirming genuine resource contention rather than a stable
number. The load/chaos tests here are kept at the scale this environment
can reliably prove (~1,000-1,500 sockets), reported as real numbers rather
than either skipped or asserted against a target this box can't sustain.
See `DECISIONS.md` for the full progression of measurements taken before
settling here.

Covered end to end by `tests/`, including the tests that actually matter: a
35-player round settling to the cent (700 pot → 560 derash → 140 house), two
simultaneous claims splitting a derash evenly, a killed engine mid-round
recovering to a full refund on the next worker startup, Postgres itself (not
Python) rejecting an unbalanced ledger transaction, a full auth→join→stake→
round-settle flow over a real WebSocket, 1,000 real concurrent WebSocket
connections in one room receiving a call in ~130–190ms on a good run (spec
budget: 300ms — see `DECISIONS.md` for why this one occasionally reads
higher on a busy shared host), a contact-mismatch registration rejected
end to end through a real aiogram Dispatcher, a duplicate Telegram
`update_id` processed exactly once, and a real Chromium browser playing an
actual round against the real backend (auth → fund → join → take a card →
watch live calls render → confirm the stake actually happened in the
ledger) — which is how three real bugs invisible to every other test got
found and fixed, and an admin console's fairness route sent over real HTTP
against a real `uvicorn` server, RBAC denying/allowing per role, session
logout actually invalidating a token, and the audit log's immutability
trigger firing on a direct `UPDATE`/`DELETE` attempt — which is how a real
Phase 2 gap (the provably-fair `server_seed` was never durably persisted)
got found and fixed, and the exact four scenarios spec Prompt 7 calls out
by name for deposits: the same webhook delivered 100 times concurrently
credits exactly once, an invalid signature is rejected before the database
is even touched, a webhook arriving after a successful poll is a no-op,
and a mismatched amount does not credit -- plus a real HTTP POST to the
payments webhook route crediting the ledger and a live-connected
WebSocket actually receiving the resulting `balance_update` push, no
mocking anywhere in that chain, and for withdrawals: a real
`RoundEngine.join()` failing with `insufficient_funds` right after a
withdrawal locks the same balance, a genuinely concurrent
`asyncio.gather` of a stake and a withdrawal against the same money
proving at most one ever succeeds, a simulated crashed payout worker
whose redelivered job still settles to the ledger exactly once even
though the (idempotent-on-`our_ref`) provider gets called again, and a
rejected payout returning the exact amount to `user_cash`; and for
responsible gaming: a self-excluded or currently-cooling-off user's real
`RoundEngine.join()` call failing with the correct reason, a cool-off
that lifts itself the instant its timestamp passes with no code path
anywhere touching it, a loss cap computed from real stake/payout ledger
entries blocking a stake that would exceed it, a limit increase proven
to still show the old cap 23 hours and 59 minutes in and the new one 24
hours in, a self-excluded phone number structurally unable to register a
second account, and the marketing-audience query proven to exclude
exactly the users spec section 12 says it must; see
`DECISIONS.md`. `mypy --strict` is clean across the
whole codebase.

**Enterprise SMS Control Plane (`services/sms/`, `packages/core/sms/`,
`web/sms/`) — a genuinely separate product, not a Bingo feature:** built
in response to a CTO-level directive for a full multi-tenant SMS
operating system; DECISIONS.md (2026-09-07) records the scoping call —
one real, fully-tested vertical slice through the whole stack rather than
shallow coverage of an effectively multi-quarter enterprise scope. What's
real: a tenant-scoped domain (contacts, suppressions, templates,
campaigns, messages, delivery attempts, delivery nodes); a campaign
lifecycle state machine (`draft → validating → ready/failed → scheduled →
running → paused/cancelled → completed/completed_with_errors`) with every
transition validated and recorded in `sms_campaign_events`; a pull-based
generic delivery-node protocol (heartbeat/fetch-job/start/report-result)
authenticated by a per-node hashed credential (never a shared static
token), with MacroDroid as only one interchangeable caller of it, exactly
as the directive required; atomic message claiming via `FOR UPDATE SKIP
LOCKED` in real priority order; retry classification with a
bounded-attempts-then-dead-letter path; a timeout-based reconciliation
sweep that moves a silently-non-reporting in-flight message to an
explicit `unknown` state rather than guessing; a real compliance
suppression list enforced both at campaign-validation time and again at
audience resolution; and RBAC (`sms:*` permissions in the same
`services/admin/rbac.py`) with a narrower `sms:campaigns:approve` gate on
the one action that actually sends real messages. Authentication, RBAC,
and the audit trail are the *same* `admin_users`/`PERMISSIONS`/
`admin_audit_log` the Bingo admin console uses — no second login system.
**Phase 2 (same day)** extended this with real node-lifecycle maturity
(`maintenance` as a stored, admin-settable status; `degraded`/`offline`
computed at read time from health/heartbeat signals, never stored),
per-node capacity enforcement (`max_concurrent_jobs`, checked against a
live in-flight count, never self-reported), node-group campaign
eligibility (`required_fleet_group`), and cross-campaign fair-share
claim ordering so one huge campaign cannot starve a smaller one queued
alongside it — plus routing-decision forensics (`routing_snapshot` on
every claim) and two explicit regression-protection tests (cross-tenant
isolation, real audit-log content). See `docs/SMS_CONTROL_PLANE.md`'s
own "Phase 2" section for why this reads as eligibility+fairness rather
than a push-style routing engine: the node protocol is pull-based, and
that's what genuinely translates onto it.

Verified with 42 real-Postgres domain tests (`test_sms_core.py`), 13
real-HTTP tests including a full campaign-to-delivery flow and an RBAC
boundary/node-ownership/suppression-enforcement/capacity/eligibility set
(`test_sms_app.py`), and a real-Chromium click-through of the console
frontend (`test_sms_console_e2e.py`); `mypy --strict` clean across the
whole repo. Explicitly deferred, named in DECISIONS.md rather than
faked: SMPP/carrier provider adapters, fleet scale validated beyond a
handful of nodes, a pluggable multi-strategy routing engine (this
slice's eligibility+fairness is real, not a stub, but it's one concrete
policy, not an interface with multiple implementations), automatic
inbound STOP-keyword suppression, a configurable N-person approval
workflow, billing/quotas, webhooks/domain events, the real-time
ops-center dashboard maturity, load/chaos testing at enterprise scale,
node-protocol-version-gated rolling upgrades, and tenant self-service
provisioning (one tenant is seeded; there is no second real tenant to
provision for yet).
