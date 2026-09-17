# Keno — Discovery Report

Status: read-only investigation, no feature code written yet (Part 0 gate).
Everything below was verified against the actual repository and a live
migrated dev database on 2026-09-17, not reconstructed from memory.

**Path correction**: the build spec refers to `~/jo-bingo/deploy`. That path
does not exist on this machine. The live "Arada Bingo" repository (matching
the README, `arada.fun` domain, and the exact commit history described) is
`/home/prophet/game` (git remote `github.com/Nebyudejenie/igame.git`), whose
own `deploy/` directory is the real deploy directory this report uses. A
second, independent clone of the same codebase (`git remote
github.com/Nebyudejenie/game.git`, same latest commit `5180690`) exists at
`~/AradaBingo` with local dev artifacts (`.venv`, `backups/`, test caches)
already populated — plausibly what "`~/jo-bingo`" was misremembered from.
This work proceeds entirely inside `/home/prophet/game`, which the harness
sandboxes me to. If `~/AradaBingo` is actually the operator's preferred
working checkout, say so and I'll re-point.

---

## A. System map

**Request path (production, per `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md`
and `DECISIONS.md` 2026-09-14):**

```
Internet → Cloudflare → Cloudflare Tunnel (host-level systemd cloudflared,
  NOT a container — deploy/docker-compose.prod.yml's own cloudflared
  service is explicitly stale/unused, see its in-file comment) → Traefik
  (a separate, shared stack on the same host, outside this repo,
  reconstructed reference at deploy/traefik/jobingo-dynamic.yml.example)
  → per-hostname routing to a Docker Compose service by name:
     arada.fun          → gateway:8000   (Mini App + player REST + WS)
     admin.arada.fun     → admin:8001
     payments.arada.fun  → payments:8002 (Chapa webhook, Telebirr ingest, Agent Portal)
     agent.arada.fun     → payments:8002 (same container, Agent Portal static bundle)
     finance.arada.fun   → (per README, part of the live set; not independently
                            re-verified this pass — flagged, not assumed)
     sms.arada.fun       → NOT YET LIVE — DNS record doesn't exist yet per
                            DECISIONS.md 2026-09-07 entry
```

Production host: a private-LAN machine, no public IP
(`deploy/systemd/README.md` records the real deploy path as
`cosmic@192.168.1.173:/home/cosmic/game`). **Not reachable from this
sandboxed environment** — no SSH, no route to that private IP. This bounds
what Part 18 (deployment) and Part 17's load test can honestly claim from
here; flagged now so it isn't a surprise later.

**Services (`deploy/docker-compose.prod.yml`, verified by reading the file
directly):** one shared Docker image (root `Dockerfile`), eight app
processes each just a different `command:` against that same image, all
sharing one `DATABASE_URL`/`REDIS_URL` via the `x-app-env` YAML anchor:

| Service | Command | Port | Restart |
|---|---|---|---|
| `migrate` | `alembic upgrade head` | — | one-shot, others wait on `service_completed_successfully` |
| `gateway` | `uvicorn services.gateway.app:app` | 8000 | `unless-stopped` |
| `admin` | `uvicorn services.admin.app:app` | 8001 | `unless-stopped` |
| `payments` | `uvicorn services.payments.app:app` | 8002 | `unless-stopped` |
| `bot` | `python -m services.bot.app` | 8003 | `unless-stopped` |
| `engine-worker` | `python -m services.engine.worker` | 8004 (metrics only) | `unless-stopped` |
| `payout-worker` | `python -m services.payments.payout_worker` | 8005 (metrics only) | `unless-stopped` |
| `sms` | `uvicorn services.sms.app:app` | 8006 | `unless-stopped` |
| `simulated-players-worker` | `python -m services.engine.simulated_players_worker` | 8007 (metrics only) | `unless-stopped` |
| `reconcile-job` | `python -m packages.core.reconcile_job` | — | one-shot, `profiles: ["reconcile"]`, not in a plain `up -d` |

Every service depends on `postgres`/`redis` (`service_healthy`) and
`migrate` (`service_completed_successfully`) via the `x-app-depends-on`
anchor. **Next free metrics port for a new worker: 8008.**

Health checks: `postgres` via `pg_isready`, `redis` via `redis-cli ping`,
both 5s interval / 10 retries. The app services themselves have no explicit
Docker `healthcheck:` in this compose file — Traefik/uptime is inferred
from the port being open plus each service's own `/healthz` route
(confirmed present on gateway/admin/payments; bot and the two workers have
no HTTP surface besides `/metrics`).

