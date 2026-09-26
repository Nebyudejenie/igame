# Telebirr deposits: evidence and redemption fixes (design for approval)

Written 2026-09-26. **Design only; nothing here is built.** Build starts once
the operator has read and approved it.

## How the rail works today

1. A player pays the platform's Telebirr collection account from their own
   phone, using the account name and number the Mini App shows them
   (`GET /api/manual-payment-destinations`).
2. Telebirr texts the collection phone a confirmation SMS. MacroDroid on that
   phone forwards the SMS text to `POST /internal/telebirr/ingest`, or a
   Telegram payment agent forwards it to the bot.
3. The server parses the text. If the recipient name (and, for one template,
   phone) matches a configured destination, it stores a `payment_evidence`
   row with status `available`.
4. The player pastes the SMS or its reference into the bot or the Mini App.
   Whoever submits an `available` reference first is credited with its amount.

## Problem 1: anyone can create evidence (critical)

The server treats the SMS **text** as proof of payment, with no independent
signal.

- The ingest payload is only `{raw_sms, device_id}`. The server never learns
  who sent the SMS (`services/payments/app.py`).
- The ops guide tells operators **not** to filter on sender number
  (`docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md`, "Do not filter on sender number").
- The "received" template is accepted on an exact match of the account name
  alone (`telebirr_ingest._find_matching_recipient`). That name, and the
  collection number, are shown to every logged-in player, because they need
  them to pay.

**The attack:** a player texts the collection number from any phone:
"Dear <account name> You have received ETB 5000.00 from … Your transaction
number is ZZ12345678." MacroDroid forwards it, the server stores it as
`available`, and the player pastes it into the bot and is credited 5,000 ETB
that never arrived. They can repeat this with new references up to the daily
deposit cap, then withdraw real money. Tricking a payment agent into
forwarding the same text works too.

Confirmed in the code, and demonstrated by the existing test
`test_redeeming_an_available_reference_credits_the_wallet`: text written by
the test itself is ingested and credited. Whether the collection phone's
MacroDroid actually forwards an SMS from an ordinary number depends on its
trigger. The guide's recommended trigger ("Message Content contains *Your
transaction number is*") forwards it.

## Problem 2: a reference is a bearer token (high)

Redemption checks only that the reference exists and is `available`. It
never checks that the redeeming player is the one who paid. Anyone who sees
the reference before the payer redeems gets the money:

- someone shown the payer's SMS or screenshot;
- staff reading the collection phone;
- a payment agent, whose portal (`/agent-portal/submissions`) lists every
  forwarded reference with its amount and status.

The payer then gets "already redeemed", and their deposit is in someone
else's wallet.

## Design

### Evidence: stop trusting the text alone

**E1. Sender check.** MacroDroid sends the SMS sender (`[sms_number]`) as a
new `sender` field. The server accepts only configured Telebirr sender IDs
(a new setting, `TELEBIRR_SENDER_IDS`) and stores everything else as
`rejected: sender_not_telebirr`. The ops guide changes to match. This alone
stops the "text it from your own phone" attack. It isn't enough by itself:
international SMS gateways can spoof alphanumeric sender IDs, so E2 is the
real control.

**E2. Balance chain (the main control).** Every real Telebirr SMS on the
collection account ends with the account's new balance ("Your current
E-Money Account balance is ETB 252.12"), for money in and money out. A forger
can't know that figure without seeing the real account. So the server keeps
the last confirmed balance for each destination and accepts new evidence as
`available` only when:

```
new balance = last confirmed balance + amount received
new balance = last confirmed balance - amount sent - fee - VAT   (transfers out)
```

Anything that doesn't fit the chain is held as `held_for_review`, not
credited. This covers a wrong balance, a gap (a missing SMS), or two SMSs
claiming the same slot. The chain is seeded once by an admin entering the
account's current balance. SMSs that arrive out of order are resolved by
their Telebirr timestamp, within a short window (a few minutes). An admin
can re-anchor the chain from the Telebirr app's balance at any time.

**E3. Admin confirmation above a threshold.** Evidence above a configured
amount (suggested 2,000 ETB to start) goes to `held_for_review` even when
the chain fits, until an admin confirms it against the Telebirr app. This
bounds the loss from anything E1 and E2 miss.

