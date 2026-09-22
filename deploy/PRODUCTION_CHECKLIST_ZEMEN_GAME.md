# Zemen Game (arada.click) production launch checklist

Run this top to bottom, once, on the actual arada.click server. This is a
**genuinely separate deployment** from `PRODUCTION_CHECKLIST.md` (the
original arada.fun/Proxmox checklist) — different server, different
domain, different cloudflared architecture (containerized here, not
host-level+external-Traefik). Don't mix steps between the two. See
`README.md`'s "Domain and Cloudflare Tunnel" section (the arada.click
half) for the *why* behind each step; this file is just the *what*, in
order.

**This is a fresh deployment**: new database, new users sign up under
"Zemen Game" from scratch — not a migration of arada.fun's existing
balances/accounts. arada.fun stays live and untouched throughout.

**Scope note**: this checklist gets you live on **Chapa + Manual** (this
platform's own launch principle — see `DECISIONS.md`). Chapa itself is
optional: if you don't have live Chapa credentials yet, leave
`CHAPA_API_KEY`/`PAYMENTS_PUBLIC_BASE_URL` blank in step 4 and the app
honestly shows only the manual rail to players (verified: `services/
payments/availability.py`). The bot's own webhook (`PUBLIC_BASE_URL`,
step 4/5) is **not optional** either way — this codebase has no polling
fallback, so without it Telegram has no way to reach the bot at all.

**A brand-new Telegram bot is needed** — this rebrand includes a new
`@username` (per explicit direction), and Telegram usernames are set by
the bot owner in BotFather, not via the Bot API. Create it yourself via
@BotFather (`/newbot`) before step 4, and set its username/display name
to whatever "Zemen Game" handle you want — `services/bot/
set_bot_profile_cli.py --fix` (step 6a below) then pushes the rest of the
profile (description, commands) from this repo's own source of truth, so
BotFather's own manual fields (short description, about text, commands)
don't need to be hand-typed there.

- [ ] **0. Prerequisites on the server**
  - Docker + Docker Compose v2 installed (`docker compose version` works)
  - This repo checked out (`git clone` from `github.com/Nebyudejenie/igame`
    — the active repo per `README.md`)
  - Ports 8000-8007 free (only used internally by the Docker network —
    nothing needs to be opened on a firewall/router; `cloudflared` in
    step 5 is the only thing that talks to the outside world)

- [ ] **1. Domain: point arada.click's nameservers at Cloudflare**
  If not already done — at your registrar, change arada.click's
  nameservers to the two Cloudflare gave you when you added the site to
  your Cloudflare account. DNS propagation can take up to 24h, though
  it's usually faster. You can proceed with the rest of this checklist
  while waiting.

- [ ] **2. Register a self-hosted GitHub Actions runner for this server**
  (Only if you want CI/CD deploys to this server too — otherwise skip to
  step 4 and deploy by hand in step 6.) On GitHub: **Settings → Actions →
  Runners → New self-hosted runner**, Linux/x64. Run the setup script it
  gives you (`./config.sh --url ... --token ...`) directly on this
  server, then `./run.sh` (or install it as a systemd service via
  `./svc.sh install && ./svc.sh start` so it survives a reboot). A
  self-hosted runner is host-specific — this server needs its own,
  separate from any runner already registered for arada.fun.

- [ ] **3. (Recommended) Require manual approval before each deploy**
  Same as the arada.fun checklist's step 3, if you haven't already set
  this up repo-wide: **Settings → Environments → production → Required
  reviewers**. This is a repo-level GitHub setting, shared across every
  deployment target — skip if already done for arada.fun.

