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
let matchesSoFar = 0;
let closingWarned = false;
let activeScreen = null; // "keno" | "keno-result" | "keno-history" | null

// Autoplay/multi-race (packages/core/keno_autoplay.py) -- last GET/POST
// /api/keno/autoplay response, or null when no session is active. No
// dedicated WS event exists for session updates (a deliberate choice --
// every autoplay-placed ticket already rides the same keno.ticket.settled
// push a manual one does), so this is kept fresh by re-fetching on the
// round-lifecycle events that already fire regardless (keno.betting.open,
// keno.round.completed) rather than adding a new server push for it.
let activeAutoplaySession = null;

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
  // Restores an already-running session's own UI (locked board, status
  // bar) if the player left mid-session and came back -- the session
  // itself lives entirely server-side, so there's nothing else to resume.
  await refreshAutoplaySession();
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
      if (tg.MainButton.offClick) {
        tg.MainButton.offClick(handlePlay);
        tg.MainButton.offClick(stopAutoplay);
      }
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
  // The board's own .locked class only blocks pointer events (CSS
  // pointer-events:none) -- a keyboard-focused cell's Enter/Space still
  // reaches this handler directly, so an active autoplay session (picks
  // fixed server-side for its whole duration) needs its own explicit
  // guard here too, not just the visual lock.
  if (activeAutoplaySession && activeAutoplaySession.status === "active") return;
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

  renderMatchPaysRows();
  renderEmptyHint();
  updatePlayEnabled();
}

// Live Match -> Pays preview (reference: a recorded competitor's Fast
// Keno keeps this visible above the grid, recalculated as picks change,
// rather than making a player open the full paytable modal to see what
// their current selection is even worth). Shows the top two match tiers
// for the current pick count -- the two a player is actually watching
// for, not the whole table (that's what keno-paytable-btn's own modal
// is for).
function renderMatchPaysRows() {
  const container = el("keno-match-pays-rows");
  const table = currentRound && currentRound.paytable ? currentRound.paytable[String(selectedPicks.size)] : null;
  if (!table || selectedPicks.size === 0) {
    container.classList.add("hidden");
    container.innerHTML = "";
    return;
  }
  const matchCounts = Object.keys(table).map(Number).sort((a, b) => b - a).slice(0, 2).reverse();
  container.innerHTML = matchCounts
    .map(
      (m) =>
        `<div class="keno-match-pays-row"><span>${t("keno.match_label")} ${m}</span><span>${t("keno.pays_label")} ×${table[String(m)]}</span></div>`
    )
    .join("");
  container.classList.remove("hidden");
}

// The numbers the player actually has riding on the displayed round: every
// ticket placed this round (the board clears after each ticket so another
// can be picked), or, with no manual ticket, the current selection -- which
// is an active autoplay session's own picks (refreshAutoplaySession()).
// Before this, the board showed nothing of a placed ticket during the draw
// and the match counter read "0 matched" while the player's numbers were
// being drawn (found in the 2026-09-25 player-experience pass).
function numbersInPlay() {
  const bucket = currentRound ? roundTickets.get(currentRound.round_id) : undefined;
  const tickets = bucket ? bucket.tickets : [];
  if (tickets.length === 0) return selectedPicks;
  const numbers = new Set();
  for (const ticket of tickets) for (const number of ticket.picks) numbers.add(number);
  return numbers;
}

