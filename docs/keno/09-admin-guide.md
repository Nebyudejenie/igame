# Keno — Admin Guide / የኬኖ አስተዳደር መመሪያ

**The admin console now has a Keno screen** (as of 2026-09-25): open the
admin panel and choose **Keno** in the left menu. It has six sections:

| Section | What it does | Who can use it |
|---|---|---|
| Overview | Kill switch, prize reserve vs. player liability (kept separate), jackpot pool, current tier, live round, active config, reserve deposit/withdraw | Everyone can view; kill switch and reserve transfers are superadmin-only, with a confirmation that shows the exact amount |
| Rounds | Every round, filterable by status; click one for its tickets, draw, and state history | Everyone |
| Paytables | Active paytables, plus an editor with live RTP/hit-frequency. Saving is blocked outside the 75–97% RTP guardrail | Editor: ops and superadmin. Saving: superadmin |
| Tiers & access | Tier ladder with the current tier marked, beta allowlist add/remove | Everyone can view; changes are superadmin-only |
| Risk simulator | Monte Carlo reserve projection against a real active paytable | Ops and superadmin |
| Reports | Last-24h GGR, hold, players, retention, LTV; daily table | Everyone |

Every change still needs a real reason of at least 10 characters, and
every change is written to the audit log. The `curl` examples below
still work and cover the same actions, which is useful for scripts or if
the screen is unavailable.

**ማስታወሻ (2026-09-25)**: የአስተዳደር ፓነሉ አሁን የኬኖ ገጽ አለው — በግራ በኩል
ካለው ምናሌ **Keno** የሚለውን ይምረጡ። ስድስት ክፍሎች አሉት፦ Overview (የማቆሚያ
ቁልፍ፣ የሽልማት መጠባበቂያ እና የተጫዋቾች ዕዳ ለየብቻ፣ የጃክፖት ገንዘብ፣ የአሁኑ tier፣
የአሁኑ ዙር)፣ Rounds (ሁሉም ዙሮች እና ዝርዝራቸው)፣ Paytables (የክፍያ ሰንጠረዥ
አርታኢ፣ RTP ከ75–97% ውጪ ከሆነ ማስቀመጥ አይቻልም)፣ Tiers & access (tier እና
የቤታ ፍቃድ ዝርዝር)፣ Risk simulator (የመጠባበቂያ ትንበያ)፣ እና Reports (ገቢ፣ hold፣
ተጫዋቾች፣ retention)። ማንኛውም ለውጥ ቢያንስ 10 ፊደል ያለው ትክክለኛ ምክንያት
ይፈልጋል፣ እና በ audit log ውስጥ ይመዘገባል። ከታች ያሉት `curl` ምሳሌዎች አሁንም
ይሠራሉ።

---

## Part 1 — English

### Getting access

Every call below needs an admin session token (the same login every
other admin panel action uses) and one of two permissions: `keno:view`
(read-only), `keno:manage` (ops — computation tools only, nothing
persisted), or `keno:configure` (superadmin-only — anything that
changes live config, money, or tier state). If you don't have the
right permission, the API returns `403` — ask a superadmin to grant it
rather than trying to work around it.

Every example below assumes an environment variable `$ADMIN_TOKEN` is
already set to a valid session token, and the admin service is reached
at `$ADMIN_URL` (e.g. `https://arada.click:8001` — check with
engineering for the real internal address, since the admin service is
not meant to be publicly exposed the way the player-facing site is).

### Checking the current state (read-only, safe to run any time)