**Internal-only enforcement today**: purely by not being tunnelled.
`engine-worker`/`payout-worker`/`simulated-players-worker` have no ingress
rule anywhere (confirmed: they're absent from the Traefik reconstruction
and the README's own subdomain table) — their ports are reachable only
inside the Compose network, scraped by Prometheus via
`host.docker.internal` in dev or a real Prometheus target in prod. There is
no separate firewall/network-policy layer in this repo; isolation is
"nothing routes to it."

## B. Code inventory

Directory layout matches the README's own stated convention, which the
actual code follows:

```
packages/core/     framework-free domain logic (ledger, bingo, config,
                    redis, telegram_auth, rate_limit, responsible_gaming,
                    notifications, bonuses, referrals, campaigns, phone,
                    phone_crypto, logging, metrics, tracing, device_auth,
                    db_pool, reconcile_job) + packages/core/sms/ (SMS
                    Control Plane domain, unrelated to Keno)
services/engine/    game engine workers (round_engine.py, room_lock.py,
                    settlement.py, refunds.py, recovery.py, worker.py,
                    commands.py, simulated_players_worker.py)
services/gateway/   FastAPI WS + REST edge (app.py, connection.py,
                    fanout.py, queries.py)
services/bot/       aiogram 3 webhook bot
services/payments/  Chapa + Telebirr-SMS-evidence + manual rails
services/admin/     RBAC-gated ops console API
services/sms/       separate SMS Control Plane product
web/miniapp/        player Mini App (vanilla JS, no build step)
web/admin/          admin console frontend (vanilla JS, 20 screens)
migrations/         Alembic, raw SQL only (no ORM, no autogenerate)
tests/unit/         pure-function tests
tests/integration/  real Postgres + Redis
```

**Domain vs. transport vs. persistence**: domain logic is plain async
functions taking an `asyncpg` connection/pool and returning
dataclasses/dicts — no ORM anywhere, no repository/DI framework. A typical
module shape (`packages/core/ledger.py`, `packages/core/responsible_gaming.py`,
`services/payments/withdrawals.py`) is: frozen dataclasses for return
types, a handful of typed exceptions for rejection reasons (never bare
`ValueError`/HTTP exceptions at the domain layer), and functions that take
either an `asyncpg.Connection` or a pool depending on whether they need
their own transaction. FastAPI route handlers (`services/*/app.py`) are
thin — they parse/validate input, call one domain function, map its typed
exceptions to HTTP status/error-code JSON, nothing else. No dependency
-injection container; `app.state` holds the pool/redis client, injected via
FastAPI `Depends()` closures reading `app.state`.

**Conventions confirmed by direct reading:**
- Errors: one exception base class per rejection domain
  (`DepositRejected`, `WithdrawalRejected`, `InvalidTransition`), each
  subtype mapped 1:1 to a typed client-facing error code — never a bare
  500 for an expected business rejection. This is exactly Part 3.3's
  requirement already; Keno should follow the identical shape.
- Validation: manual (no pydantic models on the domain layer; FastAPI
  request bodies use plain dicts/pydantic `BaseModel`s only at the route
  boundary). No DTO/mapper layer between HTTP and domain — the route
  handler itself does the translation.
- Logging: `packages/core/logging.py`'s `structlog` setup, `get_logger(__name__)`,
  structured fields, redaction of a fixed key set (phone/token/initData/etc).
- Config: one `pydantic_settings.BaseSettings` in `packages/core/config.py`,
  `@lru_cache`'d `get_settings()`, env-var driven, "empty = honestly
  degrade/refuse" discipline throughout.

**Bingo engine (`services/engine/round_engine.py`, 1328 lines) — already
read in full in an earlier pass this session:**
- Loop: `RoundEngine.run_forever()` — acquire `RoomLock` → proactively
  create and run one round after another (IDLE→LOBBY→RUNNING→SETTLING→IDLE)
  until the lock is lost or `stop_requested`.
- State lives: in-memory on the owning `RoundEngine` instance (authoritative
  for split-second decisions like the 50ms tie window) *and* persisted to
  Postgres at every real transition (`rounds.status`, `call_index`,
  `draw_order`) — Postgres is what `services/gateway/queries.py`'s
  `state_sync` reads, never the in-memory engine, so reconnection survives
  a dead engine.
- Restart recovery: `services/engine/recovery.py::recover_orphaned_rounds()`,
  run at worker startup **and** every 30s poll — any round that's not its
  room's latest `seq`, or is the latest but has no live Redis room-lock
  holder, gets voided+refunded via `services/engine/refunds.py`.
- Locking: `services/engine/room_lock.py` — Redis `SET NX EX` + a
  compare-and-refresh/compare-and-clear Lua script pair, single-owner
  election per room, 15s TTL / 5s refresh.
- Scheduling: one `asyncio` task per claimed room inside one
  `engine-worker` process; `services/engine/worker.py::run_active_rooms()`
  polls every 30s for newly-active rooms, capped at 50 new claims/poll.

**`engine-worker` / `payout-worker` queue technology**: Redis Streams with
real consumer groups (`XREADGROUP`), not a third-party queue library.
`services/engine/commands.py` is the gateway↔engine bridge: one stream per
room (`room:{id}:cmds`), request/reply correlated via a per-request Redis
pub/sub channel — no consumer group needed there since exactly one engine
ever reads a given room's stream. `services/payments/payout_worker.py` uses
a real named consumer group (`payout-workers` on stream `payouts`) with
`XAUTOCLAIM` for stale-message recovery — this is the pattern to copy for
any Keno worker that needs multi-replica-safe queueing (Keno's round
engine itself, single-owner per round like Bingo's, doesn't need a
consumer group — only a true work queue would).

Job shape / retries / dead-letter / idempotency: no generic job framework
exists — every queue consumer in this codebase is bespoke, but they share
one idiom: an idempotency key on the actual DB write (`ledger.post()`'s
`idempotency_key`, `ON CONFLICT DO NOTHING`), so a redelivered/retried job
is always safe to just re-run. `services/sms/` additionally has a genuine
retry-classification + dead-letter state machine
(`packages/core/sms/messages.py::report_result()`,
`RETRYABLE_ERROR_CLASSES`, `MAX_DELIVERY_ATTEMPTS`) — the closest existing
precedent in this repo to Keno's own settlement retry needs, though Keno's
settlement doesn't need retries in the SMS sense (it's a single DB
transaction, not a dispatch-to-an-external-node problem).