function renderEmptyHint() {
  const hint = el("keno-empty-hint");
  const showHint = currentRound && currentRound.status === "betting_open" && selectedPicks.size === 0;
  hint.classList.toggle("hidden", !showHint);
  if (showHint) {
    // "to get started" reads wrong to a player who already has a ticket in.
    const bucket = roundTickets.get(currentRound.round_id);
    const hasTicket = Boolean(bucket && bucket.tickets.length > 0);
    hint.textContent = t(hasTicket ? "keno.empty_state_hint_after_ticket" : "keno.empty_state_hint", {
      min: currentRound.min_picks,
      max: currentRound.max_picks,
    });
  }
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

// Transient "WAIT..." label: shown only in the gap right after a ticket
// was placed (board cleared for the next tap-tap-PLAY) and before the
// player has picked anything new for a second ticket this same round --
// reference: the same competitor platform's own BET button switches to
// "WAIT..." the moment a bet is accepted. Zemen Game still allows a
// second ticket immediately (unlike that reference), so this is cosmetic
// acknowledgment, not a lock.
function isAwaitingNextPick() {
  return (
    currentRound &&
    currentRound.status === "betting_open" &&
    selectedPicks.size === 0 &&
    hasActiveBetThisRound()
  );
}

function updatePlayEnabled() {
  const playable = isSelectionPlayable();
  const domBtn = el("keno-play-btn");
  domBtn.disabled = !playable;
  domBtn.textContent = isAwaitingNextPick() ? t("keno.play_button_waiting") : t("keno.play_button");
  updateAutoplayButtonEnabled();
  if (tg && tg.MainButton) updateMainButton();
}

// --- Telegram MainButton (spec 13: "MainButton for the primary action")
// Progressive enhancement: when available, it's the real primary action
// and the in-DOM button hides so the two never show at once; the in-DOM
// button is what every other context (a plain browser, a test harness
// with no MainButton stub) falls back to. -----------------------------

function updateMainButton() {
  const domBtn = el("keno-play-btn");
  domBtn.classList.add("hidden");
  const autoplayActive = activeAutoplaySession && activeAutoplaySession.status === "active";

  // MainButton-as-stop while a session is running -- the one action a
  // player actually needs one-thumb-reachable the whole time autoplay is
  // going, the same reasoning PLAY itself gets this treatment for a
  // manual bet.
  if (autoplayActive) {
    if (tg.MainButton.offClick) tg.MainButton.offClick(handlePlay);
    tg.MainButton.setText(t("keno.autoplay_stop_button"));
    if (tg.MainButton.setParams) {
      tg.MainButton.setParams({ color: "#EF4444", text_color: "#ffffff" }); // matches --danger (tokens.css)
    }
    if (tg.MainButton.enable) tg.MainButton.enable();
    tg.MainButton.show();
    if (tg.MainButton.onClick) tg.MainButton.onClick(stopAutoplay);
    return;
  }

  if (tg.MainButton.offClick) tg.MainButton.offClick(stopAutoplay);
  const playable = isSelectionPlayable();
  tg.MainButton.setText(isAwaitingNextPick() ? t("keno.play_button_waiting") : t("keno.play_button"));
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
  "missing_idempotency_key", "insufficient_balance",
  // packages/core/responsible_gaming.py's own PlayBlock.reason values,
  // now checked on every Keno bet too (2026-09-21).
  "self_excluded", "banned", "cooling_off", "loss_limit_reached",
]);

