# Telebirr MacroDroid Quick Setup

Physical Android/MacroDroid setup only. For everything else about this
system, see `docs/TELEBIRR_SMS_OPERATIONS_GUIDE.md`. Give this page to
the technician setting up the dedicated payment phone; nothing else is
required for that specific job.

## Which credential do I use?

Two credentials work against the exact same URL and JSON body — pick
the one that matches your situation, both send evidence into the same
pipeline and show up identically on the admin console's Telebirr
Evidence screen:

- **A per-device token (recommended for every new phone).** Each phone
  gets its own, independently revocable credential, and the admin
  console's **Ingestion Devices** screen shows that phone's own health
  (last seen, last successful ingestion, success/failure/duplicate
  counts) — the only way to answer "is *this specific* phone still
  working" once more than one exists. Get one from an admin (Ingestion
  Devices screen → Register a device) before Step 2 below.
- **The legacy shared token** (`MACRODROID_INGEST_TOKEN`). Still fully
  supported — an already-configured phone from before per-device tokens
  existed keeps working with zero changes. Fine for a single-phone setup
  where per-device health tracking doesn't matter yet, but every phone
  sharing this one token is indistinguishable from every other on the
  admin console, and revoking it (a suspected leak) breaks every phone
  using it at once, not just one.

Everything below applies identically either way — only the Authorization
header's value differs (Step 2).

## Before you start, you need

- [ ] A dedicated Android phone (this phone's only job is receiving
      Telebirr SMS for this system — do not use a personal phone).
- [ ] A real, active SIM able to receive SMS, registered to the Telebirr
      account that will receive/send the payments this system tracks.
- [ ] A charger, kept permanently plugged in.
- [ ] Stable internet (mobile data or Wi-Fi).
- [ ] The real ingestion URL: `https://payments.arada.fun/internal/telebirr/ingest`
      — a dedicated production payment hostname, fully live end to end
      (DNS, Cloudflare Tunnel, Traefik, the real payments service),
      confirmed 2026-09-05 with real external requests: missing token →
      401, wrong token → 401, malformed body → 422.
- [ ] Either credential from "Which credential do I use?" above — it is a
      secret either way, never write it anywhere other than this one
      macro's configuration.

## Step 1 — Prepare the phone

1. Insert the SIM, confirm it has signal.
2. Connect to Wi-Fi and/or confirm mobile data works.
3. Set the screen lock to "None" or "Swipe" (removes one variable; a
   locked screen is not required to stop SMS receipt, but this keeps
   physical checks simple).
4. Install **MacroDroid** from the Google Play Store.
5. Open MacroDroid once, grant every permission it asks for, in
   particular:
   - SMS (Read SMS / Receive SMS)
   - Notifications (optional, but recommended so you can see macro
     activity)
6. Go to **Android Settings → Apps → MacroDroid → Battery** and set it to
   **Unrestricted** (not "Optimized", not "Restricted"). This single step
   is the most common real-world cause of "SMS arrives on the phone but
   the server never sees it" — Android silently kills background apps
   otherwise.
7. If this phone is Xiaomi (MIUI), Huawei (EMUI), Oppo (ColorOS), or Vivo
   (FuntouchOS): these manufacturers have an **additional**, separate
   "Autostart" or "Auto-launch" permission beyond standard Android battery
   settings. Find it under Settings → Apps → (manage apps) →
   Autostart/Auto-launch, and enable it for MacroDroid. The exact menu
   wording varies by OS version — search "[your phone brand] autostart
   permission" if you can't find it, or check MacroDroid's own support
   pages for the current list.

## Step 2 — Build the macro

Open MacroDroid and follow this exact path:

```text
MacroDroid
  → tap "+" (Add Macro)
  → Trigger
      → Messaging
          → SMS Received
          → set "Message Content" filter → Contains →
            paste exactly: Your transaction number is
          → Save
  → Action
      → Connectivity
          → HTTP Request
          → Method: POST
          → URL: https://payments.arada.fun/internal/telebirr/ingest
          → Headers (add two):
              Authorization  =  Bearer <your token — the device token from
                                 Ingestion Devices, or the legacy
                                 MACRODROID_INGEST_TOKEN>
              Content-Type   =  application/json
          → Body: switch to raw/JSON mode, enter exactly:
              {
                "raw_sms": "[sms_message]",
                "device_id": "<the exact device_id you registered in Ingestion
                               Devices, or any fixed name if using the legacy
                               shared token, e.g. shop-till-android>"
              }
          → (optional) enable "Store response in variable" so you can see
            the result in MacroDroid's own log
          → Save
  → give the macro a name, e.g. "Telebirr SMS -> Ingest"
  → Save Macro
  → make sure the toggle at the top of the macro is ON
```

If you're using a per-device token, the `device_id` in the JSON body is
informational only — the server identifies the phone from the token
itself, not from this field, so a mismatch or typo here doesn't break
anything. Still set it to the real registered device_id for clarity in
the phone's own MacroDroid log.

`[sms_message]` is MacroDroid's own built-in variable that holds the
complete text of whatever SMS just triggered the macro — select it from
MacroDroid's variable picker inside the Body field rather than typing it
by hand, so you don't mistype the variable name.

**Do not** try to extract just the reference or amount yourself and send
only that — the server needs the **complete original SMS text** to do its
own verification. Sending anything less will fail.

## Step 3 — Test it

1. Arrange for one real (or realistic-format) Telebirr SMS to arrive on
   this phone.
2. Watch MacroDroid's own log for the macro firing and the HTTP response
   code.