- [ ] **4. Create `deploy/.env`** on this server (never commit this)
  ```bash
  cp deploy/.env.prod.example deploy/.env
  ```
  Generate three real secrets yourself, directly into `deploy/.env` — do
  **not** paste generated secrets into any git-tracked file, this one
  included:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # POSTGRES_PASSWORD
  python3 -c "import secrets; print(secrets.token_hex(32))"       # PHONE_ENCRYPTION_KEY (64 hex chars)
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # TELEGRAM_WEBHOOK_SECRET
  ```

  Values only you have — get these from your new bot's @BotFather entry
  and (if using Chapa) your Chapa merchant dashboard:
  ```
  TELEGRAM_BOT_TOKEN=<from @BotFather, the new Zemen Game bot>
  TELEGRAM_BOT_USERNAME=<the new bot's @username, no @>
  CHAPA_API_KEY=<blank is fine for a manual-only launch>
  ```

  Domain values — fixed, matching `deploy/cloudflared/config.yml.example`
  as-is (step 5 creates the DNS records for these):
  ```
  MINIAPP_URL=https://arada.click
  PUBLIC_BASE_URL=https://arada.click
  PAYMENTS_PUBLIC_BASE_URL=https://payments.arada.click
  AGENT_PORTAL_BASE_URL=https://agent.arada.click
  ```

  Your own admin IP(s), comma-separated (find yours with `curl -4
  ifconfig.me`) — **do not leave this blank in production**, an empty
  allowlist means unrestricted:
  ```
  ADMIN_IP_ALLOWLIST=<your real IP(s)>
  ```

  The withdrawal/deposit threshold values (`MIN_DEPOSIT_ETB`,
  `AUTO_APPROVE_WITHDRAW_ETB`, etc.) ship with the same defaults as the
  arada.fun deployment — leave them unless you want different numbers for
  this brand.

- [ ] **5. Set up the Cloudflare Tunnel** (README's own "Domain and
  Cloudflare Tunnel" section, arada.click half, has the full explanation;
  commands only, here, on this server)
  ```bash
  # Install cloudflared (Cloudflare's own instructions for your OS):
  # https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

  cloudflared tunnel login             # opens a browser, authorize your Cloudflare account
  cloudflared tunnel create zemen-game # prints a tunnel id and writes a credentials JSON

  cp deploy/cloudflared/config.yml.example deploy/cloudflared/config.yml
  # edit deploy/cloudflared/config.yml: replace <TUNNEL_ID> with the id just printed

  # Copy the credentials file cloudflared just created to where
  # docker-compose.prod.yml expects it:
  cp ~/.cloudflared/<TUNNEL_ID>.json deploy/cloudflared/tunnel-credentials.json

  cloudflared tunnel route dns zemen-game arada.click
  cloudflared tunnel route dns zemen-game www.arada.click
  cloudflared tunnel route dns zemen-game payments.arada.click
  cloudflared tunnel route dns zemen-game agent.arada.click
  cloudflared tunnel route dns zemen-game admin.arada.click
  cloudflared tunnel route dns zemen-game finance.arada.click
  cloudflared tunnel route dns zemen-game sms.arada.click
  ```
  Nothing to click in the Cloudflare dashboard beyond authorizing the CLI
  login above — the `route dns` commands create the real DNS records
  directly. Unlike arada.fun, there is no separate Traefik stack to
  configure — cloudflared's own `ingress:` rules in `config.yml` do all
  the per-hostname (and per-path, for the webhook) routing.

- [ ] **6. First deploy**
  Push to `main` (CI runs, then CD deploys — only if step 2's runner is
  registered for this server; otherwise run it by hand):
  ```bash
  cd deploy
  docker compose -f docker-compose.prod.yml up -d
  docker compose -f docker-compose.prod.yml ps   # everything healthy?
  ```

- [ ] **6a. Push the bot's Telegram profile metadata**
  ```bash
  docker compose -f docker-compose.prod.yml exec bot \
    python -m services.bot.set_bot_profile_cli --fix
  ```
  Sets the description, commands, and (per-language) metadata from this
  repo's own `TARGET_*` constants — the durable source of truth, so the
  new bot's BotFather-set fields don't drift the way arada.fun's own bot
  metadata evidently did across its own rebrand (see that file's
  docstring). Run without `--fix` first if you want to see the diff
  before applying it.

- [ ] **7. Verify it's actually reachable**
  ```bash
  curl -s https://arada.click/healthz
  curl -s https://admin.arada.click/healthz
  curl -s https://payments.arada.click/healthz
  curl -s -o /dev/null -w '%{http_code}\n' https://sms.arada.click/console/
  ```
  The first three should return `{"status":"ok"}`; the sms one has no
  `/healthz` route (unlike the other three services) so check its
  `/console` login page returns `200` instead. Then create your first
  real admin account (there's no self-registration path, on purpose):
  ```bash
  docker compose -f docker-compose.prod.yml exec admin \
    python -m services.admin.create_admin_cli --username <you> --role superadmin
  ```
  Prompts for a password (never a CLI argument — it'd land in shell
  history and be visible in `ps` to anyone else on the box), then prints
  a TOTP secret **shown only once** — scan it into an authenticator app
  immediately. Open `https://admin.arada.click/console` and log in with
  that username/password/TOTP code.

- [ ] **8. Configure at least one real manual payment destination**
  Log into the admin console (`admin.arada.click`, `payments:configure`
  role needed — superadmin) → **Payment Destinations** → add the real
  bank/Telebirr account players should send deposits to. This is the one
  step in this whole checklist that's a genuine business decision, not
  an engineering one — only you know the real account details. This is a
  fresh table in a fresh database — arada.fun's own configured
  destinations do **not** carry over.

- [ ] **9. A real end-to-end dry run before opening to the public**
  With a small real amount: register through the new bot → submit a
  manual deposit with a real reference → approve it as admin → confirm
  the balance updates in the Mini App → play a round → (win or lose is
  fine, the point is the round settles) → submit a manual withdrawal →
  approve and settle it as admin → confirm funds actually move. This is
  the same lifecycle `tests/integration/test_miniapp_wallet_e2e.py`'s own
  capstone test proves in the sandbox — the point of doing it for real
  here is proving the *real* Cloudflare Tunnel, *real* arada.click domain,
  and *real* Telegram webhook (on the new bot) all actually work
  together, which nothing in this sandbox could verify for you.

- [ ] **10. (Not an engineering checklist item — flagging, not blocking)**
  Legal/regulatory approval to operate real-money gambling in your
  jurisdiction, under the "Zemen Game" brand specifically if that
  registration is name-specific. Entirely outside what this session can
  assess.