**Dashboard** — current round, reserve balance vs. player liability
(always shown separately — never assume they're the same number),
current tier, jackpot pool:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/dashboard"
```

**Config history** and **paytables**:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/configs"
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/paytables"
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/tiers"
```

### Everyday tools (ops-level, `keno:manage` — nothing persisted)

**Preview a paytable idea** before anyone commits to it — pure
computation, safe to call repeatedly while designing:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/paytables/preview" \
  -d '{"pick_count": 5, "multipliers": {"3": "3.89", "4": "11.67", "5": "16.00"}}'
```

Returns the exact RTP, hit frequency, and volatility this table would
have. If RTP is outside 75%–97%, it will be rejected later if you try
to actually activate it — better to catch that here.

**Run the risk-of-ruin simulator** before deciding on a reserve amount
or a candidate paytable — see `07-economics-and-bankroll.md` for what
the numbers mean:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/risk-of-ruin" \
  -d '{"starting_reserve": "50000", "daily_handle": "500000", "avg_stake": "50",
       "pick_count": 5, "multipliers": {"5": "16.00"}, "jackpot_diversion_bps": 150,
       "floor": "0", "days": 365, "num_simulations": 2000}'
```

### Sensitive actions (superadmin-only, `keno:configure`) — real money or live-config changes

Every one of these requires a non-empty `"reason"` in the request body
— it's written to the audit trail alongside the action, so use a real,
specific reason (not "test" or "."), since this is what you or anyone
else will read back later when asking "why was this changed."

**Move real money into the reserve** (`house_float → keno_reserve`):

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/reserve/deposit" \
  -d '{"amount": "50000.00", "reason": "Initial launch funding, approved by [name] on [date]"}'
```

**Withdraw from the reserve** — will be refused (`409`) if it would
drop the reserve below the configured floor; the attempt is still
audited either way:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/reserve/withdraw" \
  -d '{"amount": "10000.00", "reason": "..."}'
```

**Emergency stop** — instantly blocks new tickets; does **not** abort
a round already in progress (see `08-runbook.md` for exactly what it
does and doesn't do):

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/kill-switch" \
  -d '{"enabled": false, "reason": "..."}'
```

**Change the live tier manually** (overriding the automated
promotion/demotion — use sparingly; the automation exists precisely to
avoid needing this):

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/tiers/set-current" \
  -d '{"tier_id": 2, "reason": "..."}'
```

**Activate a new paytable or config version** — always preview a
paytable first (above); both endpoints enforce the RTP guardrail
server-side, so a mistake here is rejected, not silently accepted:

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/paytables" \
  -d '{"pick_count": 5, "profile": "low_variance", "multipliers": {"5": "16.00"}, "reason": "..."}'
```

### Where to check what happened

Three different logs answer three different questions — see
`08-runbook.md`'s "Reading the audit trail" section for the full
explanation: `keno_round_events` (what happened to a specific round),
`keno_tier_changes` (why the tier is what it is), `admin_audit_log`
(which admin did what, including everything above).

---

## ክፍል 2 — አማርኛ

### መዳረሻ ማግኘት

ከዚህ በታች ያለው እያንዳንዱ ጥሪ የአስተዳደር session token (ሌሎች የአስተዳደር ፓነል
ተግባራት የሚጠቀሙበት ተመሳሳይ ግባ) እና ከሚከተሉት ሶስት ፈቃዶች አንዱን ይፈልጋል፦
`keno:view` (ለንባብ ብቻ)፣ `keno:manage` (ኦፕሬሽናል — ስሌት ብቻ የሚፈጽሙ
መሳሪያዎች፣ ምንም የማይቀመጥ)፣ ወይም `keno:configure` (ለ superadmin ብቻ —
ቀጥታ ውቅር (config)፣ ገንዘብ፣ ወይም tier ሁኔታን የሚቀይር ማንኛውም ተግባር)። ትክክለኛ
ፈቃድ ከሌለዎት API `403` ይመልሳል — በራስዎ ለማለፍ ከመሞከር ይልቅ ከ superadmin
ፈቃድ እንዲሰጥዎት ይጠይቁ።

ከዚህ በታች ያሉ ምሳሌዎች ሁሉ `$ADMIN_TOKEN` የተባለ environment variable
ትክክለኛ session token ይዞ አስቀድሞ የተዘጋጀ መሆኑን፣ እና የአስተዳደር አገልግሎቱ
በ`$ADMIN_URL` (ለምሳሌ `https://arada.click:8001` — ትክክለኛውን የውስጥ
አድራሻ ከምህንድስና ቡድኑ ያረጋግጡ፣ የአስተዳደር አገልግሎቱ እንደ ተጫዋቾች ገጽ በአደባባይ
የሚታይ አይደለም) መገኘቱን ይገምታሉ።

### አሁን ያለውን ሁኔታ መመልከት (ለንባብ ብቻ፣ በማንኛውም ጊዜ ለመፈጸም ደህንነቱ የተጠበቀ)

**ዳሽቦርድ** — የአሁኑ ዙር (round)፣ የመጠባበቂያ ገንዘብ (reserve) ሂሳብ ከተጫዋቾች
ዕዳ (liability) ጋር ሲነጻጸር (ሁልጊዜ ለየብቻ ይታያል — በፍጹም አንድ አይነት ቁጥር ነው
ብለው አያስቡ)፣ የአሁኑ tier፣ የጃክፖት ገንዘብ መጠን፦

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/dashboard"
```

**የውቅር ታሪክ (config history)** እና **የክፍያ ሰንጠረዦች (paytables)**፦

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/configs"
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/paytables"
curl -H "Authorization: Bearer $ADMIN_TOKEN" "$ADMIN_URL/keno/tiers"
```

### የዕለት ተዕለት መሳሪያዎች (ኦፕሬሽናል ደረጃ፣ `keno:manage` — ምንም የማይቀመጥ)

**የክፍያ ሰንጠረዥ ሃሳብን አስቀድሞ መመልከት** ማንም ከመወሰኑ በፊት — ንጹህ ስሌት ብቻ ሲሆን፣
ዲዛይን በሚደረግበት ጊዜ ደጋግሞ ለመጠቀም ደህንነቱ የተጠበቀ ነው፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/paytables/preview" \
  -d '{"pick_count": 5, "multipliers": {"3": "3.89", "4": "11.67", "5": "16.00"}}'
```

ይህ ሰንጠረዥ የሚኖረውን ትክክለኛ RTP፣ የማሸነፍ ድግግሞሽ (hit frequency)፣ እና
volatility ይመልሳል። RTP ከ75%–97% ውጪ ከሆነ፣ በኋላ ለማስነሳት ሲሞክሩ ውድቅ
ይደረጋል — ይህንን አስቀድሞ እዚህ ማወቅ የተሻለ ነው።

**የኪሳራ አደጋ (risk-of-ruin) አስመሳይ ማስኬድ** የመጠባበቂያ መጠን ወይም እጩ የክፍያ
ሰንጠረዥ ከመወሰንዎ በፊት — ቁጥሮቹ ምን ማለት እንደሆኑ ለማወቅ `07-economics-and-
bankroll.md`ን ይመልከቱ፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/risk-of-ruin" \
  -d '{"starting_reserve": "50000", "daily_handle": "500000", "avg_stake": "50",
       "pick_count": 5, "multipliers": {"5": "16.00"}, "jackpot_diversion_bps": 150,
       "floor": "0", "days": 365, "num_simulations": 2000}'
```

### ስሜታዊ ተግባራት (ለ superadmin ብቻ፣ `keno:configure`) — እውነተኛ ገንዘብ ወይም ቀጥታ ውቅር የሚቀይሩ

ከእነዚህ ተግባራት እያንዳንዱ ባዶ ያልሆነ `"reason"` በጥያቄው ውስጥ ይፈልጋል — ከተግባሩ
ጋር ወደ audit trail ስለሚጻፍ፣ እውነተኛና ግልጽ ምክንያት ይጠቀሙ (እንደ "test" ወይም
"." ያለ ሳይሆን)፣ ምክንያቱም ይህ እርስዎ ወይም ሌላ ሰው በኋላ "ይህ ለምን ተቀየረ?" ብሎ
ሲጠይቅ የሚነበብ ነው።

**እውነተኛ ገንዘብ ወደ መጠባበቂያው ማስገባት** (`house_float → keno_reserve`)፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/reserve/deposit" \
  -d '{"amount": "50000.00", "reason": "የመጀመሪያ ማስጀመሪያ ገንዘብ፣ በ[ስም] በ[ቀን] የጸደቀ"}'
```

**ከመጠባበቂያው ማውጣት** — ይህ የተቀመጠውን ዝቅተኛ ወሰን (floor) ካስከተለ ውድቅ
ይደረጋል (`409`)፣ ሙከራው ግን በሁለቱም ሁኔታ ይመዘገባል (audited)፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/reserve/withdraw" \
  -d '{"amount": "10000.00", "reason": "..."}'
```

**የአስቸኳይ ማቆሚያ (emergency stop)** — አዲስ ትኬቶችን ወዲያውኑ ያግዳል፤ አሁን
በሂደት ላይ ያለን ዙር **አያቋርጥም** (ትክክለኛ ባህሪው ምን እንደሆነ ለማወቅ
`08-runbook.md`ን ይመልከቱ)፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/kill-switch" \
  -d '{"enabled": false, "reason": "..."}'
```

**የአሁኑን tier በእጅ መቀየር** (ራስ-ሰር ማራመድ/ማውረድን override የሚያደርግ —
በጥንቃቄ ይጠቀሙ፤ አውቶሜሽኑ ይህንን አስፈላጊነት ለማስቀረት ነው የተሰራው)፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/tiers/set-current" \
  -d '{"tier_id": 2, "reason": "..."}'
```

**አዲስ የክፍያ ሰንጠረዥ ወይም ውቅር ማስነሳት** — ሁልጊዜ አስቀድመው ከላይ ያለውን
preview ይጠቀሙ፤ ሁለቱም endpoint የ RTP ገደብን በአገልጋይ በኩል ስለሚያስፈጽሙ፣ስህተት
ቢፈጠር ውድቅ ይደረጋል፣ በጸጥታ አይታለፍም፦

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  "$ADMIN_URL/keno/paytables" \
  -d '{"pick_count": 5, "profile": "low_variance", "multipliers": {"5": "16.00"}, "reason": "..."}'
```

### የተፈጸመውን ለማየት የት እንደሚታይ

ሶስት የተለያዩ መዝገቦች (logs) ሶስት የተለያዩ ጥያቄዎችን ይመልሳሉ — ሙሉ ማብራሪያ
ለማግኘት `08-runbook.md`ን "Reading the audit trail" የተባለውን ክፍል
ይመልከቱ፦ `keno_round_events` (በአንድ የተወሰነ ዙር ላይ ምን እንደተፈጸመ)፣
`keno_tier_changes` (tier ለምን አሁን ባለበት ሁኔታ ላይ እንዳለ)፣
`admin_audit_log` (የትኛው አስተዳዳሪ ምን እንዳደረገ፣ ከላይ የተጠቀሱትን ሁሉ ጨምሮ)።
