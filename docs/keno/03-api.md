# Keno — API Reference

Two HTTP surfaces: the public `gateway` service (`/api/keno/*`, player
-facing, Telegram-auth'd) and the internal `admin` service (`/keno/*`,
RBAC-gated). Realtime events (websocket) are documented separately in
`04-realtime-events.md`.

## Player API (`gateway`, `/api/keno/*`)

Every route requires the standard `Authorization` header used
platform-wide; `_authenticated_user_id()` resolves it the same way as
every existing Bingo route.

### `GET /api/keno/state`
Current-round summary for the Game Center screen: round id, status,
timestamps, `server_seed_hash`, `reveal_index`, any numbers already
drawn, the offered pick range and stake ladder (from the round's own
**pinned tier**, never the live "current" tier — an admin editing the
tier mid-round can't retroactively change what a round already offered),
`max_win_per_ticket`, the live jackpot pool balance, round timing, and
the full paytable grid for the active pick range/profile. Returns
`503 keno_not_configured` only if Keno has genuinely never been
configured (no round has ever been created).

### `POST /api/keno/tickets`
Place a ticket.

```jsonc
// request
{ "picks": [3, 17, 42], "stake": "50.00", "idempotency_key": "..." }
// response
{ "id": 1290, "round_id": 1057, "picks": [3, 17, 42], "stake": "50.00", "status": "pending" }
```

Rate-limited on two independent buckets (per-user, per-IP — either
tripping is enough to reject with `429 rate_limited`). A rejected
ticket returns `422` with `detail` set to the specific
`TicketRejected` subclass's own error code (e.g. `RoundNotAcceptingBets`,
`RoundCapacityReached`, `UserRoundShareExceeded`, `InsufficientBalance`,
`KenoDisabled`, `ResponsibleGamingBlock`). A replayed
`idempotency_key` returns the original ticket unchanged, never
re-validates or re-charges.

### `POST /api/keno/autoplay`
Start an autoplay/multi-race session (one mechanism, see
`02-data-model.md`).

```jsonc
// request
{
  "picks": [3, 17, 42], "stake": "50.00",
  "rounds_total": 10,               // null = run until stopped/threshold
  "stop_on_win_amount": "500.00",   // optional, either or both
  "stop_on_loss_amount": "200.00"
}
```

Returns the created session (see `_autoplay_session_to_dict` shape
below). `422` with the specific `AutoplayError` subclass's code on
rejection (e.g. a session already active for this user).

### `DELETE /api/keno/autoplay`
Stops the caller's active session, if any. `{"stopped": true|false}`.

### `GET /api/keno/autoplay`
Returns the caller's active session, or `null`.

Session shape (used by all three autoplay routes):
```jsonc
{
  "id": 1, "picks": [3, 17, 42], "stake": "50.00",
  "rounds_total": 10, "rounds_placed": 4,
  "stop_on_win_amount": "500.00", "stop_on_loss_amount": null,
  "net_position": "-120.00", "status": "active",
  "stop_reason": null, "last_round_id": 1057
}
```

### `GET /api/keno/tickets`
The caller's own ticket history. Query params: `round_id` (optional
filter), `before_id` (cursor pagination), `limit` (default 20).

### `GET /api/keno/stats`
The caller's own lifetime Keno statistics (total staked, total won,
etc.) via `keno_queries.my_statistics()`.

### `GET /api/keno/rounds/recent`
The last `limit` (default 20) completed rounds' results.

### `GET /api/keno/rounds/hot-cold`
Number frequency over the last `lookback_rounds` (default 50) rounds —
purely informational/entertainment, has no bearing on the draw itself
(the draw is CSPRNG-derived per round; past frequency predicts nothing).

### `GET /api/keno/rounds/{round_id}`
Round result **and the independent-verification payload** in one
response — this doubles as the spec's required "public verification
endpoint" (see `06-fairness-and-verification.md`). Returns `404
round_not_found` for an unknown id.

```jsonc
{
  "id": 1057, "status": "completed",
  "server_seed_hash": "…sha256…",
  "server_seed": "…hex, null until the round is terminal…",
  "public_seed": "1057:1758610199:…",
  "drawn_numbers": [3, 12, 17, …],   // null until terminal
  "verified": true,                    // server's own re-derivation check; null until terminal
  "total_stake": "15000.00", "total_payout": "12100.00",
  "ticket_count": 300, "jackpot_hit": false,
  "betting_closed_at": "…", "completed_at": "…"
}
```

`server_seed` and `drawn_numbers` are gated on the round being
terminal — both are written to the database well before that point
(the seed at round creation, the draw the instant betting closes), so
gating this response only on "is the column non-null" would leak the
still-live secret seed, or the full draw, before betting has actually
closed for everyone else. `server_seed_hash` and `public_seed` carry no
such risk and are never gated.

## Admin API (`admin` service, `/keno/*`)

Every route requires an authenticated admin session and one of two
Keno-specific RBAC permissions: **`keno:view`** (read-only),
**`keno:manage`** (ops — pure-computation tools, no persistence),
**`keno:configure`** (superadmin-only — anything that changes live
config, money, or tier state). Every mutating route also requires a
non-empty `reason` string, written to `admin_audit_log` alongside the
action.

| Route | Permission | Purpose |
|---|---|---|
| `GET /keno/dashboard` | `keno:view` | Current round, reserve balance vs. player liability (shown **separately** — conflating them is a named operator risk), current tier, jackpot pool. |
| `GET /keno/configs` | `keno:view` | List all config versions. |
| `POST /keno/configs` | `keno:configure` | Insert a new config version (round timing, pick/stake limits, RTP guardrail, `keno_enabled`). |
| `POST /keno/kill-switch` | `keno:configure` | The single required "stop everything" toggle — instantly stops new rounds, blocks new tickets. Same tier as `rooms:emergency_stop`. |
| `GET /keno/paytables` | `keno:view` | List paytables, optionally filtered by `pick_count`. |
| `POST /keno/paytables` | `keno:configure` | Insert a new paytable version. Server-side RTP guardrail enforced (`packages.core.keno.validate_paytable_rtp`) — never bypassable via this endpoint alone. |
| `POST /keno/paytables/preview` | `keno:manage` | Pure computation, nothing persisted — live RTP/hit-frequency/volatility preview while designing a table, safe to call on every keystroke. |
| `GET /keno/tiers` | `keno:view` | List all risk-tier versions. |
| `POST /keno/tiers` | `keno:configure` | Insert a new tier version. |
| `POST /keno/tiers/set-current` | `keno:configure` | Admin override of the current tier (also writes `keno_tier_changes`, `trigger='admin_override'`). |
| `POST /keno/reserve/deposit` | `keno:configure` | Move real money `house_float → keno_reserve`. |
| `POST /keno/reserve/withdraw` | `keno:configure` | Move real money `keno_reserve → house_float`. `409` (not `422`) if it would breach `reserve_withdrawal_floor` — a well-formed request correctly refused by a business rule, audited either way. |
| `POST /keno/risk-of-ruin` | `keno:manage` | Pure Monte Carlo computation, nothing persisted — see `07-economics-and-bankroll.md`. |

Both money-moving reserve routes and the paytable/tier routes share one
request shape pattern: a typed body plus a required `reason: str`,
`Decimal` amounts passed as strings (never floats, to avoid any binary
floating-point rounding anywhere near real money).
