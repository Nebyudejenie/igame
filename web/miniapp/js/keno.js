// Keno (build spec Part 13). Dynamically imported exactly once, the
// first time the header's Keno button is pressed (see app.v6.js's own
// click handler) -- "lazy-loaded separately from Bingo" (spec 13), so
// its board/WS-handler/onboarding weight costs nothing on every other
// screen. A self-contained module, the same precedent render/board.js
// and voice.js already set: it manages its own local state (picks,
// current round, pending tickets) rather than growing state.js's shared
// store, and reads/writes the DOM for its own three screens directly
// instead of calling back into app.v6.js's private showScreen().

import { getState, setState, subscribe, serverNow } from "./state.js";
import * as ws from "./ws.js";
import * as haptics from "./haptics.js";
import * as board from "./render/kenoBoard.js";
import { t } from "./i18n.js";

const tg = window.Telegram && window.Telegram.WebApp;
const ONBOARD_SEEN_KEY = "keno_onboarding_seen";

let boardBuilt = false;
let currentRound = null; // last GET /api/keno/state response
let selectedPicks = new Set();
let selectedStake = null;
let lastPlacedPicks = [];
// Ticket tracking is scoped per round_id, not one flat list --
// keno_round_engine.py's own settlement deliberately overlaps with the
// *next* round's betting phase ("runs as a background task, overlapping
// with the next round's own betting phase," its own comment), so a
// player can legitimately still have a settling round-N ticket at the
// exact moment round N+1 is already open for new bets. A flat,
// round-agnostic pending/settled list would mix two different rounds'
// outcomes into one result screen -- a real bug this module's own e2e
// test caught (Verify draw pointing at the wrong, still-open round).
let roundTickets = new Map(); // round_id -> {tickets: [{id,picks,stake,status,payout,matches}], pendingIds: Set<id>, settled: [ticket.settled payloads]}
let countdownInterval = null;
let closingWarned = false;
let activeScreen = null; // "keno" | "keno-result" | "keno-history" | null

function el(id) {
  return document.getElementById(id);
}

function authHeader() {
  const raw = tg ? tg.initData : "";
  return raw ? { Authorization: `tma ${raw}` } : {};
}

// --- screen switching -----------------------------------------------------
// Deliberately not app.v6.js's own showScreen(): that function's extra
// "leaving rooms" cleanup (leaveRoom/voiceCaller/countdown) only matters
// when leaving wallet/lobby/game, and the Keno nav button only exists on
// the rooms screen (currentRoomId is always already null there) -- so a
// plain generic screen toggle is exactly as correct here, without a
// static import back into app.v6.js that would defeat lazy-loading.

function showKenoScreen(name) {
  activeScreen = name;
  for (const node of document.querySelectorAll(".screen")) {
    node.classList.toggle("active", node.dataset.screen === name);
  }
  setState({ screen: name });
  if (tg) tg.BackButton.show();
}

// Called by app.v6.js's own central BackButton handler once it sees a
// "keno*" screen name -- see that file's own onClick branch. By the time
// this is reachable the player is already on a keno screen, so keno.js
// is already loaded and this dynamic import resolves instantly from the
// module cache, never re-fetching.
export function handleBack() {
  if (activeScreen === "keno-result" || activeScreen === "keno-history") {
    showKenoScreen("keno");
  } else {
    showKenoScreen("rooms");
  }
}

// --- entry point (app.v6.js's Keno nav button) -----------------------------

export async function enter() {
  if (!boardBuilt) {
    board.buildBoard(el("keno-board"), toggleNumber);
    boardBuilt = true;
    wireStaticControls();
  }
  showKenoScreen("keno");
  await refreshState();
  if (!localStorage.getItem(ONBOARD_SEEN_KEY)) {
    try {
      localStorage.setItem(ONBOARD_SEEN_KEY, "1");
    } catch {
      /* private-window/blocked storage -- show it every time rather than crash */
    }
    openOnboarding();
  }
}