**Realtime**: FastAPI native `WebSocket`, no Socket.IO/similar. One socket
per browser tab, JSON text frames, `{"t": "...", ...}` envelope. Auth:
first frame must be `{"t":"auth","init_data":...}`, validated via
`packages/core/telegram_auth.py`. Room model: a connection subscribes to
Redis pub/sub channels `room:{id}` and `user:{id}` through one process
-wide `FanoutHub` (`services/gateway/fanout.py`) — a single `psubscribe`
fans out to every locally-connected socket via a bounded
(`maxsize=100`) per-connection `asyncio.Queue`, with defined
droppable-vs-never-droppable message types for backpressure. Event
envelope always includes enough for the client to update in place
(`{"t":"call","index":n,"number":n}`) — never a full-state dump except
`state_sync`. Reconnect: `services/gateway/queries.py::build_state_sync()`
reads Postgres fresh (never the live engine) so a reconnecting/late client
always gets correct current state regardless of what it missed.

## C. Data layer

**Migration tool**: Alembic, `migrations/alembic.ini`, but **every
migration body is raw SQL via `op.execute()`** — no autogenerate, no
SQLAlchemy ORM models anywhere in the codebase. Chain is strictly linear
(each file's `down_revision` points to exactly one parent; verified by
extracting every `revision`/`down_revision` pair — no branches). Current
head (verified against a live migrated dev DB this session):
**`a1c9e7f4d2b6`** (`a1c9e7f4d2b6_simulated_players.py`). Applied via
`alembic -c migrations/alembic.ini upgrade head`, run by the `migrate`
one-shot Compose service before any app container starts (see §A).

**Wallet/ledger model — double-entry, not a balance column**
(`packages/core/ledger.py`, `migrations/versions/81d041ff4513_ledger_foundation.py`,
verified by reading both directly):

```sql
accounts(id, user_id NULL-able, kind, currency, UNIQUE(user_id,kind,currency))
  kind IN ('user_cash','user_bonus','user_locked','house_revenue',
           'house_float','pot_escrow','provider_settlement','promo_expense')
ledger_transactions(id, kind, idempotency_key UNIQUE NOT NULL, round_id, payment_id,
                     created_by, memo, created_at)
  kind IN ('deposit','withdrawal','stake','payout','refund','commission',
           'bonus_grant','bonus_convert','adjustment')
ledger_entries(id, transaction_id FK, account_id FK, amount NUMERIC(18,2))
account_balances(account_id PK/FK, kind, balance NUMERIC(18,2),
                  CHECK (kind NOT IN user-balance-kinds OR balance >= 0))
```

Guarantees, all confirmed by reading the actual DDL and `ledger.post()`:
- A Postgres **deferred constraint trigger** (`trg_ledger_entries_balance`)
  rejects any transaction whose entries don't sum to exactly zero — enforced
  by the database, not just application code.
