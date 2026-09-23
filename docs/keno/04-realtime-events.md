# Keno — Realtime Events

## Transport

One websocket per client (`GET /ws`, the same connection Bingo uses).
On authentication, every connection is **unconditionally** subscribed
to the Keno broadcast channel (`services/gateway/connection.py`) —
unlike Bingo rooms, which are only subscribed once a player actually
joins one, Keno has exactly one continuous round stream and the Game
Center/Keno screen needs to reflect it the instant a socket is up.

Two Redis pub/sub channels carry every Keno event:

- **`keno:live`** — public, round-lifecycle events. Published by
  `KenoRoundEngine` (`services/engine/keno_round_engine.py`), fanned out
  by the gateway's `ConnectionHub` to every subscribed connection.
- **`user:{user_id}`** — private, per-user outcomes. The same channel
  Bingo already uses for balance updates; a client's existing
  per-user subscription picks up Keno events with no separate
  subscription step.

Every event is a flat JSON object with a `"t"` field naming the event
type, published via `redis.publish(channel, json.dumps(payload,
default=str))` — Decimal/UUID values serialize as strings.

## Public events (`keno:live`)

| `t` | Payload (beyond `round_id`) | When |
|---|---|---|
| `keno.betting.open` | `server_time`, `server_seed_hash`, `betting_seconds`, `min_picks`, `max_picks`, `stake_options` | A new round opens for betting. `server_seed_hash` is the commit half of commit-reveal — published here, before a single ticket exists. |
| `keno.betting.closing` | `seconds_left` | Once, when the betting window drops to the closing-warning threshold — a UI cue to stop accepting new bets client-side before the server actually does. |
| `keno.betting.closed` | `ticket_count` | Betting closes; `public_seed` is now fixed (derived from the round id, close timestamp, and every accepted ticket id) but not sent in this event — clients fetch it via `GET /api/keno/rounds/{id}` once terminal, or via `GET /api/keno/state`. |
| `keno.draw.started` | — | The draw itself has been computed and persisted (one atomic operation) — reveal is about to begin. |
| `keno.number.drawn` | `index`, `number` | One ball at a time, paced over `draw_seconds` (`interval = draw_seconds / draw_count`) for the ball-by-ball animation. If a worker crashes mid-reveal, a recovering worker resumes broadcasting from the persisted `reveal_index` rather than restarting the sequence — a client that reconnects mid-draw should reconcile against `GET /api/keno/state`'s own `reveal_index`/`drawn_numbers`, not assume it saw every `keno.number.drawn` event from index 0. |
| `keno.draw.completed` | `drawn_numbers` (full array) | Every ball has been revealed. |
| `keno.round.completed` | `total_payout` | Settlement finished; the round is now `completed`. |
| `keno.round.completed` (failure variant) | `status: "failed"`, `reason` | A round couldn't be recovered (older than the 300s stuck-round threshold) — see `08-runbook.md` for the refund path this triggers. |

## Private events (`user:{user_id}`)

| `t` | Payload | When |
|---|---|---|
| `keno.ticket.settled` | `round_id`, `ticket_id`, `matches`, `payout`, `jackpot_payout` (string or `null`) | This specific ticket has been settled. Published once per ticket during settlement, immediately followed by the platform's existing balance-update publish (`ledger.publish_balance_update`) — a client should expect its balance to update right after, not necessarily in the same message. |

## Reconnection

Nothing here is a stream a client must consume gaplessly to stay
correct — every public event has a corresponding REST read
(`GET /api/keno/state` for the live round, `GET /api/keno/rounds/{id}`
for a terminal one) that reflects the exact same underlying database
state the events were derived from. A client that reconnects mid-round
should re-fetch `GET /api/keno/state` once, then resume consuming
`keno:live` — the same reconcile-then-stream pattern already used for
Bingo's own room state.

## Why there's no per-round channel

Bingo fans events out per-room (`room:{room_id}`) because many rooms
run concurrently and a client only cares about the one it joined. Keno
has exactly one round stream, ever — a per-round channel would mean
resubscribing on every single round boundary for zero benefit, so
`keno:live` is deliberately a single, permanent channel for the whole
game rather than mirroring Bingo's per-room shape.
