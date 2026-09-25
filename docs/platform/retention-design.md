# Player retention features — design for approval

Written 2026-09-25. **Design only; nothing here is built.** Several pieces
already exist, and this design extends them rather than adding parallel
mechanisms. Every dollar amount stays an admin-configured rule, not a code
constant, matching how `bonus_rules` already works.

## What already exists (checked in the code)

| Piece | Where | State |
|---|---|---|
| Sticky bonus money with server-enforced playthrough | `packages/core/bonuses.py` | Built. Bonus sits in `user_bonus`, can't be staked or withdrawn, and converts to cash only once the player's own real-cash wagering since the grant reaches `wagering_required` |
| Welcome bonus on first qualifying deposit | `referrals.py::maybe_grant_welcome_bonus` | Built |
| Referral reward | `referrals.py::maybe_grant_referral_bonus` | Built. Blocks self-referral, pays once per referee, requires a minimum qualifying deposit, caps grants per referrer, and refuses when referrer and referee share a payout account |
| Referral fraud review | `bonus_queries.py::referral_fraud_candidates_admin` | Built. Flags shared payout accounts and referral velocity in 24h for human review |
| Admin notification campaigns | `packages/core/campaigns.py` | Built. **Always** excludes self-excluded and banned players, regardless of filters |
| Bot notification relay | `packages/core/notifications.py` | Built |

## Gaps found while designing

1. **Keno doesn't count toward bonus playthrough.**
   `wagering_progress_for_user_since()` counts only `lt.kind = 'stake'`
   (Bingo). A player who clears a welcome bonus by playing Keno never
   unlocks it. That's a player-facing bug the moment both games are live.
   **Your decision**: count Keno stakes at 100% (my recommendation, since
   Keno's 18% edge is comparable to Bingo's 20% house cut), at a reduced
   weight, or not at all.
2. **No player notification opt-out exists.** The spec requires re-engagement
   messages to be user-toggleable, and nothing stores that preference.
3. **Referral rewards can be farmed with deposit-then-withdraw.** The
   reward fires on the referee's qualifying *deposit* alone. The reward is
   sticky bonus money, which limits the damage, but a ring of accounts can
   still accumulate bonuses. Fix below.

## 1. Daily streak

- **Play day**: an Ethiopian calendar day (`Africa/Addis_Ababa`, the same
  day boundary the loss cap already uses) with at least one settled
  real-cash stake in any game. **Any stake counts**, including the
  smallest offered, so the streak never nudges anyone toward bigger bets.
- **Rewards at milestones, not daily**: e.g. day 3, 7, 14, 30, each a
  sticky bonus granted through the existing `grant_bonus()` path with its
  own wagering requirement. Amounts are a new `bonus_rules.trigger_type`
  (`streak_milestone`), admin-configured. Each milestone pays once per
  streak (idempotency key `streak-{user}-{streak_start}-{day}`).
- **Responsible-gaming rules**:
  - A day on which the player was blocked by their own limits
    (self-exclusion, cooling-off, loss cap reached) **pauses** the streak
    rather than breaking it. Taking a break a player chose for themselves
    must never cost them.
  - No "your streak is about to break" messages. That's loss-aversion
    pressure, and it's the one retention mechanic most likely to push
    someone to play when they'd otherwise stop.
  - Players with any active responsible-gaming limit get no streak
    notifications at all.
- **Eligibility**: only players with at least one successful deposit,
  which cuts off multi-account farming with free accounts.
- **Storage**: `player_streaks (user_id, current_start_day, current_length,
  longest, last_play_day, paused_days)`, updated by a nightly job reading
  the ledger. The ledger stays the source of truth, and the table can be
  rebuilt from it.

## 2. Referral rewards: tighten, don't rebuild

- **Pay on real play, not deposit**: grant the referrer's reward only once
  the referee has also *wagered* at least a configured multiple of their
  qualifying deposit (new rule column `min_referee_wagering`). Depositing
  and immediately withdrawing no longer earns anything.
- **Hold through the chargeback window**: don't grant until the referee's
  qualifying deposit is older than `WITHDRAW_CHARGEBACK_WINDOW_MINUTES`.
  That setting already exists.
- **New fraud signals for the existing review screen**: phone-number
  overlap (`users.phone_lookup_hash` already exists) and the referrer
  having been referred by their own referee (A→B→A rings). Still flagged
  for a human, never auto-blocked, consistent with the current design.

## 3. Welcome bonus with server-enforced playthrough

- The mechanism exists and is correctly server-enforced. The work is
  **gap 1** above (which games count) plus a player-facing progress view
  in the Mini App ("wagered 340 of 1,000 ETB to unlock 100 ETB"). Today a
  player has no way to see their own playthrough progress.
- Bonus terms shown before the first deposit, in plain language. The
  spec's own "all bonus figures displayed honestly" requirement.

## 4. Telegram re-engagement notifications

- **Triggers**: a jackpot pool milestone ("the Keno jackpot passed 10,000
  ETB"), a notable community win, a lapsed-player nudge after N days away,
  and a streak milestone *reached*. Never a streak about to break.
- **Limits, all enforced server-side**:
  - At most **one per player per day** across all automated triggers (the
    spec's default).
  - A new `users.notifications_opt_in` flag (gap 2), toggled from the
    Mini App and the bot's settings menu. Default on for new players,
    with the toggle shown on first message.
  - Quiet hours: nothing between 22:00 and 08:00 Addis Ababa time.
  - Always excluded: self-excluded, banned, cooling-off, anyone who hit
    their own loss cap today, and anyone with an active responsible-gaming
    limit, for any lapsed-player nudge. This reuses the exact exclusion
    `campaigns.py` already applies, so the two paths can't drift.
- **Content**: factual only. No "you're due a win", no losses framed as
  near-misses.

## How we'll know if it works

The Part 8 gauges built today (D1/D7/D30 retention, sessions per player,
ARPDAU, LTV) are the measure. Recommended: switch features on one at a
time, a week apart, so each one's effect on retention is visible in the
cohort numbers rather than blended together.

## Estimate

| Piece | Estimate |
|---|---|
| Playthrough game coverage (gap 1) + progress view in the Mini App | 2–3 days |
| Notification opt-in + shared exclusion + daily cap + quiet hours | 3–4 days |
| Referral tightening (wagering gate, chargeback hold, 2 new signals) | 2–3 days |
| Daily streak (table, nightly job, milestone rules, RG pause, UI) | 4–5 days |
| Re-engagement triggers (jackpot, big win, lapsed) | 3–4 days |

Roughly 3–4 weeks in total. Gap 1 and the opt-in are worth doing first
regardless, since they fix existing player-facing problems.

## Decisions I need from you before building

1. How Keno counts toward playthrough (100% / reduced weight / not at all).
2. Streak rewards as bonus money, or non-monetary (badges only) to start.
3. Whether new players default to notifications **on** (my recommendation,
   with the toggle shown on the first message) or **off**.
4. The actual amounts: milestone rewards, referral wagering multiple. I'd
   suggest running each through the risk-of-ruin simulator's handle
   assumptions before switching it on.