function setPlayStatus(key, kind) {
  el("keno-deposit-btn").classList.add("hidden");
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
      // Not a dead end: a player with too little balance gets the way to fix it.
      el("keno-deposit-btn").classList.toggle("hidden", code !== "insufficient_balance");
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

// --- autoplay / multi-race (packages/core/keno_autoplay.py) ---------------
// One server-driven mechanism covering both "autoplay with stop-on-win/
// loss" and "buy N future rounds" -- see that module's own docstring.
// Every ticket it places goes through the exact same place_ticket() path
// a manual bet does, so a rejection (insufficient balance, a responsible
// -gaming block, anything) just stops the session server-side; this UI
// only starts/stops sessions and reflects whatever state the server
// reports, never decides on its own whether to keep going.

const AUTOPLAY_START_ERROR_KEYS = new Set([
  "autoplay_session_already_active", "invalid_autoplay_config", "invalid_stake",
  "invalid_stop_on_win_amount", "invalid_stop_on_loss_amount",
  // keno_autoplay.start_session() refuses up front when the player's own
  // responsible-gaming status would refuse the first ticket.
  "self_excluded", "banned", "cooling_off", "loss_limit_reached",
]);

let autoplayRoundsSelection = 10; // null means "no limit" -- only meaningful while the setup sheet is open

function isAutoplayOfferable() {
  return isSelectionPlayable() && !(activeAutoplaySession && activeAutoplaySession.status === "active");
}

function updateAutoplayButtonEnabled() {
  el("keno-autoplay-btn").disabled = !isAutoplayOfferable();
}

// Kept fresh on the round-lifecycle WS events (see this module's own
// activeAutoplaySession comment for why there's no dedicated push).
async function refreshAutoplaySession() {
  try {
    const response = await fetch("/api/keno/autoplay", { headers: authHeader() });
    if (!response.ok) return;
    const data = await response.json();
    const wasActive = activeAutoplaySession && activeAutoplaySession.status === "active";
    activeAutoplaySession = data;
    if (wasActive && (!data || data.status !== "active")) {
      announceAutoplayEnded(data);
    }
    renderAutoplayUI();
  } catch {
    /* keeps showing the last-known state -- never worse than stale */
  }
}

function announceAutoplayEnded(session) {
  if (!session || !session.stop_reason) return;
  let key = `keno.autoplay_ended.${session.stop_reason}`;
  if (session.stop_reason.startsWith("ticket_rejected:")) {
    const code = session.stop_reason.slice("ticket_rejected:".length);
    key = ERROR_KEYS.has(code) ? `keno.error.${code}` : "keno.autoplay_error.generic";
  } else if (!["manual", "rounds_exhausted", "stop_on_win", "stop_on_loss", "placement_error", "settlement_error"]
    .includes(session.stop_reason)) {
    key = "keno.autoplay_error.generic";
  }
  const won = session.net_position && Number(session.net_position) > 0;
  setPlayStatus(key, won ? "success" : null);
}

function renderAutoplayUI() {
  const active = activeAutoplaySession && activeAutoplaySession.status === "active";
  el("keno-autoplay-status-bar").classList.toggle("hidden", !active);
  el("keno-betting-controls").classList.toggle("hidden", active);
  // Clear/Lucky Pick/Repeat all mutate selectedPicks directly -- without
  // this, tapping any of them while a session is running would stomp the
  // locked session's own picks display (the session itself is unaffected
  // server-side either way, but the board would lie about what it's
  // actually betting on).
  for (const id of ["keno-clear-btn", "keno-lucky-btn", "keno-repeat-btn"]) {
    el(id).disabled = active;
  }
  if (active) {
    selectedPicks = new Set(activeAutoplaySession.picks);
    board.setSelected(selectedPicks);
    board.setLocked(true);
    const s = activeAutoplaySession;
    el("keno-autoplay-status-rounds").textContent = s.rounds_total
      ? t("keno.autoplay_status_rounds", { placed: s.rounds_placed, total: s.rounds_total })
      : t("keno.autoplay_status_rounds_unlimited", { placed: s.rounds_placed });
    const net = Number(s.net_position);
    const netEl = el("keno-autoplay-net-amount");
    netEl.textContent = `${net >= 0 ? "+" : ""}${net.toFixed(2)} ETB`;
    netEl.classList.toggle("win", net > 0);
    netEl.classList.toggle("loss", net < 0);
  } else {
    board.setLocked(!currentRound || currentRound.status !== "betting_open");
  }
  updateAutoplayButtonEnabled();
}

function openAutoplaySetup() {
  if (!isAutoplayOfferable()) return;
  el("keno-autoplay-summary").textContent = t(selectedPicks.size === 1 ? "keno.autoplay_summary_one" : "keno.autoplay_summary", {
    picks: selectedPicks.size,
    stake: selectedStake,
  });
  autoplayRoundsSelection = 10;
  const chipsEl = el("keno-autoplay-rounds-chips");
  chipsEl.innerHTML = "";
  const options = [["5", 5], ["10", 10], ["25", 25], ["50", 50], [t("keno.autoplay_no_limit"), null]];
  for (const [label, value] of options) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "amount-chip";
    chip.textContent = label;
    if (value === autoplayRoundsSelection) chip.classList.add("selected");
    chip.addEventListener("click", () => {
      autoplayRoundsSelection = value;
      chipsEl.querySelectorAll(".amount-chip").forEach((c) => c.classList.remove("selected"));
      chip.classList.add("selected");
    });
    chipsEl.appendChild(chip);
  }

  el("keno-autoplay-stop-win-toggle").checked = false;
  el("keno-autoplay-stop-win-amount").disabled = true;
  el("keno-autoplay-stop-win-amount").value = "";
  el("keno-autoplay-stop-loss-toggle").checked = false;
  el("keno-autoplay-stop-loss-amount").disabled = true;
  el("keno-autoplay-stop-loss-amount").value = "";
  setAutoplaySetupStatus(null);

  el("keno-autoplay-overlay").classList.remove("hidden");
}

function closeAutoplaySetup() {
  el("keno-autoplay-overlay").classList.add("hidden");
}

function setAutoplaySetupStatus(key, kind) {
  const node = el("keno-autoplay-setup-status");
  node.textContent = key ? t(key) : "";
  node.classList.remove("error", "success");
  if (kind) node.classList.add(kind);
}