// --- Telegram integration: reacts to *any* screen change, not just
// keno.js's own -- the same reactive pattern app.v6.js's connection-
// banner subscriber already uses, so MainButton/swipes never linger once
// the player has actually left every keno screen by any path (Back,
// wallet, a room). ---------------------------------------------------------

subscribe((state) => {
  const onKeno = state.screen === "keno";
  if (tg && tg.MainButton) {
    if (onKeno) updateMainButton();
    else {
      tg.MainButton.hide();
      if (tg.MainButton.offClick) tg.MainButton.offClick(handlePlay);
    }
  }
  if (tg && tg.disableVerticalSwipes && tg.enableVerticalSwipes) {
    if (onKeno) tg.disableVerticalSwipes();
    else tg.enableVerticalSwipes();
  }
  if (!state.screen.startsWith("keno")) {
    clearInterval(countdownInterval);
  }
});

function ticketBucket(roundId) {
  let bucket = roundTickets.get(roundId);
  if (!bucket) {
    bucket = { tickets: [], pendingIds: new Set(), settled: [] };
    roundTickets.set(roundId, bucket);
  }
  return bucket;
}

function hasActiveBetThisRound() {
  for (const bucket of roundTickets.values()) {
    if (bucket.tickets.length > 0) return true;
  }
  return false;
}

function updateClosingConfirmation() {
  if (!tg || !tg.enableClosingConfirmation) return;
  if (hasActiveBetThisRound()) tg.enableClosingConfirmation();
  else tg.disableClosingConfirmation();
}

// --- board interaction -----------------------------------------------------

function toggleNumber(number) {
  if (!currentRound || currentRound.status !== "betting_open") return;
  if (selectedPicks.has(number)) {
    selectedPicks.delete(number);
  } else {
    if (selectedPicks.size >= currentRound.max_picks) return;
    selectedPicks.add(number);
  }
  haptics.lightTap();
  board.setSelected(selectedPicks);
  renderPicksAndPayout();
}

function clearPicks() {
  selectedPicks = new Set();
  board.setSelected(selectedPicks);
  renderPicksAndPayout();
}

function luckyPick() {
  if (!currentRound) return;
  const count = selectedPicks.size > 0 ? selectedPicks.size : Math.min(5, currentRound.max_picks);
  const pool = Array.from({ length: 80 }, (_, i) => i + 1);
  const chosen = new Set();
  while (chosen.size < count && pool.length > 0) {
    const index = Math.floor(Math.random() * pool.length);
    chosen.add(pool.splice(index, 1)[0]);
  }
  selectedPicks = chosen;
  haptics.mediumTap();
  board.setSelected(selectedPicks);
  renderPicksAndPayout();
}

function repeatLastPicks() {
  if (lastPlacedPicks.length === 0 || !currentRound) return;
  selectedPicks = new Set(lastPlacedPicks.filter((n) => n <= 80).slice(0, currentRound.max_picks));
  board.setSelected(selectedPicks);
  renderPicksAndPayout();
}

// --- payout preview (spec 13: "updates live from the server-provided
// paytable... exact payout shown before confirming the bet") -----------

function bestMultiplierFor(pickCount) {
  const table = currentRound && currentRound.paytable ? currentRound.paytable[String(pickCount)] : null;
  if (!table) return null;
  const matchKeys = Object.keys(table).map(Number);
  if (matchKeys.length === 0) return null;
  const bestMatches = Math.max(...matchKeys);
  return { matches: bestMatches, multiplier: Number(table[String(bestMatches)]) };
}

function renderPicksAndPayout() {
  const max = currentRound ? currentRound.max_picks : 0;
  el("keno-picks-count").textContent = `${selectedPicks.size} / ${max}`;

  const best = selectedPicks.size > 0 ? bestMultiplierFor(selectedPicks.size) : null;
  const stake = selectedStake ? Number(selectedStake) : 0;
  const potential = best && stake > 0 ? stake * best.multiplier : 0;
  el("keno-payout-amount").textContent = `${potential.toFixed(2)} ETB`;

  updatePlayEnabled();
}

