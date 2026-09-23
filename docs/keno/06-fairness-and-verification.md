# Keno — Fairness and Verification

## The guarantee

Every Keno draw is **CSPRNG-derived, seed-committed, persisted,
immutable, and independently verifiable by anyone** — including a
player, using nothing but data the API already publishes, with no
special access and no trust required in the server's own honesty at
draw time.

## Commit-reveal, step by step

1. **At round creation** (`_create_round()`), before a single ticket
   exists: `server_seed = generate_server_seed()` — 32 bytes of real
   CSPRNG entropy (`secrets.token_bytes`), kept secret. Its commitment,
   `server_seed_hash = sha256(server_seed)`, is published immediately
   in the `keno.betting.open` event and via `GET /api/keno/state`. The
   server has now committed to a specific seed **before it has seen a
   single bet** — it cannot pick a different seed later based on what
   players stake, even in principle, because the hash is already public.

2. **At betting close** (`_close_betting()`): `public_seed` is derived
   from data that *only exists at this exact moment* —
   `derive_public_seed(round_id, betting_closed_at_epoch,
   ticket_ids_hash)`, where `ticket_ids_hash` is a `sha256` over every
   accepted ticket's id, **sorted first** so the arrival order of bets
   (which the server could otherwise influence) can never change the
   resulting hash. This closes the one remaining lever a dishonest
   server might have: even knowing `server_seed` already, it still
   can't predict `public_seed` before betting genuinely closes, because
   the close timestamp and the full ticket set aren't fixed until then.

3. **The draw** (`derive_keno_draw(server_seed, public_seed, pool_size,
   draw_count)`): a deterministic HMAC-SHA256 counter-mode byte stream
   (`_ByteStream`, byte-for-byte identical construction to Bingo's own
   fairness-critical byte stream — proven by
   `tests/unit/test_keno.py::test_byte_stream_matches_bingo_byte_stream`,
   so the two can never silently drift apart despite being independent
   implementations). Unbiased selection via **rejection sampling**
   (`below()`) — never modulo, which would bias low values whenever the
   pool size doesn't evenly divide the byte range. The draw pops a
   uniformly-chosen number out of a shrinking 80-number pool, 20 times
   — duplicates are **structurally impossible** (a drawn number is
   removed from the pool the instant it's drawn), not merely avoided by
   a dedup step afterward.

4. **Persisted once, atomically, immutably.** `drawn_numbers` goes from
   `NULL` to its final value in one `UPDATE`, and a real database
   trigger (`trg_keno_rounds_drawn_numbers_immutable`) raises an
   exception on any attempt to change it afterward — enforced at the
   schema level, not just by application discipline.

5. **`server_seed` is revealed only after the round is terminal** — the
   verification payload (`GET /api/keno/rounds/{id}`, see `03-api.md`)
   gates `server_seed` and `drawn_numbers` on `status` being terminal,
   even though both columns are written to the database well before
   that point (the seed at creation, the draw the instant betting
   closes). A real bug this project's own spec-compliance audit caught:
   gating only on "is the column non-null" would have exposed the live
   secret seed — or the full draw, ahead of `keno.number.drawn`'s own
   paced reveal — for a round that hadn't finished yet.

## The independence guarantee

`derive_keno_draw`'s signature is exactly
`(server_seed, public_seed, pool_size, draw_count)` — there is **no
parameter through which ticket or selection data could reach it**.
This isn't just a design intention; it's enforced by a dedicated test
(`tests/unit/test_keno.py::test_derive_draw_signature_has_no_ticket_data`)
that fails the build outright if this function's signature ever grows
a ticket-shaped parameter. Match-counting (`count_matches`) is a
completely separate, later step that composes the already-fixed draw
with a ticket's picks — it has no way to feed back into what was drawn.

## Verifying a round independently

Anyone can re-derive a completed round's draw from data the API
already publishes and confirm it matches:

1. Fetch `GET /api/keno/rounds/{round_id}`. Take `server_seed` (hex),
   `public_seed`, and `drawn_numbers`.
2. Recompute `sha256(server_seed)` and confirm it equals
   `server_seed_hash` — proves the server didn't swap seeds after the
   fact (the hash was published *before* betting even opened).
3. Recompute `derive_keno_draw(server_seed, public_seed, pool_size=80,
   draw_count=20)` and confirm it equals `drawn_numbers`.

The server performs exactly this check itself and returns the result
as `"verified": true|false` in the same response — `packages.core.
keno.verify_draw()` is the single function both the server-side check
and any independent client-side re-derivation must reproduce.
`scripts/verify-round.ts` is a standalone, dependency-free port of this
same logic (Node's built-in `crypto` only), runnable fully offline given
just the three published values — no server trust, no account, no
network call required. It also has a `--round <id> --auth 'tma
<initData>'` convenience mode that fetches those values for you first,
but that mode still needs a real, signed Telegram session — every
gateway route requires one by platform-wide design, not a
Keno-specific restriction — so it's a convenience for someone who
already has API access, not a way to bypass authentication. The offline
mode is the one that actually delivers "verifiable by anyone, trusting
nothing." Cross-checked against the real Python implementation with
several random seed/draw vectors before being trusted here — see that
script's own header comment for exact usage.

## What is deliberately *not* covered here

This guarantees the draw is fair and untamperable **given** the
seeds shown. It does not, by itself, guarantee the platform's ledger
math (stake debited once, payout computed correctly from the paytable,
no double-settlement) — those are separate, also-tested guarantees;
see `02-data-model.md`'s ledger-integration section and
`10-test-report.md`.