async function startAutoplay() {
  const winEnabled = el("keno-autoplay-stop-win-toggle").checked;
  const lossEnabled = el("keno-autoplay-stop-loss-toggle").checked;
  const winAmount = winEnabled ? el("keno-autoplay-stop-win-amount").value : null;
  const lossAmount = lossEnabled ? el("keno-autoplay-stop-loss-amount").value : null;

  // Mirrors keno_autoplay.start_session()'s own validation client-side --
  // a fast, specific message instead of a round trip for the common
  // mistakes; the server's own check is still the real gate either way.
  if (autoplayRoundsSelection === null && !winEnabled && !lossEnabled) {
    setAutoplaySetupStatus("keno.autoplay_error.config_required", "error");
    return;
  }
  if (winEnabled && !(Number(winAmount) > 0)) {
    setAutoplaySetupStatus("keno.autoplay_error.win_amount_required", "error");
    el("keno-autoplay-stop-win-amount").focus();
    return;
  }
  if (lossEnabled && !(Number(lossAmount) > 0)) {
    setAutoplaySetupStatus("keno.autoplay_error.loss_amount_required", "error");
    el("keno-autoplay-stop-loss-amount").focus();
    return;
  }

  el("keno-autoplay-setup-start-btn").disabled = true;
  try {
    const response = await fetch("/api/keno/autoplay", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({
        picks: Array.from(selectedPicks),
        stake: selectedStake,
        rounds_total: autoplayRoundsSelection,
        stop_on_win_amount: winEnabled ? winAmount : null,
        stop_on_loss_amount: lossEnabled ? lossAmount : null,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      const code = AUTOPLAY_START_ERROR_KEYS.has(data.detail) ? data.detail : "generic";
      setAutoplaySetupStatus(`keno.autoplay_error.${code}`, "error");
      haptics.warning();
      return;
    }
    activeAutoplaySession = data;
    closeAutoplaySetup();
    haptics.success();
    renderAutoplayUI();
  } catch {
    setAutoplaySetupStatus("keno.autoplay_error.generic", "error");
  } finally {
    el("keno-autoplay-setup-start-btn").disabled = false;
  }
}

async function stopAutoplay() {
  el("keno-autoplay-stop-btn").disabled = true;
  try {
    await fetch("/api/keno/autoplay", { method: "DELETE", headers: authHeader() });
  } catch {
    /* refreshAutoplaySession() below is the real source of truth regardless */
  } finally {
    el("keno-autoplay-stop-btn").disabled = false;
  }
  await refreshAutoplaySession();
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
    // A fresh betting phase -- the previous round's hero reveal (if this
    // client was open through settlement) is no longer relevant.
    el("keno-hero-reveal").classList.add("hidden");
  }
  if (Array.isArray(currentRound.drawn_numbers)) {
    board.markAllDrawn(currentRound.drawn_numbers.slice(0, currentRound.reveal_index || 0));
  }
  renderPicksAndPayout();
  renderMyTickets(); // reflects whichever round is now current -- empty/hidden unless a ticket was already placed for it
  refreshBoardHotCold();
}

// Hot/cold dots directly on the live betting grid (not just the History
// tab's own separate hot/cold pane) -- fetched once per round via the
// same event refreshState() already runs on, not on every WS frame,
// since the 50-round lookback this reads barely moves within a single
// round. Best-effort: a failure here just means the grid shows no dots
// this round, never blocks picking or betting.
async function refreshBoardHotCold() {
  try {
    const response = await fetch("/api/keno/rounds/hot-cold?lookback_rounds=50", { headers: authHeader() });
    if (!response.ok) return;
    const data = await response.json();
    board.setHotCold(data.hottest, data.coldest);
  } catch {
    /* display-only curiosity -- see comment above */
  }
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
  // Every new round is exactly when place_for_active_sessions() places
  // this round's autoplay ticket server-side (if any session is active) --
  // the natural, already-firing moment to pick up rounds_placed advancing
  // or a just-crossed exhaustion, without a dedicated WS push for it.
  // Runs after refreshState() above so its own board.setLocked()/
  // setSelected() (an active session's own picks) wins over that call's.
  await refreshAutoplaySession();
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
  board.setSelected(numbersInPlay());
  updatePlayEnabled();
  // renderEmptyHint() alone, not the full renderPicksAndPayout() -- the
  // hint is status-gated (betting_open only) so it's the one piece of
  // that render pass actually stale here; nothing else this event
  // touches needs a fresh potential-payout/match-pays recompute. A real
  // bug this redesign's own visual check caught: without this, a hint
  // shown right after a ticket clears the board (selectedPicks back to
  // 0) stayed on screen straight through the draw, never re-evaluated
  // once status left betting_open.
  renderEmptyHint();
});