**E4. Agents corroborate, they don't originate.** A Telegram-forwarded SMS
only confirms evidence that MacroDroid already ingested. On its own it's
held for review. Agents stop being a way to create money.

**E5. Independent verification (investigate first).** Transfer SMSs link to
a public Ethio Telecom receipt page (`transactioninfo.ethiotelecom.et/receipt/
<reference>`). If that page also works for payments *received*, and returns
the payer, the payee and the amount, the server can check every reference
against Ethio Telecom's own record. That would make E2 a second line of
defence rather than the only one. I haven't checked whether it works for
received payments, or whether scraping it is acceptable to Ethio Telecom;
that needs a manual test from the collection account first. The durable fix
is a Telebirr merchant (C2B) API account, which replaces SMS entirely. That
is a business decision.

### Redemption: bind the deposit to the payer

**R1. Match the payer.** The "received" SMS carries the payer's masked phone
(`2519****6294`). The server masks the redeeming player's registered phone
the same way, then:

- **matches**: credit immediately, as today;
- **doesn't match**: record a *claim* and send it to admin review. The
  evidence is **not** locked to that claimant, so if the real payer (whose
  phone matches) claims afterwards, they are credited immediately.

Residual risk: a masked phone only shows the first four and last four
digits, so a thief whose number shares them could still claim. That's
roughly 1 in 10,000 of players on the same prefix, and the attacker still
has to see the reference first.

**R2. Paying from someone else's phone.** A player who paid from a
relative's phone taps "I paid from another number" and enters it. The claim
goes straight to admin review with that number attached. No automatic credit.

**R3. Stop exposing references.** The agent portal shows references
masked (`DI41****4J`) and never shows amounts of unredeemed evidence.

**R4. Alerting.** Repeated not-found or mismatched claims from one player
(more than 3 an hour) are flagged in the admin console.

### Parser fixes shipped in the same change

- Amounts with thousands separators (`ETB 1,500.00`) are read as 1 today.
  The regex gets fixed, with a test above 1,000 ETB.
- Redeeming a `rejected` evidence row raises an unhandled error (a 500 in
  the Mini App, silence in the bot). It gets its own code and message.
- The duplicate check hashes the raw bytes, so the same SMS arriving twice
  with different whitespace marks real evidence `disputed`. It will compare
  parsed fields instead.

## Before the rail is switched back on

1. Keep `telebirr_sms` deposits **off** until E1, E2 and R1 are built,
   tested and deployed.
2. Reconcile every `available` and `redeemed` evidence row since launch
   against the collection account's real Telebirr transaction history. Any
   reference Telebirr doesn't show is a forgery; freeze it and review the
   player. I'll prepare the query and a review screen, but reading the
   Telebirr history is a manual step for whoever holds the collection phone.
3. Switch on with the E3 threshold low (say 500 ETB) for the first week,
   then raise it.

## Estimate

| Piece | Estimate |
|---|---|
| E1 sender check + MacroDroid/ops-guide change | 0.5 day (plus the phone reconfiguration) |
| E2 balance chain, review status, admin seed/re-anchor | 2–3 days |
| E3 threshold + admin confirm action | 1 day |
| E4 agent corroboration | 0.5 day |
| R1–R2 payer binding, claims, review queue | 2 days |
| R3–R4 portal masking, alerting | 0.5 day |
| Parser fixes | 1 day |
| Tests (forged SMS, broken chain, thief claims first, relative's phone) | included above |
| **Total** | **about 1.5–2 weeks** |

E5 is separate: half a day to investigate the receipt page, and more only if
it works.

## Decisions I need from you

1. **Telebirr's real sender ID(s)** on the collection phone (what the SMS
   shows as "from"). Needed for E1.
2. **The review threshold** (E3). I suggest 500 ETB for the first week, then
   2,000.
3. **Relative's-phone payments** (R2): review every one, as designed, or
   allow automatic credit up to a small amount.
4. **Whether to pursue a Telebirr merchant API account.** It's the only fix
   that removes SMS trust entirely.