function isSelectionPlayable() {
  return (
    currentRound &&
    currentRound.status === "betting_open" &&
    selectedPicks.size >= currentRound.min_picks &&
    selectedPicks.size <= currentRound.max_picks &&
    selectedStake !== null
  );
}

function updatePlayEnabled() {
  const playable = isSelectionPlayable();
  el("keno-play-btn").disabled = !playable;
  if (tg && tg.MainButton) updateMainButton();
}

// --- Telegram MainButton (spec 13: "MainButton for the primary action")
// Progressive enhancement: when available, it's the real primary action
// and the in-DOM button hides so the two never show at once; the in-DOM
// button is what every other context (a plain browser, a test harness
// with no MainButton stub) falls back to. -----------------------------

function updateMainButton() {
  const domBtn = el("keno-play-btn");
  const playable = isSelectionPlayable();
  domBtn.classList.add("hidden");
  tg.MainButton.setText(t("keno.play_button"));
  if (tg.MainButton.setParams) {
    tg.MainButton.setParams({ color: "#FFC94A", text_color: "#1a1200" });
  }
  if (playable) {
    if (tg.MainButton.enable) tg.MainButton.enable();
    tg.MainButton.show();
  } else {
    if (tg.MainButton.disable) tg.MainButton.disable();
    else tg.MainButton.hide();
  }
  if (tg.MainButton.onClick) tg.MainButton.onClick(handlePlay);
}

// --- placing a ticket (REST, spec 10 -- no WS message type exists for
// this) ---------------------------------------------------------------

const ERROR_KEYS = new Set([
  "keno_disabled", "round_not_accepting_bets", "invalid_picks", "stake_not_allowed",
  "pick_count_not_allowed_at_tier", "too_many_tickets_this_round", "round_capacity_reached",
  "user_round_share_exceeded", "simulated_player", "rate_limited", "invalid_stake",
  "missing_idempotency_key",
]);

function setPlayStatus(key, kind) {
  const node = el("keno-play-status");
  node.textContent = key ? t(key) : "";
  node.classList.remove("error", "success");
  if (kind) node.classList.add(kind);
}

