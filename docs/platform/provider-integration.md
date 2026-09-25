# Third-party game provider integration — design, not a build

Written 2026-09-25. Research and design only; nothing here is implemented.
Where this describes how aggregators "typically" work, that's the general
shape of the industry's seamless-wallet model, **not** any specific
provider's spec — every provider publishes its own API, and the real
integration is written against that document, not this one.

## What an aggregator integration actually is

A casino aggregator (one contract, hundreds of third-party slots and live
games) runs the games on its own servers. The operator — us — owns the
player, the money, and the responsible-gaming obligations. The two talk
through three surfaces:

1. **Game launch.** We ask the aggregator for a launch URL for a given
   player and game. We pass an opaque session token we minted; the
   aggregator returns a URL the player's client opens. The game itself
   (reels, RNG, animations) runs entirely on the provider's side.
2. **Seamless wallet callbacks (provider → us).** While the player plays,
   the provider calls *our* server, synchronously, for every money event.
   The common set:
   - `authenticate` / `balance` — exchange our session token for a player
     and return their current balance.
   - `bet` (debit) — take a stake. We must answer approved/declined, fast;
     the player's spin is waiting on us.
   - `win` (credit) — pay a win, usually referencing the bet's round.
   - `rollback` (refund) — reverse a bet that the provider couldn't
     complete (a timeout, a crashed round). Can arrive *after* the bet, or
     for a bet we never saw — both must be handled.
   Every callback carries the provider's own transaction ID and a round
   ID, and **every one can be retried** by the provider after a timeout.
   Idempotency on the provider's transaction ID is the single most
   important property of the whole integration.
3. **Reconciliation.** The provider periodically reports what it believes
   happened (bets, wins, rollbacks per round). We match that against our
   ledger and resolve differences. This is also the basis of the monthly
   invoice: aggregators typically bill a percentage of GGR generated on
   their games.

(The alternative "transfer wallet" model — move money into a
provider-held balance before play — is simpler to build but worse for
players and for responsible gaming, since limits can't be enforced per
bet. This doc assumes seamless wallet, which most aggregators support.)

## What our platform already has that fits

- **The ledger** (`packages/core/ledger.py`) is the right foundation.
  It's double-entry, append-only at the database level, row-locked, and
  `ledger.post()` already treats `idempotency_key` as unique. A provider
  bet keyed as `provider:<aggregator>:<provider_txn_id>` gets retry-safety
  for free. This is the hardest part of most integrations, and it's
  already built and tested.
- **Responsible gaming** (`packages/core/responsible_gaming.py`):
  `check_stake_allowed()` and `today_net_loss()` already combine across
  games. A provider `bet` callback calls the same check a Keno ticket
  does, and declines if it fails.
- **Player identity**: users are keyed by Telegram ID; the provider only
  ever sees an opaque token we issue, never the Telegram identity.
- **Admin, RBAC, audit log, Prometheus metrics** — all reusable as-is.
- **Cloudflare Tunnel** — adding a hostname for the callback endpoint is
  one ingress rule (`deploy/cloudflared/config.yml.example`).

## What would have to change

**Ledger**
- New `ledger_transactions.kind` values (`provider_bet`, `provider_win`,
  `provider_rollback`) — a backward-compatible CHECK-constraint widening,
  the same pattern the Keno migration used.
- A new system account kind for the house side of provider games. The
  obvious name, `provider_settlement`, is **already taken** by *payment*
  providers (`services/admin/queries.py`, `payout_worker.py`) — reusing it
  would mix game GGR with payment float. Needs its own kind, e.g.
  `game_provider_house`.
- `ledger_transactions.round_id` has a hard foreign key into Bingo's
  `rounds` table, so it can't hold a provider round. A new
  `provider_rounds` table (aggregator, provider round ID, game ID, player,
  status) linked from the transaction, the way Keno keeps its own
  `keno_rounds`.

**Responsible gaming — the one that's easy to get wrong**
- `today_net_loss()` sums a **hard-coded list** of stake and payout kinds
  (`_STAKE_KINDS = ("stake", "keno_stake")`). A new game's kinds are
  invisible to loss limits until they're added there. A provider
  integration that forgets this line silently lets players bypass their
  own daily loss cap. It needs a test that fails if a stake-like kind
  exists that the loss calculation doesn't know about.
