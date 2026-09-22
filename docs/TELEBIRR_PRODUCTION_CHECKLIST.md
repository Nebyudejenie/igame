# Telebirr Production Checklist

Work through this top to bottom, in order. Do not enable `telebirr_sms`
for players until every item is checked. Full detail behind each item is
in `docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md` (section references given).

## A. Code & deployment

- [ ] Latest commit pushed to `origin/main`.
- [ ] Server pulled the latest commit (`/home/cosmic/game` on the
      production host, per this project's real deployment topology).
- [ ] `alembic upgrade head` run — confirm
      `9c1f4d7a2b3e_telebirr_sms_evidence`,
      `2f6b1a9c4d8e_telebirr_evidence_vat_receipt_url`, and
      `e3a7c9f01b2d_telebirr_ingestion_devices` are all applied.
- [ ] `gateway`, `payments`, `admin`, `bot`, `payout-worker` containers
      recreated with the current image/code (`docker compose ... up -d
      --force-recreate --no-deps <services>`).
- [ ] `GET https://app.arada.fun/healthz` → 200.
- [ ] `GET https://pay.arada.fun/healthz` → 200.
- [ ] Admin console loads at `https://admin.arada.fun/console/` and login
      works (§6 of the ops guide).

## B. Secrets & configuration

- [ ] **Preferred path**: a per-device token registered via admin console
      → Ingestion Devices → Register device (§3.2a/§4.1 of the ops
      guide) — copied once, immediately, into the phone's macro. No env
      var, no server restart, isolated per phone.
- [ ] **Legacy path** (only if not using per-device tokens):
      `MACRODROID_INGEST_TOKEN` generated (`python -c "import secrets;
      print(secrets.token_hex(32))"`) and set in the server's `deploy/.env`
      — **never** committed to git, never pasted into a shared doc/chat.
      Confirmed known only to this env file and the MacroDroid macro on
      the dedicated phone.
- [ ] `payment_provider_availability` shows `telebirr_sms / in = false`
      (the shipped default) — confirm this **before** doing anything else,
      so no player can attempt a redemption mid-setup.

## C. Recipient configuration (real, not example data)

- [ ] The real Zemen Game Telebirr receiving account is known: full
      account name **exactly** as Telebirr's own SMS states it, and the
      full (unmasked) phone number.
- [ ] Added via admin console → Payment Destinations → Add destination,
      method `telebirr`, `is_active = true`.
- [ ] Confirmed the exact account name matches character-for-character
      what appears in a real Telebirr SMS's recipient field (§6.2, §13 of
      the ops guide — this match is case-insensitive but otherwise exact,
      no fuzzy matching).

## D. Payment agent configuration

- [ ] At least one real person identified as a payment agent.
- [ ] Their real numeric Telegram user id obtained.
- [ ] Added via admin console → Payment Agents → Add agent, confirmed
      `is_active = true`.
- [ ] Agent confirms they can message the private Telegram bot and that a
      test message gets a reply (even a rejection reply proves the
      authorization path works — see §7.1 of the ops guide for exact
      reply text to expect).

## E. MacroDroid device

- [ ] Device registered in admin console → Ingestion Devices (§B above)
      *before* touching the phone, its token copied to a temporary note.
- [ ] Dedicated Android phone set up per
      `docs/TELEBIRR_MACRODROID_QUICK_SETUP.md`, start to finish.
- [ ] Battery optimization disabled for MacroDroid; autostart permission
      granted if applicable to this phone's manufacturer.
- [ ] Macro configured with the real ingest URL and this device's own
      token (or the legacy `MACRODROID_INGEST_TOKEN` only if not using
      per-device tokens).
- [ ] Macro toggled ON.
- [ ] A realistic-format test SMS produces a real HTTP 200 from the
      server (checked in MacroDroid's own log).
- [ ] The device's row in Ingestion Devices shows a real `last_seen_at`/
      `last_success_at` after that test.

## F. Parser verification against real data

- [ ] At least one real Telebirr SMS (either template, §13 of the ops
      guide) has been ingested for real (via MacroDroid or the agent
      bot) and its `payment_evidence` row inspected directly (admin
      console → Telebirr Evidence) to confirm reference/amount/recipient
      extracted correctly.
- [ ] If the real recipient's SMS uses different wording than the two
      confirmed templates, **stop** — do not enable the rail. Get a real
      sample of the new wording and have the parser extended first
      (§13/§24 of the ops guide — this system fails closed on an
      unrecognized template by design).

## G. End-to-end functional proof

Run every one of these for real, against the real production system,
before enabling the rail (§21 of the ops guide has the full live proof
already run once against local infrastructure — repeat it here against
production):

- [ ] A real small-value Telebirr payment sent to the configured account.
- [ ] Real SMS ingested (via MacroDroid or agent) → evidence row
      `status = available`, correct stored amount.
- [ ] A real test player redeems using **only** the reference → wallet
      credited the exact stored amount.
- [ ] Same reference submitted again → rejected as already redeemed, **no**
      additional credit.
- [ ] A **different** real player attempts the same reference → rejected,
      **zero** credit to them.
- [ ] `SELECT count(*) FROM ledger_transactions WHERE idempotency_key =
      '<the our_ref for this payment>'` → exactly `1`.
- [ ] Reconciliation sweep run (or wait for the hourly job) →
      `telebirr_evidence_reconciliation_mismatch_count == 0`.

## H. Admin & audit verification

- [ ] A `support`-role admin confirms they can list evidence but get a
      403 (not a crash) attempting to view raw SMS or resolve a row.
- [ ] A `finance`-role admin confirms they can view raw SMS and resolve a
      row.
- [ ] Only `superadmin` can create/deactivate agents and edit the
      recipient — confirm a `finance` admin gets a 403 attempting either.
- [ ] `admin_audit_log` shows the recipient-creation and agent-creation
      actions performed above, with the correct admin id and reason.

## I. Monitoring

- [ ] `telebirr_evidence_reconciliation_mismatch_count` and
      `telebirr_evidence_by_status` visible in Prometheus (scrape the
      payments service's `/metrics`).
- [ ] `TelebirrEvidenceReconciliationMismatch` and
      `TelebirrParserFailureSpike` alert rules loaded (`promtool check
      rules` against the live `deploy/prometheus/alerts.yml`, or confirm
      in the Prometheus UI's Rules page).
- [ ] Alertmanager receiver configured to actually page someone on
      `severity: page` (this repository does not configure a real
      Slack/PagerDuty receiver — that credential is a real deployment-time
      decision, not something committed to source).

## J. Enable

- [ ] Every item above is checked.
- [ ] Admin console → Provider Availability → `telebirr_sms` / `in` →
      Enable, with a real reason recorded.
- [ ] The player-facing Telebirr toggle now appears on the Mini App's
      Deposit screen — confirm visually in a real browser.
- [ ] Run **one more** real end-to-end test (section G) through the
      now-live player-facing UI specifically, not just the API, to prove
      the full real player experience.

## K. Rollback readiness (confirm before, not after, you need it)

- [ ] Know the exact command/action to disable instantly: admin console →
      Provider Availability → `telebirr_sms` / `in` → Disable.
- [ ] Understand that disabling stops new redemptions but **not**
      ingestion (§20 of the ops guide) — evidence keeps accumulating
      safely.
- [ ] Know the token rotation procedure (§4 of the ops guide) in case a
      device/token compromise is the reason for an emergency disable.