async function handlePlay() {
  if (!isSelectionPlayable()) return;
  const roundId = currentRound.round_id;
  const picks = Array.from(selectedPicks);
  const idempotencyKey = `keno-${roundId}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  el("keno-play-btn").disabled = true;
  if (tg && tg.MainButton && tg.MainButton.showProgress) tg.MainButton.showProgress(false);
  setPlayStatus(null);
  try {
    const response = await fetch("/api/keno/tickets", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ picks, stake: selectedStake, idempotency_key: idempotencyKey }),
    });
    const data = await response.json();
    if (!response.ok) {
      const code = ERROR_KEYS.has(data.detail) ? data.detail : "ticket_rejected";
      setPlayStatus(`keno.error.${code}`, "error");
      haptics.warning();
      return;
    }
    lastPlacedPicks = picks.slice();
    const bucket = ticketBucket(roundId);
    bucket.pendingIds.add(data.id);
    bucket.tickets.push({ id: data.id, picks: data.picks, stake: data.stake, status: "pending" });
    renderMyTickets();
    updateClosingConfirmation();
    haptics.success();
    setPlayStatus("keno.ticket_placed", "success");
    // A player can hold more than one ticket per round (Part 14's own
    // "multi-ticket paths") -- clear the board for a fresh pick set
    // rather than locking it, so placing a second ticket is just another
    // tap-tap-PLAY, not a whole new screen.
    selectedPicks = new Set();
    board.setSelected(selectedPicks);
    renderPicksAndPayout();
  } catch {
    setPlayStatus("keno.error.generic", "error");
  } finally {
    if (tg && tg.MainButton && tg.MainButton.hideProgress) tg.MainButton.hideProgress();
    updatePlayEnabled();
  }
}

// Always the *displayed* round's own tickets, not a flat cross-round
// list -- a still-settling previous round's tickets belong to its own
// (already-dismissed) result screen, not to whatever round is on screen
// now.
function renderMyTickets() {
  const section = el("keno-my-tickets-section");
  const list = el("keno-my-tickets-list");
  const tickets = currentRound ? ticketBucket(currentRound.round_id).tickets : [];
  section.classList.toggle("hidden", tickets.length === 0);
  list.innerHTML = "";
  for (const ticket of tickets) {
    const row = document.createElement("div");
    row.className = "keno-ticket-row";
    if (ticket.status === "won") row.classList.add("won");
    const picksText = ticket.picks.join(", ");
    const statusText =
      ticket.status === "pending"
        ? t("keno.ticket_pending")
        : ticket.status === "won"
          ? t("keno.ticket_won", { amount: ticket.payout })
          : t("keno.ticket_lost");
    row.innerHTML = `<span class="keno-ticket-picks">${picksText}</span><span>${statusText}</span>`;
    list.appendChild(row);
  }
}

// --- server state (REST) ---------------------------------------------------

async function refreshState() {
  try {
    const response = await fetch("/api/keno/state", { headers: authHeader() });
    if (!response.ok) return;
    currentRound = await response.json();
  } catch {
    return;
  }
  onNewRoundSnapshot();
}

function onNewRoundSnapshot() {
  el("keno-jackpot-amount").textContent = `${currentRound.jackpot_pool} ETB`;
  renderStakeChips();
  renderStatusPill();
  startCountdown();
  board.setLocked(currentRound.status !== "betting_open");
  if (currentRound.status === "betting_open") {
    board.resetDraw();
  }
  if (Array.isArray(currentRound.drawn_numbers)) {
    board.markAllDrawn(currentRound.drawn_numbers.slice(0, currentRound.reveal_index || 0));
  }
  renderPicksAndPayout();
  renderMyTickets(); // reflects whichever round is now current -- empty/hidden unless a ticket was already placed for it
}

function renderStakeChips() {
  const container = el("keno-stake-chips");
  container.innerHTML = "";
  for (const stake of currentRound.stake_options) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "amount-chip";
    chip.textContent = `${stake} ETB`;
    if (stake === selectedStake) chip.classList.add("selected");
    chip.addEventListener("click", () => {
      selectedStake = stake;
      container.querySelectorAll(".amount-chip").forEach((c) => c.classList.remove("selected"));
      chip.classList.add("selected");
      renderPicksAndPayout();
    });
    container.appendChild(chip);
  }
  if (selectedStake === null && currentRound.stake_options.length > 0) {
    selectedStake = currentRound.stake_options[0];
    container.firstChild.classList.add("selected");
  }
}

function renderStatusPill() {
  const pill = el("keno-status-pill");
  pill.classList.remove("betting", "closing", "drawing");
  const status = currentRound.status;
  if (status === "betting_open") {
    pill.classList.add("betting");
    pill.textContent = t("keno.status_betting_open");
  } else if (status === "betting_closed") {
    pill.textContent = t("keno.status_betting_closed");
  } else if (status === "drawing" || status === "draw_complete") {
    pill.classList.add("drawing");
    pill.textContent = t("keno.status_drawing");
  } else if (status === "settling") {
    pill.textContent = t("keno.status_settling");
  } else {
    pill.textContent = t("keno.status_waiting");
  }
}

function startCountdown() {
  clearInterval(countdownInterval);
  if (currentRound.status !== "betting_open" || !currentRound.betting_opened_at) return;
  const openedAtMs = new Date(currentRound.betting_opened_at).getTime();
  const deadlineMs = openedAtMs + currentRound.betting_seconds * 1000;
  const tick = () => {
    const remaining = Math.max(0, Math.round((deadlineMs - serverNow()) / 1000));
    if (currentRound.status === "betting_open") {
      el("keno-status-pill").textContent = `${t("keno.status_betting_open")} · ${remaining}s`;
    }
    if (remaining <= 0) clearInterval(countdownInterval);
  };
  tick();
  countdownInterval = setInterval(tick, 1000);
}

// --- live round events (keno:live -- every authenticated connection is
// auto-subscribed server-side, no join() call needed) -----------------

function roundIdMatches(msg) {
  return currentRound && msg.round_id === currentRound.round_id;
}

ws.on("keno.betting.open", async (msg) => {
  closingWarned = false;
  await refreshState(); // paytable/jackpot/tier can only change between rounds -- one cheap fetch per round keeps them honest without a dedicated field on every WS frame
  if (currentRound) currentRound.round_id = msg.round_id;
});

ws.on("keno.betting.closing", (msg) => {
  if (!roundIdMatches(msg) || closingWarned) return;
  closingWarned = true;
  const pill = el("keno-status-pill");
  pill.classList.add("closing");
  pill.textContent = t("keno.status_betting_closing", { seconds: Math.ceil(msg.seconds_left) });
  haptics.warning();
});

ws.on("keno.betting.closed", (msg) => {
  if (!roundIdMatches(msg)) return;
  currentRound.status = "betting_closed";
  clearInterval(countdownInterval);
  renderStatusPill();
  board.setLocked(true);
  updatePlayEnabled();
});

ws.on("keno.draw.started", (msg) => {
  if (!roundIdMatches(msg)) return;
  currentRound.status = "drawing";
  renderStatusPill();
  board.resetDraw();
  el("keno-recent-draws").innerHTML = "";
  el("keno-match-counter").classList.remove("hidden");
  el("keno-match-counter").textContent = t("keno.match_counter", { matches: 0 });
});

let matchesSoFar = 0;

ws.on("keno.number.drawn", (msg) => {
  if (!roundIdMatches(msg)) return;
  board.markDrawn(msg.number);
  const ball = document.createElement("div");
  ball.className = "keno-ball";
  ball.textContent = String(msg.number);
  const trail = el("keno-recent-draws");
  trail.appendChild(ball);
  trail.scrollLeft = trail.scrollWidth;
  if (selectedPicks.has(msg.number)) {
    matchesSoFar += 1;
    el("keno-match-counter").textContent = t("keno.match_counter", { matches: matchesSoFar });
    haptics.mediumTap();
  } else {
    haptics.lightTap();
  }
});

ws.on("keno.draw.completed", (msg) => {
  if (!roundIdMatches(msg)) return;
  currentRound.status = "draw_complete";
  currentRound.drawn_numbers = msg.drawn_numbers;
  board.markAllDrawn(msg.drawn_numbers); // safety net for any individual reveal event this client missed
  renderStatusPill();
});

ws.on("keno.round.completed", (msg) => {
  // Purely a status-pill convenience for whichever round is actually on
  // screen right now -- ticket settlement/result-display is handled
  // entirely by keno.ticket.settled below, independently of this event
  // (see roundTickets' own comment: settlement can arrive well after
  // currentRound has already moved on to the next round).
  if (!roundIdMatches(msg)) return;
  currentRound.status = "settling";
  renderStatusPill();
  matchesSoFar = 0;
});

ws.on("keno.ticket.settled", (msg) => {
  const bucket = roundTickets.get(msg.round_id);
  if (!bucket || !bucket.pendingIds.has(msg.ticket_id)) return;
  bucket.pendingIds.delete(msg.ticket_id);
  bucket.settled.push(msg);
  const ticket = bucket.tickets.find((t2) => t2.id === msg.ticket_id);
  if (ticket) {
    ticket.status = msg.payout && Number(msg.payout) > 0 ? "won" : "lost";
    ticket.payout = msg.payout;
    ticket.matches = msg.matches;
  }
  if (currentRound && currentRound.round_id === msg.round_id) renderMyTickets();
  updateClosingConfirmation();
  if (bucket.pendingIds.size === 0) {
    showResult(msg.round_id, bucket.settled);
    roundTickets.delete(msg.round_id);
  }
});

// --- result screen -----------------------------------------------------

function showResult(roundId, settled) {
  const totalPayout = settled.reduce(
    (sum, r) => sum + Number(r.payout || 0) + Number(r.jackpot_payout || 0),
    0
  );
  const won = totalPayout > 0;
  const jackpotHit = settled.some((r) => r.jackpot_payout && Number(r.jackpot_payout) > 0);

  showKenoScreen("keno-result");
  el("keno-result-confetti").innerHTML = "";
  el("keno-fairness-panel").classList.add("hidden");
  const titleEl = el("keno-result-title");
  const amountEl = el("keno-result-amount");
  if (won) {
    titleEl.textContent = jackpotHit ? t("keno.result.jackpot_title") : t("keno.result.win_title");
    titleEl.classList.add("win");
    amountEl.textContent = `+ ${totalPayout.toFixed(2)} ETB`;
    amountEl.classList.add("win");
    spawnConfetti();
    haptics.success();
  } else {
    titleEl.textContent = t("keno.result.lose_title");
    titleEl.classList.remove("win");
    amountEl.textContent = t("keno.result.no_win");
    amountEl.classList.remove("win");
  }
  const metaParts = settled.map((r) => t("keno.result.ticket_line", { matches: r.matches, amount: r.payout }));
  el("keno-result-meta").textContent = metaParts.join(" · ");
  // The round passed in explicitly, not currentRound.round_id: see
  // roundTickets' own comment above -- by the time this settlement
  // arrives, currentRound may already have moved on to the next round.
  // A real bug this module's own e2e test caught (Verify draw pointing
  // at the wrong, still-open round).
  showResult._lastRoundId = roundId;
  if (currentRound && currentRound.round_id === roundId) renderMyTickets();
}

function spawnConfetti() {
  const container = el("keno-result-confetti");
  const colors = ["var(--gold)", "var(--accent)", "var(--call)"];
  for (let i = 0; i < 28; i++) {
    const piece = document.createElement("i");
    piece.style.left = `${Math.random() * 100}%`;
    piece.style.background = colors[i % colors.length];
    piece.style.animationDuration = `${1.4 + Math.random() * 1.1}s`;
    piece.style.animationDelay = `${Math.random() * 0.35}s`;
    container.appendChild(piece);
  }
}

// --- verifiable commit-reveal draw check (reuses round_detail + keno's own
// verified flag, computed server-side the same way Bingo's fairness
// panel already does for its own rounds) --------------------------------

async function verifyResult() {
  const roundId = showResult._lastRoundId;
  if (roundId == null) return;
  const panel = el("keno-fairness-panel");
  el("keno-fairness-verified").textContent = "";
  el("keno-fairness-hash").textContent = "";
  el("keno-fairness-seed").textContent = "";
  panel.classList.remove("hidden");
  try {
    const response = await fetch(`/api/keno/rounds/${roundId}`, { headers: authHeader() });
    if (!response.ok) throw new Error("not found");
    const detail = await response.json();
    const verifiedEl = el("keno-fairness-verified");
    if (detail.verified === true) {
      verifiedEl.textContent = t("fairness.yes");
      verifiedEl.className = "verified-yes";
    } else if (detail.verified === false) {
      verifiedEl.textContent = t("fairness.no");
      verifiedEl.className = "verified-no";
    } else {
      verifiedEl.textContent = t("fairness.not_yet");
    }
    el("keno-fairness-hash").textContent = detail.server_seed_hash || "";
    el("keno-fairness-seed").textContent = detail.server_seed || "";
  } catch {
    el("keno-fairness-verified").textContent = t("fairness.error");
  }
}

// --- paytable modal ------------------------------------------------------

function openPaytable() {
  if (!currentRound) return;
  const overlay = el("keno-paytable-overlay");
  overlay.classList.remove("hidden");
  const tabsEl = el("keno-paytable-picks-tabs");
  tabsEl.innerHTML = "";
  const pickCounts = Object.keys(currentRound.paytable || {}).map(Number).sort((a, b) => a - b);
  const renderRows = (pickCount) => {
    const rows = el("keno-paytable-rows");
    rows.innerHTML = "";
    const table = currentRound.paytable[String(pickCount)] || {};
    const matches = Object.keys(table).map(Number).sort((a, b) => a - b);
    for (const m of matches) {
      const row = document.createElement("div");
      row.className = "keno-paytable-row";
      row.innerHTML = `<span class="matches">${t("keno.paytable_match_row", { matches: m, picks: pickCount })}</span><span class="multiplier">×${table[String(m)]}</span>`;
      rows.appendChild(row);
    }
  };
  pickCounts.forEach((pickCount, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "amount-chip";
    if (index === 0) chip.classList.add("selected");
    chip.textContent = String(pickCount);
    chip.addEventListener("click", () => {
      tabsEl.querySelectorAll(".amount-chip").forEach((c) => c.classList.remove("selected"));
      chip.classList.add("selected");
      renderRows(pickCount);
    });
    tabsEl.appendChild(chip);
  });
  if (pickCounts.length > 0) renderRows(pickCounts[0]);
}

// --- zero-knowledge onboarding (spec 13) --------------------------------

const ONBOARD_STEPS = [
  { pic: "🎯", textKey: "keno.onboard_step1" },
  { pic: "🎱", textKey: "keno.onboard_step2" },
  { pic: "💰", textKey: "keno.onboard_step3" },
];
let onboardIndex = 0;

function renderOnboardStep() {
  const stepsEl = el("keno-onboard-steps");
  stepsEl.innerHTML = "";
  ONBOARD_STEPS.forEach((step, index) => {
    const div = document.createElement("div");
    div.className = "keno-onboard-step";
    if (index === onboardIndex) div.classList.add("active");
    div.innerHTML = `<div class="keno-onboard-pic">${step.pic}</div><div class="keno-onboard-text">${t(step.textKey)}</div>`;
    stepsEl.appendChild(div);
  });
  const dotsEl = el("keno-onboard-dots");
  dotsEl.innerHTML = "";
  ONBOARD_STEPS.forEach((_, index) => {
    const dot = document.createElement("span");
    if (index === onboardIndex) dot.classList.add("active");
    dotsEl.appendChild(dot);
  });
  el("keno-onboard-next-btn").textContent =
    onboardIndex === ONBOARD_STEPS.length - 1 ? t("keno.onboard_done") : t("keno.onboard_next");
}

function openOnboarding() {
  onboardIndex = 0;
  renderOnboardStep();
  el("keno-onboarding-overlay").classList.remove("hidden");
}

function closeOnboarding() {
  el("keno-onboarding-overlay").classList.add("hidden");
}

// --- history / stats screen ---------------------------------------------

async function enterHistory() {
  showKenoScreen("keno-history");
  switchHistoryTab("rounds");
}

function switchHistoryTab(tab) {
  document.querySelectorAll("#screen-keno-history .keno-history-tab").forEach((tabEl) => {
    tabEl.classList.toggle("active", tabEl.dataset.tab === tab);
  });
  document.querySelectorAll("#screen-keno-history .keno-history-pane").forEach((pane) => pane.classList.add("hidden"));
  el(`keno-history-pane-${tab}`).classList.remove("hidden");
  if (tab === "rounds") loadRecentRounds();
  else if (tab === "tickets") loadMyTickets();
  else if (tab === "stats") loadMyStats();
  else if (tab === "hotcold") loadHotCold();
}

async function loadRecentRounds() {
  const list = el("keno-history-rounds-list");
  list.textContent = "";
  try {
    const response = await fetch("/api/keno/rounds/recent?limit=20", { headers: authHeader() });
    const rounds = await response.json();
    if (rounds.length === 0) {
      list.textContent = t("wallet.history_empty");
      return;
    }
    for (const round of rounds) {
      const row = document.createElement("div");
      row.className = "keno-round-row";
      const numbers = (round.drawn_numbers || []).join(", ");
      row.innerHTML = `<span>#${round.id}${round.jackpot_hit ? " 🎉" : ""}</span><span class="keno-round-numbers">${numbers}</span>`;
      list.appendChild(row);
    }
  } catch {
    list.textContent = t("wallet.error.generic");
  }
}