3. Expect **HTTP 200** with a JSON body containing `"status"`.
4. Ask whoever has admin/database access to confirm a new row appeared
   (or ask them to check the admin console's **Telebirr Evidence**
   screen for the reference from that SMS).

## What each response means

| You see | Meaning | What to do |
|---|---|---|
| HTTP 200, `"status": "ingested_available"` | Success — recipient matched, ready for a player to redeem. | Nothing — working as intended. |
| HTTP 200, `"status": "ingested_rejected"` | The SMS parsed fine, but its recipient doesn't match the configured Zemen Game account. | Tell an admin — likely the recipient isn't configured yet, or this SMS is for a different account entirely. |
| HTTP 200, `"status": "duplicate"` | This exact SMS was already ingested. | Nothing — this is safe and expected if the macro somehow fires twice for one message. |
| HTTP 200, `"status": "unparseable"` | The server couldn't read a reference from this message at all. | Check the SMS is a real Telebirr payment confirmation, not something else that happened to contain the trigger phrase. |
| HTTP 401, `"invalid bearer token"` | Wrong or missing bearer token. | Double check the `Authorization` header value for typos/extra spaces; confirm the token hasn't been rotated (ask an admin). |
| HTTP 401, `"device revoked"` | This device's own token was intentionally revoked in the admin console (Ingestion Devices screen). | Tell an admin — either this was deliberate (the phone was decommissioned) or a mistake; either way a new token must be issued to resume. |
| HTTP 503 | The server isn't configured to accept ingestion right now (no device registered and no legacy token configured). | Tell an admin — this is a server-side configuration issue, not something fixable on the phone. |
| No response / timeout | Network issue on the phone, or the server is unreachable. | Check the phone's own internet connection first; if that's fine, tell whoever manages deployment the server may be down. |

## Retry on a failed request

If the HTTP Request action fails (no response, a timeout, or a non-200
your own network caused rather than the server rejecting the content),
the SMS itself is **not lost** — it stays in the phone's normal SMS
inbox like any other message; only the *forward-to-server* step failed.
MacroDroid's HTTP Request action has its own retry/timeout settings
(exact wording varies by MacroDroid version — look for "Timeout" and any
retry-count option in the action's own advanced settings when building
Step 2) — enable a short retry there if available. Whether or not an
automatic retry is configured, treat a "No response / timeout" result
(see the table above) as something to physically check on, not silently
ignore: if the server was briefly unreachable, the specific SMS that
failed may need a manual re-trigger (MacroDroid can usually re-run a
macro against a stored/older SMS, or the message can be manually
forwarded through the same macro once — check MacroDroid's own
documentation for "re-run macro" / "test with existing message" if this
comes up).

## Reboot survival

After any phone restart (a real reboot, not just screen lock/unlock),
explicitly verify the macro is still armed **before** trusting the phone
again — don't assume it:

1. Reboot the phone.
2. Open MacroDroid and confirm the macro's own toggle (Step 2's last
   line) is still **ON**. If MacroDroid failed to auto-start at all
   (rare, but possible if the Autostart/Auto-launch permission from Step
   1.7 didn't take effect), the toggle may show ON but nothing will
   actually fire — open the app itself at least once after every reboot
   to be sure it's genuinely running, not just configured to.
3. Send one real or test-format SMS and confirm the macro fires
   (Step 3's own test procedure) — this is the only way to be certain
   the phone is actually working again after a reboot, not an assumption
   from the toggle state alone.

Add this check to whatever routine covers "what to do after this phone
loses power or restarts" — a phone that silently stopped forwarding SMS
after a reboot is indistinguishable from a working one until someone
checks.

## PRIMARY INGESTION DEGRADED

This is now the **primary** ingestion path once a device is set up — the
Telegram payment-agent flow (an authorized Telegram account pasting SMS
text directly to the bot) is the fallback if this phone goes down, not
the other way around.

If a registered device's admin-console row (Ingestion Devices screen)
shows **degraded** — active, but no successful ingestion in the last few
hours — that means either this phone stopped forwarding SMS (dead
battery, lost signal, MacroDroid killed by battery optimization, a
reboot the macro didn't survive) or genuinely no real Telebirr SMS has
arrived in that window. It does **not** mean payments have stopped being
accepted: the Telegram payment-agent path keeps working independently
and is not affected by this phone's state at all. An admin/finance
operator seeing "degraded":

1. Check whether real Telebirr transactions are expected to have
   happened in that window at all (no traffic isn't the same as broken).
2. If transactions are expected, physically check the phone: charging,
   has signal, MacroDroid's own log shows the macro firing.
3. Walk this doc's own "Reboot survival" checklist below if the phone
   was recently restarted.
4. Confirm the Telegram payment-agent path remains available as a
   fallback while this is investigated — no player-facing outage exists
   purely because one phone went quiet.

## Ongoing care

- Keep the phone charging at all times.
- Don't install unrelated apps that might trigger battery-optimization
  prompts affecting MacroDroid.
- After any Android system update, re-check Step 1.6/1.7 (OS updates
  sometimes silently re-enable battery restrictions).
- After any reboot, walk the "Reboot survival" checklist above.
- If the phone will be replaced or the SIM moved to a new device, repeat
  this entire guide on the new phone before decommissioning the old one.
- If the phone is lost or the token may have leaked, **stop** — do not
  keep using the old macro. Tell an admin immediately: for a per-device
  token, they can rotate it from the Ingestion Devices screen (the old
  token stops working the instant they do, no need to re-register the
  device from scratch) or revoke it outright if the phone is being
  decommissioned; for the legacy shared token, see the main operations
  guide, §4 — rotating it affects every phone still using it.