- `ledger.post(conn, kind, entries, idempotency_key, *, round_id=None,
  payment_id=None, memo=None, created_by="system")` is the **only** write
  path. `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, then
  re-`SELECT` on conflict — a retried call is a safe no-op returning the
  original result.
- Account rows are locked `FOR UPDATE` in **ascending `account_id` order**
  before any balance check — the deadlock-avoidance mechanism for any
  transaction touching more than one account.
- **`CRITICAL, VERIFIED CONSTRAINT`: `ledger_transactions.round_id` carries
  a real foreign key to `rounds(id)` (added in
  `88556f01eaf8_game_tables.py`), and `ledger_transactions.payment_id`
  carries a real foreign key to `payments(id)` (added in
  `cad1633f4597_payments.py`).** Both are Bingo-specific tables. **Keno
  ledger transactions must never populate either column** — doing so would
  either violate the FK outright or, worse, silently coincide with an
  unrelated Bingo round/payment id if the id happened to exist. This is a
  hard integration constraint, not a style choice — see §G.

**How a Bingo stake is deducted / a win credited** (for the pattern Keno's
own bet/settle must mirror): `RoundEngine.join()` posts a `"stake"`
ledger transaction (`user_cash -= stake`, `pot_escrow += stake`),
idempotency-keyed `stake-{round_id}-{user_id}-{card_no}`, **inside the same
Postgres transaction** as the `round_entries` INSERT and a
`pg_advisory_xact_lock(user_id)` (cross-room serialization for the
responsible-gaming loss-cap check). A win posts `"payout"`
(`pot_escrow -= amount`, `user_cash += amount`), idempotency-keyed by
round/user/card. Atomicity: one DB transaction per bet, one per
settlement batch — never two round trips with a window between them.

**Existing idempotency keys** (the vocabulary Keno's own keys should
follow): `stake-{round_id}-{user_id}-{card_no}`, `refund-{round_id}-{user_id}-{card_no}`,
`payout-settle-{our_ref}`, `payout-reverse-{our_ref}`, `admin-adjust-{admin_id}-{user_id}-{request_id}`,
`bonus-convert-{bonus_id}`, `referral-{referrer_id}-{user_id}`.

**Card/round tables** (`88556f01eaf8_game_tables.py`, Bingo-only, for
contrast with what Keno needs — confirmed no generic "game" abstraction
exists to extend):

```sql
rooms(id, code, stake, house_cut_bps, min_players, max_players,
      lobby_seconds, call_interval_ms, result_seconds,
      win_patterns jsonb, is_active)
rounds(id, room_id FK, seq, status CHECK(...), stake, house_cut_bps,
       player_count, pot, derash, server_seed bytea, server_seed_hash,
       client_seed, draw_order smallint[], call_index, lobby_deadline,
       started_at, ended_at, UNIQUE(room_id, seq))
round_entries(round_id, card_no, user_id, auto_mark, stake_txn_id FK,
              joined_at, PRIMARY KEY(round_id, card_no), UNIQUE(round_id, user_id))
round_winners(round_id, user_id, card_no, pattern, won_on_call, amount,
              payout_txn_id FK, PRIMARY KEY(round_id, user_id))