async function loadMyTickets() {
  const list = el("keno-history-tickets-list");
  list.textContent = "";
  try {
    const response = await fetch("/api/keno/tickets?limit=20", { headers: authHeader() });
    const tickets = await response.json();
    if (tickets.length === 0) {
      list.textContent = t("wallet.history_empty");
      return;
    }
    for (const ticket of tickets) {
      const row = document.createElement("div");
      row.className = "keno-ticket-row";
      if (ticket.status === "won") row.classList.add("won");
      const label = ticket.status === "won" ? t("wallet.history_won", { amount: ticket.payout }) : t("wallet.history_lost");
      row.innerHTML = `<span class="keno-ticket-picks">${ticket.picks.join(", ")}</span><span>${label}</span>`;
      list.appendChild(row);
    }
  } catch {
    list.textContent = t("wallet.error.generic");
  }
}

async function loadMyStats() {
  const grid = el("keno-history-stats-grid");
  grid.innerHTML = "";
  try {
    const response = await fetch("/api/keno/stats", { headers: authHeader() });
    const stats = await response.json();
    const cards = [
      [t("keno.stat_tickets"), stats.ticket_count],
      [t("keno.stat_wins"), stats.wins],
      [t("keno.stat_net"), `${stats.net} ETB`],
      [t("keno.stat_jackpot_wins"), stats.jackpot_wins],
    ];
    for (const [label, value] of cards) {
      const card = document.createElement("div");
      card.className = "keno-stat-card";
      card.innerHTML = `<div class="keno-stat-value">${value}</div><div class="keno-stat-label">${label}</div>`;
      grid.appendChild(card);
    }
  } catch {
    grid.textContent = t("wallet.error.generic");
  }
}