ws.on("keno.draw.started", (msg) => {
  if (!roundIdMatches(msg)) return;
  currentRound.status = "drawing";
  renderStatusPill();
  board.resetDraw();
  board.setSelected(numbersInPlay());
  matchesSoFar = 0;
  el("keno-recent-draws").innerHTML = "";
  el("keno-match-counter").classList.remove("hidden");
  el("keno-match-counter").textContent = t("keno.match_counter", { matches: 0 });
  renderEmptyHint(); // safety net if keno.betting.closed was somehow missed -- see that handler's own comment
  el("keno-hero-reveal").classList.remove("hidden");
  el("keno-hero-ball").textContent = "";
  el("keno-hero-ball").classList.remove("keno-hero-ball-pop");
  el("keno-draw-progress").textContent = t("keno.draw_progress", { count: 0 });
});

ws.on("keno.number.drawn", (msg) => {
  if (!roundIdMatches(msg)) return;
  board.markDrawn(msg.number);
  const matched = numbersInPlay().has(msg.number);

  const heroBall = el("keno-hero-ball");
  heroBall.textContent = String(msg.number);
  heroBall.classList.toggle("matched", matched);
  // Retrigger the pop animation on every draw, not just the first --
  // removing then re-adding the class in the same frame wouldn't repaint,
  // so force a reflow between the two (the standard, dependency-free way
  // to restart a CSS animation on an already-animated element).
  heroBall.classList.remove("keno-hero-ball-pop");
  void heroBall.offsetWidth;
  heroBall.classList.add("keno-hero-ball-pop");

  el("keno-draw-progress").textContent = t("keno.draw_progress", { count: msg.index + 1 });

  const ball = document.createElement("div");
  ball.className = "keno-ball";
  if (matched) ball.classList.add("matched");
  ball.textContent = String(msg.number);
  const trail = el("keno-recent-draws");
  trail.appendChild(ball);
  trail.scrollLeft = trail.scrollWidth;
  if (matched) {
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
  // record_settlement() (packages/core/keno_autoplay.py) updates
  // net_position/status right around when this event fires -- refreshed
  // unconditionally, not gated by roundIdMatches like the status-pill
  // logic below, since an active session's own net_position matters
  // regardless of which round happens to be on screen right now.
  refreshAutoplaySession();

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
    const text = step.textKey === "keno.onboard_step1" && currentRound
      ? t("keno.onboard_step1", { min: currentRound.min_picks, max: currentRound.max_picks })
      : t(step.textKey === "keno.onboard_step1" ? "keno.onboard_step1_generic" : step.textKey);
    div.innerHTML = `<div class="keno-onboard-pic">${step.pic}</div><div class="keno-onboard-text">${text}</div>`;
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
  // app.v6.js owns the wallet screen; its own header button is the one entry point.
  el("keno-deposit-btn").addEventListener("click", () => el("open-wallet-btn").click());
  el("keno-help-btn").addEventListener("click", openOnboarding);
  el("keno-clear-btn").addEventListener("click", clearPicks);
  el("keno-lucky-btn").addEventListener("click", luckyPick);
  el("keno-repeat-btn").addEventListener("click", repeatLastPicks);

  el("keno-autoplay-btn").addEventListener("click", openAutoplaySetup);
  el("keno-autoplay-setup-close-btn").addEventListener("click", closeAutoplaySetup);
  el("keno-autoplay-setup-cancel-btn").addEventListener("click", closeAutoplaySetup);
  el("keno-autoplay-setup-start-btn").addEventListener("click", startAutoplay);
  el("keno-autoplay-stop-btn").addEventListener("click", stopAutoplay);
  el("keno-autoplay-stop-win-toggle").addEventListener("change", (event) => {
    el("keno-autoplay-stop-win-amount").disabled = !event.target.checked;
  });
  el("keno-autoplay-stop-loss-toggle").addEventListener("change", (event) => {
    el("keno-autoplay-stop-loss-amount").disabled = !event.target.checked;
  });
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