- Self-exclusion and cooling-off must block game *launch*, not just bets.
- Autoplay/turbo features on provider games aren't under our control; our
  per-bet check is the only enforcement point.

**Auth**
- Callbacks are server-to-server, so Telegram `initData` doesn't apply.
  They need their own authentication — typically an HMAC signature over
  the request body with a shared secret, plus IP allowlisting where the
  provider publishes its ranges.
- A new session-token table (token → user, game, expiry), revocable when
  a player self-excludes mid-session.

**Latency and failure handling**
- Bet callbacks are on the player's critical path. The ledger's row lock
  plus the responsible-gaming check must answer well within the provider's
  timeout (each provider documents its own; confirm before building).
- A timed-out bet the provider later rolls back must net to zero; a
  rollback for a bet we never recorded must be accepted as a no-op, not an
  error. Both need explicit tests.

**Game catalog and frontend**
- There is **no game registry today**. The Mini App hard-codes Bingo rooms
  plus a separate Keno button (`checkKenoAvailability()`), so "a third game
  addable by config" is not currently true. It needs a `games` catalog
  table (provider, game ID, name, thumbnail, enabled, category) and a Game
  Center grid that reads it.
- Many provider games are built for a full browser tab. Running them inside
  Telegram's WebApp view (an embedded webview) needs testing per provider;
  some block framing or behave poorly in webviews.

## What's reusable from Bingo/Keno, and what isn't

| Reusable | Not reusable |
|---|---|
| Ledger, idempotency, reconciliation job pattern | `RoundEngine` / `KenoRoundEngine` — the provider runs the game |
| Responsible-gaming checks (after the kind-list fix) | Commit-reveal fairness — the provider's own certified RNG replaces it |
| Admin RBAC, audit log, metrics, alerting | Paytables, RTP guardrail, risk tiers, exposure cap, prize reserve — RTP is set by the provider's certified game, not by us |
| Telegram identity, bot notifications | Keno's per-round exposure model — provider wins are paid straight from the house side |

One real consequence: with provider games, **we don't control RTP or
volatility**. Our margin is the provider's house edge minus the
aggregator's GGR share. The platform's own games (Bingo, Keno) remain the
only ones where we set the economics.

## Engineering cost — an estimate, not a quote

For one aggregator, one engineer familiar with this codebase. These are my
estimate, and the largest uncertainty is the provider's own certification
process, which typically involves their test environment, a checklist of
edge cases, and a sign-off before production credentials are issued.

| Work | Estimate |
|---|---|
| Wallet callback API: auth, bet/win/rollback, idempotency, ledger kinds, provider rounds | 3–4 weeks |
| Responsible-gaming integration + the kind-list guard test | ~1 week |
| Session tokens, game launch, catalog table, Game Center UI, webview testing | 2–3 weeks |
| Reconciliation against provider reports, admin screens, GGR reporting | ~2 weeks |
| Provider certification: staging, their test suite, fixes | 2–4 weeks, largely outside our control |
| **Total** | **roughly 2–3 months** before real money flows |

Adding a *second* aggregator later is much cheaper (mostly an adapter),
provided the first is built with a provider-neutral internal interface.

## Non-technical prerequisites — your call, not engineering's

These decide whether the integration is possible at all, and none of them
can be solved in code:

- **Licensing.** Reputable aggregators generally require the operator to
  hold a recognized gaming license, and many won't serve certain
  jurisdictions at all. Whether a license is required to offer real-money
  games in Ethiopia, and from which authority, is the same open legal
  question flagged in `docs/keno/12-compliance.md` — it applies even more
  strongly to third-party content.
- **Provider contracts.** Aggregator agreement, per-studio approvals (some
  studios must approve each operator individually), and territory
  restrictions per game.
- **Commercial terms.** GGR revenue share, minimum monthly fees, setup
  fees, and currency. **ETB support is not a given** — many providers
  only settle in major currencies, which would mean currency conversion
  and FX exposure on every bet.
- **Payment and KYC expectations.** Providers may require stronger player
  verification than the platform does today.

**Recommendation**: settle licensing and one aggregator's commercial terms
first. Engineering would build against that specific provider's API
document; building speculatively before a contract exists would target an
API that may not be the one we end up using.