async function loadHotCold() {
  try {
    const response = await fetch("/api/keno/rounds/hot-cold?lookback_rounds=50", { headers: authHeader() });
    const data = await response.json();
    const hottest = el("keno-hottest-numbers");
    const coldest = el("keno-coldest-numbers");
    hottest.innerHTML = data.hottest.map((n) => `<span>${n}</span>`).join("");
    coldest.innerHTML = data.coldest.map((n) => `<span>${n}</span>`).join("");
  } catch {
    /* hot/cold is a display-only curiosity -- fail silently, same as the rest of this screen's own network-error handling */
  }
}

// --- wiring (once, at first enter()) --------------------------------------

function wireStaticControls() {
  el("keno-back-btn").addEventListener("click", () => showKenoScreen("rooms"));
  el("keno-help-btn").addEventListener("click", openOnboarding);
  el("keno-clear-btn").addEventListener("click", clearPicks);
  el("keno-lucky-btn").addEventListener("click", luckyPick);
  el("keno-repeat-btn").addEventListener("click", repeatLastPicks);
  el("keno-play-btn").addEventListener("click", handlePlay);
  el("keno-paytable-btn").addEventListener("click", openPaytable);
  el("keno-paytable-close-btn").addEventListener("click", () => el("keno-paytable-overlay").classList.add("hidden"));
  el("keno-history-btn").addEventListener("click", enterHistory);
  el("keno-history-back-btn").addEventListener("click", () => showKenoScreen("keno"));
  document.querySelectorAll("#screen-keno-history .keno-history-tab").forEach((tabEl) => {
    tabEl.addEventListener("click", () => switchHistoryTab(tabEl.dataset.tab));
  });

  el("keno-onboard-skip-btn").addEventListener("click", closeOnboarding);
  el("keno-onboard-next-btn").addEventListener("click", () => {
    if (onboardIndex === ONBOARD_STEPS.length - 1) {
      closeOnboarding();
      return;
    }
    onboardIndex += 1;
    renderOnboardStep();
  });

  el("keno-play-again-btn").addEventListener("click", () => {
    showKenoScreen("keno");
    refreshState();
  });
  el("keno-result-verify-btn").addEventListener("click", verifyResult);
  el("keno-fairness-close-btn").addEventListener("click", () => el("keno-fairness-panel").classList.add("hidden"));
}