claim_attempts(id, round_id, user_id, call_index, valid, created_at)
```

There is no shared `games`/`rounds`-polymorphic table Keno could plug into
— it is entirely Bingo-shaped (card_no, win_patterns, claim semantics).
**Decision (§G): Keno gets its own fully independent `keno_*` table set,
zero shared FKs into Bingo's `rooms`/`rounds`/`round_entries`.** This is
also what Part 9 of the spec itself asks for.

**Redis**: `packages/core/redis_conn.py`'s `get_redis()` factory
(`SOCKET_TIMEOUT_SECONDS=10.0`, `MAX_CONNECTIONS=200`, one shared client
per process). Namespaces observed in the codebase: `room:lock:{room_id}`
(engine ownership), `room:{room_id}:cmds` (Redis Stream, gateway→engine
commands), `cmdreply:{request_id}` (pub/sub, one-shot reply), `room:{id}`
/`user:{id}` (pub/sub, fanout broadcast — not persisted, ephemeral),
`rl:{scope}:{id}` (rate-limit token buckets, `packages/core/rate_limit.py`),
`admin_session:{token}` / `agent_session:{token}` (opaque bearer sessions,
TTL-bound), `pending_referral:{telegram_id}` (1h TTL), `payouts` /
`bot_notifications` (Streams with consumer groups). Redis is documented and
verified (via a real chaos test, `test_chaos_redis_restart.py`) to hold
**zero source-of-truth financial state** — a wipe is survivable, Postgres
recovers everything. Any Keno Redis usage (round-owner lock, a
pre-warmed-next-round cache) must follow this same rule: cache/fan-out
only, reconstructible from Postgres.

## D. Identity & security

**Telegram `initData` validation**: `packages/core/telegram_auth.py::validate_init_data()`
— the documented, standard Telegram algorithm (HMAC-SHA256 double-hash,
`hmac.compare_digest`, 24h max age, 60s future-skew tolerance). This is
the **entire** player auth boundary; there is no session cookie. Called
from two places: `services/gateway/connection.py`'s WS handshake and
`services/gateway/app.py`'s `_authenticated_user_id()` REST helper (reads
`Authorization: tma <initData>`). **Keno must reuse this exact function,
not reimplement it** — both its WS entry and any new REST routes.

Session/JWT: there isn't one for players — `initData` itself (with its own
freshness window) is re-validated per connection/request; no refresh
token, no separate session store for the Mini App. Admin sessions
(`services/admin/auth.py`) are a completely different, separate mechanism
(bcrypt+TOTP, opaque Redis-backed bearer token, 8h TTL) — not relevant to
player-facing Keno, only to the Keno admin screens.

**Admin auth/permission model**: `services/admin/rbac.py`'s `PERMISSIONS`
dict, four roles (`support`/`finance`/`ops`/`superadmin`), checked via
`has_permission(role, permission)` on every route via a `require(permission)`
FastAPI dependency. **Keno reuses this exact dict and mechanism** — add new
`keno:*` permission strings following the existing breadth-tiering
convention (broad `*:view`, narrower `*:manage`, superadmin-only for the
single highest-leverage action — here, `keno:configure`/paytable-activation
and `keno:kill_switch`).

**Rate limiting**: `packages/core/rate_limit.py` — one atomic Lua
token-bucket script, `packages/core/rate_limit.py`'s predefined bucket
constants (`WS_MESSAGES`, `TAKE_CARD`, `CLAIM`, `DEPOSIT`, `TELEBIRR_REDEEM`,
`ADMIN_LOGIN`). Fails **closed** on Redis error, by explicit documented
design. Keno bet placement should add its own bucket constant here
(`KENO_TICKET`), same module, same pattern — not a new rate-limit
mechanism.

**Validation/CORS/CSP**: no CORS middleware found configured on any
service (the Mini App is same-origin, served by the same FastAPI process
it talks to — `services/gateway/app.py` mounts the static files and the
API in one process). No CSP header set anywhere in the codebase (a real,
pre-existing gap, not something to introduce scope-creep fixing for Keno).
Validation is manual, per-field, inside each domain function — no schema
-validation library beyond FastAPI's own request-body Pydantic models at
the route boundary.

## E. Mini App frontend

**Framework**: none. Vanilla JS ES modules, zero build step, zero bundler
— confirmed by reading `web/miniapp/index.html` and
`web/miniapp/js/app.v6.js` directly. `index.html` is one static file with
every current screen's markup already inline as
`<main class="screen" data-screen="...">` blocks (rooms, lobby, game,
result, wallet); `js/app.v6.js` (1981 lines) is a single monolithic module
that: imports `state.js` (plain pub/sub store, no framework), `ws.js`
(WebSocket client), `i18n.js`, `haptics.js`, `render/board.js`,
`render/card.js`, `voice.js`; defines `showScreen(name)` (a plain
`classList` toggle keyed by `data-screen`) as the entire "router"; and
drives the whole UI from `state_sync`/live WS events. Routing state is a
single `state.screen` string, no URL/history-based routing, no client-side
framework routing.

**Build**: none — files are served as-is by
`services/gateway/app.py`'s static mount (with a `_RevalidateStaticFiles`
class forcing `no-store` on `index.html`/`*.js` specifically because of
two prior real incidents with WebView caching stale JS). Any new Keno JS
files just need to exist under `web/miniapp/js/` and be referenced by a
`<script type="module">`/dynamic `import()` — no bundler config to touch.

**State**: `state.js` — a plain object + `Set<listener>` pub/sub, no
Redux/similar. `serverNow()` exposes `Date.now() + serverTimeOffsetMs` for
all countdown rendering, offset tracked from every WS message's numeric
`server_time` field.

**Styling**: hand-written CSS, no framework — `css/tokens.css` (design
tokens/custom properties), `css/base.css`, `css/screens.css`,
`css/wallet.css`, loaded as plain `<link>` tags in `index.html`. A new
`css/keno.css` following the same token variables is the natural,
zero-risk addition.

**i18n**: `js/i18n.js`, mirrors the bot's own `am` default / `en` complete
/ `om`+`ti` stub-fallback chain exactly, reading `locales/{lang}.json`.
Keno adds new keys to the same locale files (`am.json`/`en.json` fully,
`om.json`/`ti.json` untouched — they're deliberately minimal stubs
project-wide).

**Auth/realtime subscription**: `ws.js::connect(initData)` — one
WebSocket for the app's whole lifetime, auth handshake first frame,
transparent reconnect with full-jitter exponential backoff, re-`join`s
whatever room was active on reconnect. **Keno should reuse this exact
same WebSocket connection and client module** (subscribe to a new
`keno:{round_id}` server-side channel over the same socket) rather than
opening a second connection — this is both the natural integration point
and avoids a second auth handshake/connection-limit concern.

**Telegram SDK usage** (`window.Telegram.WebApp`, confirmed via grep):
`themeParams` read for dynamic theming, `expand()`, `HapticFeedback`
(wrapped in `haptics.js`), `openLink()` (deposit checkout URLs),
`enableClosingConfirmation()` is **not currently used anywhere** (a real
gap the spec's Part 13 asks Keno to add — for Keno this should be scoped
to "confirmation is pending" only, never a whole-session lock). No
`BackButton`/`MainButton` usage found in the current codebase — Bingo's UI
uses its own in-page buttons throughout; Keno should follow the same
convention for consistency rather than introducing the native
BackButton/MainButton pattern only for itself.

## F. Ops

**Prometheus naming**: flat `snake_case`, one label discipline
per-metric-group (documented at length in `packages/core/metrics.py`,
confirmed by reading it) — e.g. `ledger_transactions_total{kind}`,
`deposit_outcomes_total{outcome}`, `telegram_command_rate_limited_total{handler,limit_type}`.
All metrics live in **one shared module** (`packages/core/metrics.py`) on
the default Prometheus registry (one exception: `reconcile_job.py` uses
its own separate `CollectorRegistry` since it pushes to a Pushgateway and
must not drag along unrelated same-process metrics). **Keno metrics
(Part 16) belong in this same file**, added as new module-level
`Counter`/`Gauge`/`Histogram` objects, not a new registry.

**Existing dashboards/alerts**: `deploy/grafana/dashboards/` (provisioned
as code, one dashboard file), `deploy/prometheus/alerts.yml` (the spec's
five original alert rules). `deploy/prometheus/prometheus.yml`'s own
scrape target list is **already stale** — it lists gateway/admin/payments
/bot/engine-worker/payout-worker/pushgateway but is missing `sms:8006` and
`simulated-players-worker:8007` (a pre-existing gap, confirmed by reading
the file; not caused by this work, but Keno's own new worker port must not
repeat it — the discovery finding is flagged here so the eventual Keno
scrape-target addition doesn't get silently skipped the same way).

**Loki/log format**: no Loki config found in this repo (`deploy/` has
Prometheus+Grafana+Pushgateway+Jaeger, no Loki/Promtail). Structured JSON
logs to stdout via `packages/core/logging.py` is the only log
infrastructure that exists — "Loki log format/labels" in the spec's Part F
doesn't apply here; flagging rather than fabricating a Loki setup that
isn't in this codebase.

**Backup job scope**: `deploy/backup.sh` (`pg_dump -F custom`, whole
database) + `deploy/basebackup.sh`/WAL archiving for PITR
(`deploy/systemd/jobingo-*.timer`, confirmed by reading the unit files —
daily logical backup 02:00, weekly base backup Sunday 03:00, daily WAL
prune 04:00). This is **whole-database**, not per-table — new `keno_*`
tables are automatically covered by the existing backup job with zero
config changes (Part 18's backup-coverage requirement is satisfied
structurally, verified rather than assumed, and still worth a real
restore-drill check once Keno tables have real data in them).

## G. Integration plan

| Concern | Decision | Where |
|---|---|---|
| **Auth** | Reuse `packages/core/telegram_auth.py::validate_init_data()` verbatim for both the WS path (shared socket) and any new REST routes. No new auth mechanism. | no new file; called from new Keno route/WS handlers |
| **Wallet/ledger** | Reuse `packages/core/ledger.py::post()`/`get_or_create_account()`/`user_balance_snapshot()` verbatim. **New transaction kinds** added to the existing `TRANSACTION_KINDS` CHECK constraint: `keno_stake`, `keno_payout`, `keno_refund`, `keno_jackpot_contribution`, `keno_jackpot_payout`. **New account kinds** added to `ACCOUNT_KINDS`: `keno_reserve`, `keno_jackpot_pool` (system accounts, `user_id IS NULL`, mirroring `pot_escrow`/`house_revenue`). `round_id`/`payment_id` are **never populated** for Keno transactions (hard FK constraint, see §C) — linkage instead lives on `keno_tickets.ledger_txn_id`/`keno_rounds.*_txn_id`, exactly mirroring `round_entries.stake_txn_id`/`round_winners.payout_txn_id`. This is purely additive: widening a CHECK constraint's allowed value list is backward compatible, existing rows/queries are untouched. | new migration `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...` widening both CHECKs |
| **User model** | Reuse `users`/`accounts` as-is, zero schema change. A Keno ticket references `users.id` the same way `round_entries.user_id` does. | no change |
| **Admin auth** | Reuse `services/admin/rbac.py`'s `PERMISSIONS` dict + `services/admin/auth.py` session mechanism verbatim. New permission keys only (`keno:view`, `keno:manage`, `keno:configure`, `keno:kill_switch`, mirroring `payments:view`/`payments:configure`/`rooms:emergency_stop`'s breadth tiering). | edit `services/admin/rbac.py`'s dict (additive keys) |
| **Realtime transport** | Reuse the existing WS connection/module (`services/gateway/connection.py`, `web/miniapp/js/ws.js`) and `FanoutHub` (`services/gateway/fanout.py`). New Redis pub/sub channel `keno:{round_id}` alongside existing `room:{id}`/`user:{id}`; `FanoutHub` already generalizes over channel patterns via one `psubscribe`, so this is a subscribe-side addition, not a fanout-mechanism change. | `services/gateway/connection.py` (new message-type dispatch cases), `services/gateway/fanout.py` (extend the psubscribe pattern or add a second one) |
| **Worker framework** | New dedicated `services/engine/keno_engine_worker.py` process, same shape as `services/engine/worker.py`/`simulated_players_worker.py` (own asyncio loop, own `/metrics` on port **8008**, `restart: unless-stopped` in Compose) — **not** absorbed into the existing Bingo `engine-worker`. Justification: Bingo's `RoundEngine`/`RoomLock` are structurally per-*room*, one lock per room id, with Bingo-specific state (cards, claims); Keno rounds are global (one continuous cycle, not per-room), so forcing them through the same per-room lock abstraction would be a worse fit than a small parallel worker reusing the same *patterns* (Redis lock, Postgres `FOR UPDATE` truth, `recover_orphaned_rounds`-style crash recovery) without reusing the same *code path*. A crash in the Keno engine must never be able to touch a Bingo room the way a shared process could risk. | new file + new Compose service |
| **Migrations** | Alembic, raw SQL, linear chain off current head `a1c9e7f4d2b6`. All new tables/columns namespaced `keno_*`. Every migration ships a tested `downgrade()`. | `migrations/versions/` |
| **Metrics** | Added to the existing `packages/core/metrics.py` shared module/default registry, not a new file/registry. | edit `packages/core/metrics.py` (additive) |
| **Logging** | Reuse `packages/core/logging.py::get_logger()` verbatim; `round_id`→`keno_round_id` as the correlation field per log line (Part 16). | no change to the module itself |
| **Docker/Traefik** | New `keno-engine-worker` service in `deploy/docker-compose.prod.yml` (additive block, port 8008, same `x-app-env`/`x-app-depends-on` anchors). **No new public route** — Keno rides the existing `gateway:8000` (Mini App REST/WS) and `admin:8001` (admin screens) hostnames; nothing new needs Traefik/Cloudflare Tunnel changes at all. | `deploy/docker-compose.prod.yml`, `deploy/docker-compose.yml` (dev) |
| **i18n** | New keys appended to `web/miniapp/locales/am.json`/`en.json` (om/ti stay minimal stubs, matching existing project convention) and `services/bot/locales/` only if a bot-side Keno notification is added (Part 8.4's Telegram re-engagement pings reuse `packages/core/notifications.py`+`Notifier`, so yes, a handful of new i18n keys there too). | locale JSON files, additive keys only |

**Bingo files touched, and exactly why** (kept to the minimum the spec's
golden rule demands):
1. `migrations/versions/` — one new migration widening
   `ledger_transactions.kind`/`accounts.kind` CHECK constraints (additive
   value lists; existing rows and every existing query against these
   columns are unaffected — proven by a regression test asserting every
   existing `TRANSACTION_KINDS`/`ACCOUNT_KINDS` value plus row count is
   unchanged after migrating).
2. `packages/core/metrics.py` — additive new metric objects only.
3. `services/admin/rbac.py` — additive new permission dict keys only.
4. `services/gateway/connection.py` / `fanout.py` — additive new
   message-type branches / a second subscribe pattern; existing
   `room:*`/`user:*` dispatch is untouched code, proven by the existing
   Bingo WS test suite passing unchanged (baseline run captured before any
   Keno code lands — see below).
5. `web/miniapp/index.html` — additive new `<main data-screen="...">`
   blocks appended after the existing ones; zero edits to existing screen
   markup.
6. `deploy/docker-compose*.yml` — additive new service block.

Nothing else in the Bingo-specific surface (`services/engine/round_engine.py`,
`services/bot/*`, `services/payments/*`, existing `rooms`/`rounds`/`round_entries`
tables) needs to change at all.

**Backward compatibility proof plan**: before any Keno code is written, the
full existing test suite is run once as a captured baseline (in progress
as of this report — see "Verification run" below). After each phase, the
same suite is re-run; any prior-passing test that now fails is treated as
a Keno-caused regression and blocks progress until fixed. A new
`tests/integration/test_keno_does_not_affect_bingo.py` additionally pins a
handful of full Bingo flows (stake→settle, deposit webhook→credit, admin
dashboard numbers) end to end as an explicit regression contract, per
Part 10's requirement.

**Risk list:**

| Risk | Mitigation |
|---|---|
| A shared migration widening `ledger_transactions.kind`/`accounts.kind` CHECK constraints could be written wrong and reject a legitimate Bingo transaction kind | Migration includes its own test inserting one of every pre-existing kind before and after upgrade; `downgrade()` re-narrows and is tested round-trip |
| A bug in the new Keno WS message-dispatch branch inside `services/gateway/connection.py` crashes the shared connection handler, breaking Bingo's WS for the same connection | Every new branch wrapped in the same isolation discipline `_handle_command()` already uses for Bingo (catch, log, typed error reply — never let an exception propagate out of one message's handling); full existing WS test suite as regression gate |
| Two engines (a hypothetical second `keno-engine-worker` replica) both advance one Keno round | Copy the proven Bingo pattern exactly: Redis lock (fast path) + Postgres `SELECT...FOR UPDATE`/`WHERE state = expected` (source of truth) — same two-worker race test pattern as `test_chaos_engine_crash.py` |
| Production deployment / real 10k-socket load testing can't be verified from this sandbox (no route to the private production host) | Stated honestly in Part 17/18's reporting rather than fabricated; load tests run against this dev environment's own real Postgres/Redis at whatever scale it can sustain, numbers reported as what they are (dev-box ceiling, not production capacity) — same honesty precedent this codebase's own `DECISIONS.md` already sets for Bingo's own load tests |
| RNG/paytable bugs are uniquely catastrophic (real money, structurally impossible to "patch a live draw") | Pure-function, 100%-unit-tested domain layer before any engine/wallet wiring touches it (Part 21's own ordering); statistical tests (≥1M simulated draws) before any real-money path is enabled behind the kill switch |
| Scope: this specification is genuinely multi-week production work (RNG, engine, wallet, realtime, full UI, admin, economics simulator, load testing at scale, docs) | Delivered in the phased order Part 21 specifies, with a real, runnable, tested artifact at the end of each phase rather than a single unverifiable "done" claim at the very end — progress reported phase by phase, as instructed |

## H. Open decisions (defaults per Part 7, marked as such, all admin-changeable)

- **Launch tier / RTP / stakes / cycle time**: using Part 7.5's exact
  defaults (Tier 1, 82% target RTP, picks 1–5, 16x top multiplier, 10/20/50
  ETB stakes, 800 ETB max win/ticket, 3 tickets/user/round, 45s cycle,
  1.5% jackpot diversion). **Default, changeable in admin.**
- **Round exposure percentile methodology**: Part 7.3 specifies 99.9th
  percentile via Monte Carlo, recomputed when the paytable changes,
  cached. Implementation default: recompute on paytable save, cache in
  `keno_paytables.cached_exposure_99_9` — no operator input needed here,
  purely a computed value. **Not a genuine open question, noted for
  completeness.**
- **`finance.arada.fun`'s exact routing** — README lists it as live;
  this pass didn't independently re-verify it against the real Traefik
  config the way `docs/PRODUCTION_DOMAIN_AND_CLOUDFLARE.md` did for the
  other four. Irrelevant to Keno (no new subdomain needed either way) —
  noted, not blocking.
- **Genuinely operator-only**: whether/when to actually flip the Keno kill
  switch on in production, and the real prize-reserve deposit amount
  funding `keno_reserve` at launch (the spec's own "under 50,000 ETB"
  figure is business context, not a number this session can credibly post
  into the ledger on the operator's behalf without them confirming the
  real figure at the real moment of launch).

---

## Verification run (baseline, captured before any Keno code)

```
$ cd /home/prophet/game
$ python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.lock && .venv/bin/pip install -e . --no-deps
$ docker start jobingo-postgres-1 jobingo-redis-1   # pre-existing dev containers, were stopped
$ .venv/bin/alembic -c migrations/alembic.ini current
a1c9e7f4d2b6 (head)   # already at head, confirms repo/DB agree
$ .venv/bin/python -m pytest tests/ -q
```

**Actual result (2026-09-17, before any Keno code existed):**

```
2 failed, 1442 passed, 69 deselected in 832.27s (0:13:52)

FAILED tests/integration/test_admin_queries.py::test_retention_cohorts_places_a_backdated_signup_in_a_later_week_offset
FAILED tests/integration/test_backup_restore.py::test_wal_archiving_supports_point_in_time_recovery
```

Both failures are **pre-existing** — confirmed by the fact that no Keno
code existed anywhere in the working tree when this ran. `test_wal_archiving_supports_point_in_time_recovery`
fails with `recovered instance never became ready within 60s ... No such
container` — a real Docker-in-this-sandbox limitation around
`restore_pitr.sh`'s throwaway container spin-up, not an application bug.
`test_retention_cohorts_places_a_backdated_signup_in_a_later_week_offset`
is a pre-existing admin-reports test failure, also unrelated to anything
this work touches. **These two are the accepted baseline going forward**:
they're allowed to keep failing; any newly-failing test beyond these two
is treated as a real regression and blocks progress. Full log retained at
`/tmp/claude-1000/-home-prophet-game/ffbe6c15-4dea-47d0-aaef-8cdb9f62179b/tasks/b8uby90we.output`
for this session's duration.
