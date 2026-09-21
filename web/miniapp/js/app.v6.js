import { initI18n, t, setLanguage, applyServerLanguage } from "./i18n.js";
import { getState, setState, subscribe, serverNow } from "./state.js";
import * as ws from "./ws.js";
import * as haptics from "./haptics.js";
import * as board from "./render/board.js";
import * as card from "./render/card.js";
import { voiceCaller } from "./voice.js";

const tg = window.Telegram && window.Telegram.WebApp;

let winPatterns = ["row", "col", "diag"];
// Real, admin-configured per-room value from the server's own state_sync
// (rooms.min_winning_lines) -- 2 here is only the pre-sync placeholder,
// exactly like winPatterns' own default above, never a hardcoded rule.
let minWinningLines = 2;
let countdownTimer = null;
let lastLobbyCountdownSeconds = null;

// Renders the lobby countdown's gold pulse-per-second, switching to a
// continuously-pulsing red in the final 3 seconds. Only re-triggers the
// pulse animation when the integer seconds value actually changes --
// startLobbyCountdown's own interval ticks every 250ms for a smooth
// clock, far more often than the displayed number itself changes.
function renderLobbyCountdown(label, seconds) {
  label.textContent = t("lobby.starts_in", { seconds });
  label.classList.toggle("lobby-countdown-urgent", seconds > 0 && seconds <= 3);
  if (seconds !== lastLobbyCountdownSeconds) {
    lastLobbyCountdownSeconds = seconds;
    label.classList.remove("lobby-countdown-tick");
    void label.offsetWidth; // force reflow so the animation restarts
    label.classList.add("lobby-countdown-tick");
  }
}

// --- screen management --------------------------------------------------

function showScreen(name) {
  // Real, reported bug: joinRoom() subscribes this connection to a room's
  // live broadcasts (services/gateway/connection.py's _joined_rooms/
  // FanoutHub.subscribe_room()) but nothing ever called leaveRoom() to
  // undo it -- browsing from room to room across a session (create
  // several rooms, look into each one) kept every previously-viewed
  // room's subscription alive on top of the new one. ws.on("call")'s own
  // guard only checks the *screen* name ("game"/"lobby"), never which
  // room a call actually belongs to, so a still-subscribed room's own
  // independent call sequence kept overwriting the call badge/count for
  // whichever room the player was actually looking at -- exactly the
  // "shows 8 but it's really over 20" symptom this fixes. Every path
  // back to the room list goes through this one function, so leaving
  // here (not at each individual call site) closes every one of them at
  // once, including the round naturally ending and bouncing back on its
  // own via state_sync.
  if (name === "rooms") {
    const { currentRoomId } = getState();
    if (currentRoomId !== null) ws.leaveRoom(currentRoomId);
    // Two smaller leaks in the same family, closed at the same spot:
    // (1) any voice announcements this room already queued but hadn't
    // played yet would otherwise keep draining and playing *through*
    // whatever room is entered next, announcing that room's numbers
    // wrong; (2) the lobby countdown's setInterval (startLobbyCountdown())
    // is only ever cleared by the *next* one starting, so leaving a
    // lobby screen without ever opening another one left it ticking
    // forever in the background.
    voiceCaller.resetRound();
    clearInterval(countdownTimer);
  }
  for (const el of document.querySelectorAll(".screen")) {
    el.classList.toggle("active", el.dataset.screen === name);
  }
  setState({ screen: name });
  if (tg) {
    if (name === "rooms") tg.BackButton.hide();
    else tg.BackButton.show();
  }
  // The lobby's own card preview (renderLobbyCards() below) only ever gets
  // rebuilt on the next enterLobby() call -- leaving this screen doesn't
  // clear it, just hides its container via CSS. A real test caught that
  // stale DOM: an unscoped ".your-card-item" query on #screen-game picked
  // up the lobby's own leftover items too (4 instead of 2), since they're
  // still genuinely in the document, just invisible.
  if (name !== "lobby") {
    const lobbyCardsList = document.getElementById("lobby-your-cards-list");
    if (lobbyCardsList) lobbyCardsList.innerHTML = "";
  }
}

function el(id) {
  return document.getElementById(id);
}

// For the handful of places server-sourced text (an admin-entered
// manual payment destination's name/reference, notably) gets built into
// innerHTML via a template literal rather than set as textContent --
// this closes what would otherwise be a stored-XSS gap if a value ever
// contained "<" (admin-only input, so a low-likelihood path, but a real
// one worth not leaving open just because the blast radius is smaller).
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Room cards, the card-selection grid, and the AUTO toggle are plain
// <div>s (not <button>s -- they need custom layout/styling <button>'s
// own UA defaults would fight) -- an architecture audit caught that
// left them unreachable without a pointer. tabindex="0" + role="button"
// makes an element real Tab-stop; the keydown handler is what actually
// makes Enter/Space activate it the way a native button already would
// (preventDefault on Space specifically, so it activates the control
// instead of scrolling the page, the one thing a real <button> handles
// for free that this doesn't get automatically).
function makeKeyboardActivatable(element, handler) {
  element.tabIndex = 0;
  if (!element.hasAttribute("role")) element.setAttribute("role", "button");
  element.addEventListener("click", handler);
  element.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handler();
    }
  });
}

function applyStaticTranslations() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
}

// --- connection banner ----------------------------------------------------

subscribe((state) => {
  const banner = el("connection-banner");
  if (state.connection === "connected") {
    banner.classList.remove("visible");
  } else if (state.connection === "auth_failed") {
    // A code review pass caught that a terminal auth failure (stale
    // initData past Telegram's own validity window, most commonly) used
    // to look identical to an ordinary transient drop -- ws.js retried
    // forever against a handshake that could only ever fail again, and
    // the player just saw an endless "Reconnecting..." with no way to
    // know reloading the app would actually fix it.
    banner.textContent = t("connection.expired");
    banner.classList.add("visible");
  } else if (state.connection === "reconnecting" || state.connection === "offline") {
    banner.textContent = t("connection.reconnecting");
    banner.classList.add("visible");
  } else if (state.connection === "connect_failed") {
    // ws.js gave up after the very first connection attempt could never
    // complete within INITIAL_AUTH_TIMEOUT_MS -- unlike "reconnecting"
    // (which resolves itself once the network recovers), this needs an
    // explicit action: a full page reload re-runs the whole boot()
    // sequence fresh, the same recovery this banner already tells the
    // player to do for an expired session.
    banner.textContent = t("connection.connect_failed");
    banner.classList.add("visible");
  }
  banner.classList.toggle(
    "actionable", state.connection === "auth_failed" || state.connection === "connect_failed"
  );
});

// Both terminal states' own banner text tells the player to reload --
// this is what actually does it. A full reload re-runs boot() fresh,
// which is simpler and more robust than trying to resume a half
// -completed startup sequence in place. No-ops for every other
// connection state (reconnecting/offline resolve themselves).
makeKeyboardActivatable(el("connection-banner"), () => {
  if (["auth_failed", "connect_failed"].includes(getState().connection)) {
    window.location.reload();
  }
});

// --- room list --------------------------------------------------------

function renderRoomList() {
  const state = getState();
  const list = el("room-list");
  list.innerHTML = "";
  if (state.rooms.length === 0) {
    const empty = document.createElement("div");
    empty.className = "wallet-note";
    empty.textContent = t("rooms.empty");
    list.appendChild(empty);
    return;
  }
  for (const room of state.rooms) {
    const card = document.createElement("div");
    card.className = "room-card" + (room.status === "running" ? " playing" : "");
    card.dataset.roomId = String(room.room_id);
    card.innerHTML = `
      <div>
        <div class="stake">${room.stake} ETB</div>
        <div class="players">🟢 ${room.players}</div>
      </div>
      <div style="text-align:right">
        <div class="derash-line">${
          room.status === "running" ? t("rooms.derash_amount", { amount: room.derash }) : ""
        }</div>
        <div class="countdown">${roomCountdownText(room)}</div>
      </div>
    `;
    makeKeyboardActivatable(card, () => enterRoom(room.room_id));
    list.appendChild(card);
  }
}

// Mini App spec 2.1's own mockup: "0:18" (a bare countdown) while a room
// is still filling its lobby, the bare word "Playing" once the round's
// started -- "← countdown or 'Playing'" is the diagram's own inline
// label for this exact column. There's no honest way to predict when a
// running round will actually end (that depends entirely on when a
// player completes a pattern, purely random), so this never fabricates
// a number for one -- the spec's separate prose line ("next in ~40s")
// reads as a looser paraphrase of the same row, not a literal formula
// this could compute without guessing.
function roomCountdownText(room) {
  if (room.status === "lobby" && room.lobby_deadline_ms != null) {
    const secondsLeft = Math.max(0, Math.round((room.lobby_deadline_ms - serverNow()) / 1000));
    const minutes = Math.floor(secondsLeft / 60);
    const seconds = String(secondsLeft % 60).padStart(2, "0");
    return `${minutes}:${seconds}`;
  }
  if (room.status === "running") return t("rooms.playing_now");
  return "";
}

ws.on("rooms", (msg) => {
  setState({ rooms: msg.rooms, onlineCount: msg.online_count ?? 0 });
  updateRoomsActivityHeader();
  if (getState().screen === "rooms") renderRoomList();
});

// "Online" comes straight from the server (a real WebSocket headcount);
// "Playing" is summed here from each room's own player count rather than
// asking the server for a second figure -- list_rooms() already carries
// everything needed for it.
function updateRoomsActivityHeader() {
  const state = getState();
  const playing = state.rooms.reduce((sum, room) => sum + room.players, 0);
  el("rooms-online-count").textContent = String(state.onlineCount ?? 0);
  el("rooms-playing-count").textContent = String(playing);
}

function refreshRoomList() {
  ws.requestRooms();
}

// The room list only gets a fresh "rooms" push on request (boot, or
// returning here after a round ends) -- without this, a lobby room's
// countdown would sit frozen at whatever number happened to be true the
// last time the list was fetched, indistinguishable from a bug to a
// player watching it. Same guarded-permanent-interval shape as the
// session-time reminder below; updates the existing room data in-memory
// (no new request) so lobby countdowns actually tick.
//
// This used to call renderRoomList() itself, which wipes and rebuilds
// every card via innerHTML = "" every single second -- destroying and
// replacing the exact DOM node a keyboard user just tabbed to, silently
// kicking focus back to <body> once a second. Ticking only the countdown
// text in place keeps the existing nodes (and their focus/listeners)
// alive; a full rebuild is still what happens whenever genuinely new
// room data arrives above.
setInterval(() => {
  if (getState().screen === "rooms") tickRoomCountdowns();
}, 1000);

function tickRoomCountdowns() {
  const rooms = getState().rooms;
  for (const roomEl of el("room-list").children) {
    const room = rooms.find((r) => String(r.room_id) === roomEl.dataset.roomId);
    if (!room) continue;
    const countdownEl = roomEl.querySelector(".countdown");
    if (countdownEl) countdownEl.textContent = roomCountdownText(room);
  }
}

// --- entering a room: state_sync decides which screen to show -----------

function enterRoom(roomId) {
  // Earliest reliable user gesture in the join flow -- satisfies mobile
  // Safari / Telegram WebView's autoplay policy for every announce()
  // later in this session, without needing a gesture on every call.
  voiceCaller.unlock();
  ws.joinRoom(roomId);
}

ws.on("state_sync", (msg) => {
  // Defense in depth alongside the server-side single-room-per-connection
  // enforcement in services/gateway/connection.py's _handle_join(): a
  // state_sync for a room this client has already navigated away from can
  // still be in flight (queued server-side before a "leave" was processed,
  // or a queue-overflow resync) when it lands here. Applying it blind used
  // to be able to overwrite state.round with a stranger room's data, or --
  // worse, for a voided/done sync -- call showScreen("rooms") -> leaveRoom
  // against whatever room this connection is *actually* in now, silently
  // kicking the player out of a live round they're still genuinely playing.
  if (msg.room_id !== getState().currentRoomId) return;
  setState({ round: msg });
  winPatterns = msg.win_patterns || winPatterns;
  minWinningLines = msg.min_winning_lines || minWinningLines;

  if (msg.status === "voided" || msg.status === "done") {
    showScreen("rooms");
    refreshRoomList();
  } else if (msg.status === "idle" || msg.status === "lobby") {
    // "idle" means no round has ever been created for this room yet --
    // round_engine.py only creates one lazily, on the very first
    // take_card. A genuinely fresh room (every room in a brand-new
    // deployment, before its first-ever player) has no other path to
    // the card-selection screen: routing "idle" back to the room list
    // here left the very first player able to open a room but never
    // take a card, permanently, since nothing else in this client ever
    // requests the lobby UI. The whole e2e suite missed this because
    // every existing test pre-seeds a round via a direct engine.join()
    // before the browser ever connects, so "idle" was never actually
    // exercised end-to-end. enterLobby() already tolerates the fields
    // an idle sync doesn't have yet (no round_id, no lobby_deadline_ms,
    // your_cards: []) -- it just shows an open 432-card grid with no
    // countdown running yet, which is exactly correct for "nobody has
    // taken a card in this room yet."
    enterLobby(msg);
  } else if (msg.status === "running" || msg.status === "settling") {
    if (msg.your_cards && msg.your_cards.length > 0) {
      enterGame(msg);
    } else {
      enterSpectate(msg);
    }
  }
});

// --- lobby (card selection) --------------------------------------------

// heldCards is what the server has actually confirmed this user owns;
// pendingCards is a number just tapped, with its take_card command sent
// but not yet acked. There's no separate "select, then confirm" step --
// a tap commits immediately (product decision: a confirm button between
// tapping a number and actually taking it was pure friction, since the
// server is already the real gate on balance/room-cap/races either way).
// pendingCards exists only so a cell shows as taken right away (instant
// feedback) and can't be double-tapped again before its own ack lands.
let heldCards = new Set();
let pendingCards = new Set();
const takenCards = new Set();
let maxCardsPerPlayer = 1;
// Counts take_card commands sent that haven't acked yet -- see the ack
// handler below for why this exists.
let pendingTakeCardAcks = 0;
// The grid's cell count is always just 1..card_pool_size -- server-told,
// never a hardcoded literal (a real production incident: the frontend
// used to hardcode this number itself, a separate value from packages/
// core/bingo.py's own _POOL_SIZE and from the cards table's real seeded
// rows, with nothing forcing them to agree -- see state_sync's own
// card_pool_size field). Room-independent, so the DOM only ever needs
// building once, not on every enterLobby() call. That matters more now
// than it used to: a tap commits immediately (see the ack handler below),
// so a resync can land while a player is still mid-tap on a second card,
// and rebuilding via grid.innerHTML = "" right then would both flicker
// the whole grid and detach the very cell they're about to tap (an E2E
// test caught this for real -- Playwright's own "Element is not attached
// to the DOM" on the second of two quick taps).
let cardGridBuilt = false;

function enterLobby(sync) {
  showScreen("lobby");
  const yourCards = sync.your_cards || [];
  heldCards = new Set(yourCards.map((c) => c.card_no));
  pendingCards.clear();
  maxCardsPerPlayer = sync.max_cards_per_player || 1;
  pendingTakeCardAcks = 0;
  renderLobbyCards(yourCards);

  // A real production gap, caught live: a player who joined a room after
  // someone else already took a card saw that card as still available --
  // takenCards used to only ever learn about a taken card from a live
  // card_taken broadcast, which a client that joins *after* the take
  // already happened was never subscribed to receive. state_sync now
  // carries the room's real, complete taken_cards list on every sync
  // (join, resync after an ack, reconnect), so this can just be
  // rebuilt from the server's own authoritative answer every time,
  // instead of the client trying to reconstruct it from a stream of
  // broadcasts it may have joined partway through.
  takenCards.clear();
  for (const cardNo of sync.taken_cards || []) takenCards.add(cardNo);

  if (!cardGridBuilt) {
    buildCardGrid(sync.card_pool_size);
    cardGridBuilt = true;
  } else {
    renderCardGridState();
  }

  updateLobbyCta();
  startLobbyCountdown(sync);
  updateLobbyMoneyBar(sync);
  const state = getState();
  if (state.user) el("lobby-balance-amount").textContent = `${state.user.balance} ETB`;
}

// A real production gap, flagged directly from a user recording: taking a
// card during selection produced no visible confirmation beyond the grid
// cell itself turning purple -- the actual generated 5x5 grid a player is
// about to play only ever rendered once the round transitioned to
// #screen-game. This is the lobby's own equivalent of buildGameCards()
// below, deliberately simpler: nothing has been called yet and the round
// hasn't started, so there's no marking to do and no claim to make --
// just the plain static preview renderStaticCard() already provides for
// exactly this "no live state to track" case (also used by the result
// screen's winning-card preview).
function renderLobbyCards(yourCards) {
  const section = el("lobby-your-cards-section");
  const list = el("lobby-your-cards-list");
  list.innerHTML = "";
  section.classList.toggle("hidden", yourCards.length === 0);
  for (const c of yourCards) {
    const item = document.createElement("div");
    item.className = "your-card-item";

    const title = document.createElement("div");
    title.className = "your-card-title";
    title.textContent = t("game.your_card_no", { card: c.card_no });
    item.appendChild(title);

    const cardEl = document.createElement("div");
    item.appendChild(cardEl);
    card.renderStaticCard(cardEl, c.grid, [], null);

    list.appendChild(item);
  }
}

// Shared by enterLobby() (initial values) and the lobby_tick handler
// below (live updates every second while players keep joining) --
// stake/derash/players all already ride lobby_tick's own payload
// (round_engine.py's _run_lobby()), so this is zero new backend work.
function updateLobbyMoneyBar(msg) {
  if (msg.stake !== undefined) el("lobby-stake-amount").textContent = `${msg.stake} ETB`;
  if (msg.derash !== undefined) el("lobby-win-amount").textContent = `${msg.derash} ETB`;
}

function buildCardGrid(cardPoolSize) {
  const grid = el("card-grid");
  grid.innerHTML = "";
  for (let n = 1; n <= cardPoolSize; n++) {
    const cell = document.createElement("div");
    cell.className = "card-grid-cell";
    cell.textContent = String(n);
    makeKeyboardActivatable(cell, () => takeCardNow(n));
    grid.appendChild(cell);
  }
  renderCardGridState();
}

// A tap commits immediately -- no separate confirm step (see heldCards'
// own comment above for why). The server is still the real gate: a
// rejection (insufficient balance, someone else took it a moment earlier)
// shows the existing ack toast and, once the batch's re-sync below runs,
// the cell simply reverts to unselected -- "no change" from the player's
// point of view, exactly as if the tap had never landed, and nothing about
// the round or other players is affected either way.
function takeCardNow(n) {
  if (heldCards.has(n) || pendingCards.has(n)) return; // already committed or in flight
  if (takenCards.has(n)) return; // someone else's
  if (heldCards.size + pendingCards.size >= maxCardsPerPlayer) return; // at this room's cap
  const state = getState();
  if (!state.currentRoomId) return;
  haptics.lightTap();
  pendingCards.add(n);
  pendingTakeCardAcks += 1;
  renderCardGridState();
  updateLobbyCta();
  ws.takeCard(state.currentRoomId, n);
}

function renderCardGridState() {
  for (const cellEl of el("card-grid").children) {
    const n = Number(cellEl.textContent);
    const mine = heldCards.has(n) || pendingCards.has(n);
    cellEl.classList.toggle("taken", takenCards.has(n) && !mine);
    cellEl.classList.toggle("selected", mine);
  }
}

// Purely a status readout now (never clickable -- taking a card happens
// on the grid tap itself, see takeCardNow() above).
function updateLobbyCta() {
  const cta = el("lobby-cta");
  const total = heldCards.size + pendingCards.size;
  cta.textContent = total > 0 ? t("lobby.cards_held", { count: total }) : t("lobby.pick_card");
}

ws.on("card_taken", (msg) => {
  if (msg.taken) takenCards.add(msg.card_no);
  else takenCards.delete(msg.card_no);
  if (getState().screen === "lobby") renderCardGridState();
});

ws.on("ack", (msg) => {
  if (!msg.ok) showToast(msg.reason || "error.generic");
  // A take_card ack for a room this client has already left (in flight
  // when the leave happened) must never touch this room's own
  // pendingTakeCardAcks counter or trigger a re-join -- enterLobby()
  // already reset that counter to 0 for whatever room is actually on
  // screen now, so decrementing it here for a stray ack could drive it
  // negative-then-clamped or, worse, hit 0 and force a spurious
  // ws.joinRoom() against the *current* room, restarting its countdown
  // and rebuilding its lobby DOM out from under the player mid-interaction.
  if (msg.for === "take_card" && msg.room_id !== getState().currentRoomId) return;
  if (msg.for === "take_card") {
    if (msg.ok) haptics.success();
    // Several take_card commands can be in flight at once (quick taps on
    // different cards) -- re-syncing after every single ack would rebuild
    // the whole lobby grid that many times, destroying scroll/focus each
    // time. Wait until every outstanding one has acked (success or
    // failure) before doing the one re-sync that actually matters.
    pendingTakeCardAcks = Math.max(0, pendingTakeCardAcks - 1);
    if (pendingTakeCardAcks === 0) {
      // The ack only confirms success -- it doesn't carry the card's
      // actual grid (the command channel's replies are deliberately
      // {ok, reason} only). Re-requesting state_sync is what actually
      // populates your_cards, which enterGame() needs once the round
      // starts; patching held state locally from acks alone left grids
      // null and crashed setGrid() the moment round_start fired (a real
      // bug an E2E test caught -- see DECISIONS.md). This same re-sync is
      // also what clears pendingCards (enterLobby() rebuilds it from
      // scratch), so a rejected tap cleanly reverts without any special
      // -casing here.
      const state = getState();
      if (state.currentRoomId !== null) ws.joinRoom(state.currentRoomId);
    }
  }
});

function startLobbyCountdown(sync) {
  clearInterval(countdownTimer);
  const label = el("lobby-countdown");
  // An "idle" room (nobody has ever taken a card in it yet) has no real
  // deadline -- round_engine.py only sets one once the first take_card
  // creates the round. Showing "Starts in 0s" against a null deadline
  // would be actively misleading here; the CTA below is the real call
  // to action until a real round_start/state_sync hands back a genuine
  // lobby_deadline_ms.
  if (sync.lobby_deadline_ms == null) {
    label.classList.remove("lobby-countdown-tick", "lobby-countdown-urgent");
    label.textContent = t("lobby.pick_card");
    return;
  }
  lastLobbyCountdownSeconds = null;
  countdownTimer = setInterval(() => {
    const secondsLeft = Math.max(0, Math.round((sync.lobby_deadline_ms - serverNow()) / 1000));
    renderLobbyCountdown(label, secondsLeft);
  }, 250);
}

ws.on("lobby_tick", (msg) => {
  if (getState().screen !== "lobby") return;
  renderLobbyCountdown(el("lobby-countdown"), msg.seconds_left);
  updateLobbyMoneyBar(msg);
});

el("lobby-refresh-btn").addEventListener("click", () => {
  const state = getState();
  if (state.currentRoomId === null) return;
  haptics.lightTap();
  const btn = el("lobby-refresh-btn");
  btn.classList.add("spinning");
  setTimeout(() => btn.classList.remove("spinning"), 300);
  ws.joinRoom(state.currentRoomId);
});

// Lobby's own connection pill -- the room-list header only ever shows a
// top-level banner for actionable states (auth_failed/connect_failed);
// this gives the lobby screen the video reference's own always-visible
// "CONNECTED" status alongside the countdown.
subscribe((state) => {
  const pill = el("lobby-connection-pill");
  const connected = state.connection === "connected";
  pill.classList.toggle("connected", connected);
  el("lobby-connection-label").textContent = connected ? t("lobby.connected") : t("lobby.not_connected");
});

// --- game screen (RUNNING) ---------------------------------------------

// One entry per card the player holds this round -- a player can hold
// several (room.max_cards_per_player), each with its own independent
// createCard() instance and its own claim button, so marking or claiming
// one can never bleed into another (see render/card.js's factory
// conversion). { cardNo, instance, btn, claimed }.
let gameCards = [];

// Mirrors packages/core/bingo.py's own letter_for() range split -- kept
// this simple rather than importing/duplicating a shared table, since
// it's a fixed, permanent rule (spec: B 1-15, I 16-30, N 31-45, G 46-60,
// O 61-75) with nowhere else in this file that already needs it.
function letterForNumber(n) {
  return ["B", "I", "N", "G", "O"][Math.floor((n - 1) / 15)];
}

// A real gap: the big current-ball circle and its trailing recent-calls
// only ever got set by a live "call" WS event, never restored from
// state_sync -- so a player who reconnects, or joins a room mid-round as
// a spectator, saw a permanently blank circle until the *next* number
// happened to be called, even though the board and call count both
// already correctly showed the round was well underway. sync.called is
// already in call order, so its own tail is exactly "the current ball,
// then the trail behind it" -- the same information a live call event
// carries, just read from the reconnect snapshot instead of the wire.
// Shared by enterGame() and enterSpectate(), the two screens that can
// each be reached either by watching a live transition (called === [],
// correctly leaves the badge blank until the first real call) or by
// joining/reconnecting mid-round (called already has entries).
function restoreCallBadge(called) {
  if (called.length > 0) {
    const current = called[called.length - 1];
    const badge = el("call-badge");
    badge.textContent = `${letterForNumber(current)}${current}`;
    badge.dataset.letter = letterForNumber(current);
    badge.classList.add("show");
  }
  el("recent-calls").innerHTML = "";
  for (const n of called.slice(-4, -1)) {
    pushRecentCall({ letter: letterForNumber(n), number: n });
  }
}

function enterGame(sync) {
  showScreen("game");
  board.buildBoard(el("board"));
  // your_cards is the real, complete list; your_card/your_card_grid stay
  // as a fallback purely for a state_sync payload from before this synced
  // (shouldn't happen post-deploy, but costs nothing to tolerate).
  const yourCards =
    sync.your_cards && sync.your_cards.length > 0
      ? sync.your_cards
      : sync.your_card_grid
        ? [{ card_no: sync.your_card, grid: sync.your_card_grid, auto_mark: sync.auto_mark }]
        : [];
  buildGameCards(yourCards);
  board.setYourCardNumbers(yourCards.flatMap((c) => gridNumbers(c.grid)));
  board.markAllCalled(sync.called || []);
  const calledSet = new Set(sync.called || []);
  for (const gc of gameCards) gc.instance.markCalled(calledSet);
  updateStatStrip(sync);
  updateBingoButtons(calledSet);
  restoreCallBadge(sync.called || []);

  const autoOn = sync.auto_mark !== false;
  setState({ autoMark: autoOn });
  el("auto-switch").classList.toggle("on", autoOn);
  el("auto-switch").setAttribute("aria-checked", String(autoOn));
}

function buildGameCards(yourCards) {
  const list = el("your-cards-list");
  list.innerHTML = "";
  gameCards = yourCards.map((c) => {
    const item = document.createElement("div");
    item.className = "your-card-item";

    const cardEl = document.createElement("div");
    item.appendChild(cardEl);

    // Card number reads BELOW its own grid, not above -- matches the
    // reference layout's own "Card #N" placement.
    const title = document.createElement("div");
    title.className = "your-card-title";
    title.textContent = t("game.your_card_no", { card: c.card_no });
    item.appendChild(title);

    const btn = document.createElement("button");
    btn.className = "btn-primary bingo-btn";
    btn.disabled = true;
    btn.textContent = t("game.bingo");
    item.appendChild(btn);

    list.appendChild(item);

    const instance = card.createCard(cardEl);
    instance.setGrid(c.grid);
    instance.onCellClick(() => {
      if (getState().autoMark) return;
      // Optimistic local mark only -- the server never trusts this; it
      // always recomputes from its own called-numbers set (spec principle 1).
      haptics.lightTap();
    });

    const entry = { cardNo: c.card_no, instance, btn, claimed: false };
    btn.addEventListener("click", () => {
      const state = getState();
      if (btn.disabled || !state.round || !state.round.round_id) return;
      entry.claimed = true;
      ws.claim(state.round.round_id, entry.cardNo);
    });
    return entry;
  });
}

function gridNumbers(grid) {
  const numbers = [];
  for (const row of grid) for (const v of row) if (v !== 0) numbers.push(v);
  return numbers;
}

function updateStatStrip(sync) {
  // Was `sync.derash || sync.pot || "0.00"`: every server message that
  // reaches this function always sends derash as a non-empty string
  // ("0.00" included), and "0.00" is truthy in JS -- so the `|| sync.pot`
  // fallback could never actually fire, dead code masking a real
  // server-side bug (state_sync used to always send "0.00" for any round
  // still in progress; fixed in services/gateway/queries.py::
  // build_state_sync). Kept as `??`, not `||`, so a genuinely missing
  // field still falls back safely without ever substituting `pot` (a
  // different, larger number -- the gross stake, not the payout) mislabeled
  // as derash.
  el("stat-derash").textContent = `${sync.derash ?? "0.00"} ETB`;
  el("stat-players").textContent = String(sync.players || "");
  el("stat-stake").textContent = `${sync.stake || ""} ETB`;
  el("stat-call").textContent = `${sync.call_index || 0}/75`;
}

ws.on("round_start", (sync) => {
  // One merge, reused everywhere below -- round_start's own payload
  // doesn't carry `stake` (only round.js/state_sync do), and `called`
  // must actually reset to [] for the new round, not just look reset to
  // whatever enterGame() was locally handed. Splitting these into
  // separate ad-hoc merges previously left the stat strip's stake blank
  // after the first round_start of a session, and left state.round.called
  // stale across rounds (caught by review of a real E2E screenshot, not
  // by a unit test -- see DECISIONS.md).
  const merged = { ...getState().round, ...sync, called: [] };
  setState({ round: merged });
  voiceCaller.resetRound();
  if (getState().screen === "lobby" || getState().screen === "game") {
    if (merged.your_cards && merged.your_cards.length > 0) enterGame(merged);
    else enterSpectate(merged);
  }
  updateStatStrip(merged);
});

ws.on("call", (msg) => {
  const state = getState();
  if (state.screen !== "game" && state.screen !== "lobby") return;
  // Defense in depth alongside the subscription-lifecycle fix in
  // showScreen() above: a call belongs to exactly one round, so if this
  // connection is (or ever again becomes, from some future regression)
  // subscribed to more than one room at once, a stray round's own calls
  // must never be applied to whatever round is actually on screen.
  if (!state.round || msg.round_id !== state.round.round_id) return;
  board.markCalled(msg.number);
  const calledSoFar = new Set([...(state.round ? state.round.called || [] : []), msg.number]);
  setState({ round: { ...state.round, called: [...calledSoFar], call_index: msg.index } });
  for (const gc of gameCards) gc.instance.markCalled(calledSoFar);

  const badge = el("call-badge");
  // The ball that was current until just now becomes the newest entry in
  // the recent-calls trail -- matches the reference layout, where the
  // small trailing balls are strictly *behind* the current one, never a
  // duplicate of it (a real mismatch this used to have: pushRecentCall()
  // was called with this same new call, showing it in both places at
  // once).
  if (badge.textContent) {
    pushRecentCall({ letter: badge.dataset.letter, number: badge.textContent.slice(1) });
  }
  badge.textContent = `${msg.letter}${msg.number}`;
  badge.dataset.letter = msg.letter;
  badge.classList.remove("show");
  void badge.offsetWidth; // restart the CSS animation
  badge.classList.add("show");
  haptics.mediumTap();
  voiceCaller.announce(msg.letter, msg.number, msg.index);

  el("stat-call").textContent = `${msg.index}/75`;
  updateBingoButtons(calledSoFar);
});

function pushRecentCall(msg) {
  const recent = el("recent-calls");
  const chip = document.createElement("span");
  chip.textContent = `${msg.letter}${msg.number}`;
  chip.dataset.letter = msg.letter;
  recent.insertBefore(chip, recent.firstChild);
  while (recent.children.length > 3) recent.removeChild(recent.lastChild);
}

// Each held card's completion/claim state is independent -- a false claim
// or an already-claimed card never disables another card's own button,
// matching the reference's per-card claim buttons and the engine's
// per-card lockout (round_engine.py's claim()).
function updateBingoButtons(calledSet) {
  const state = getState();
  for (const gc of gameCards) {
    const complete = gc.instance.hasCompletePattern(calledSet, winPatterns, minWinningLines);
    gc.btn.disabled = !complete || gc.claimed;
    if (state.autoMark && complete && !gc.claimed && state.round && state.round.round_id) {
      gc.claimed = true;
      ws.claim(state.round.round_id, gc.cardNo);
    }
  }
}

// role="switch" (not the generic "button" makeKeyboardActivatable
// defaults to), since this is a real on/off toggle -- set before calling
// it, which the helper takes as "already has a role, don't override".
el("auto-switch").setAttribute("role", "switch");
el("auto-switch").setAttribute("aria-checked", "true"); // matches index.html's own static class="switch on" default
makeKeyboardActivatable(el("auto-switch"), () => {
  const state = getState();
  const next = !state.autoMark;
  setState({ autoMark: next });
  el("auto-switch").classList.toggle("on", next);
  el("auto-switch").setAttribute("aria-checked", String(next));
  if (state.currentRoomId !== null) ws.setAuto(state.currentRoomId, next);
});

// --- voice caller settings -------------------------------------------
// localStorage, not the server-synced pattern auto_mark uses above --
// this is a per-device audio-playback preference with zero gameplay
// impact (a player reasonably wants different volume on phone vs.
// desktop), so there's no server-side consumer for it to round-trip
// through. See DECISIONS.md for why this is a deliberate exception.

const VOICE_STORAGE_KEYS = {
  enabled: "jobingo_voice_enabled",
  volume: "jobingo_voice_volume",
  speed: "jobingo_voice_speed",
};

function loadVoiceSettings() {
  try {
    const enabled = localStorage.getItem(VOICE_STORAGE_KEYS.enabled);
    const volume = localStorage.getItem(VOICE_STORAGE_KEYS.volume);
    const speed = localStorage.getItem(VOICE_STORAGE_KEYS.speed);
    if (enabled !== null) voiceCaller.setEnabled(enabled === "true");
    if (volume !== null) voiceCaller.setVolume(parseFloat(volume));
    if (speed !== null) voiceCaller.setSpeed(parseFloat(speed));
  } catch {
    // Private-mode/unavailable localStorage -- voice defaults stand,
    // never blocks boot.
  }
}

function saveVoiceSetting(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Best-effort; nothing to recover from here.
  }
}

function syncVoiceToggleUI() {
  const on = voiceCaller.isEnabled();
  for (const id of ["voice-switch", "voice-switch-settings"]) {
    const node = el(id);
    node.classList.toggle("on", on);
    node.setAttribute("aria-checked", String(on));
  }
}

function setVoiceEnabled(next) {
  voiceCaller.setEnabled(next);
  saveVoiceSetting(VOICE_STORAGE_KEYS.enabled, next);
  syncVoiceToggleUI();
}

loadVoiceSettings();

for (const id of ["voice-switch", "voice-switch-settings"]) {
  el(id).setAttribute("role", "switch");
  makeKeyboardActivatable(el(id), () => setVoiceEnabled(!voiceCaller.isEnabled()));
}
syncVoiceToggleUI();

el("voice-volume-slider").value = String(Math.round(voiceCaller.getVolume() * 100));
el("voice-volume-slider").addEventListener("input", () => {
  const volume = Number(el("voice-volume-slider").value) / 100;
  voiceCaller.setVolume(volume);
  saveVoiceSetting(VOICE_STORAGE_KEYS.volume, volume);
});

el("voice-speed-slider").value = String(Math.round(voiceCaller.getSpeed() * 100));
el("voice-speed-slider").addEventListener("input", () => {
  const speed = Number(el("voice-speed-slider").value) / 100;
  voiceCaller.setSpeed(speed);
  saveVoiceSetting(VOICE_STORAGE_KEYS.speed, speed);
});

el("voice-replay-btn").addEventListener("click", () => voiceCaller.replayLast());

el("voice-settings-btn").addEventListener("click", () => {
  el("voice-settings-panel").classList.toggle("hidden");
});

ws.on("claim_result", (msg) => {
  if (msg.valid) return; // the eventual round_end message drives the UI
  // card_no alone isn't unique across rooms -- every room deals from the
  // same shared 1..card_pool_size range independently, so a false-claim
  // reply that arrives late (already in flight when this connection left
  // that room) could otherwise match an identically-numbered card
  // genuinely held in whatever room the player has since switched to. The
  // round this claim was actually for rides along in msg.round_id (see
  // services/gateway/connection.py's claim dispatch), gating this exactly
  // like ws.on("call")'s own round-identity guard above.
  const state = getState();
  if (!state.round || msg.round_id !== state.round.round_id) return;
  // card_no targets exactly the card that was claimed -- without it, a
  // false claim on one card would reset/shake every held card's button
  // instead of just the one that was actually wrong.
  const gc = gameCards.find((c) => c.cardNo === msg.card_no);
  if (!gc) return;
  gc.claimed = false;
  gc.btn.classList.remove("shake");
  void gc.btn.offsetWidth;
  gc.btn.classList.add("shake");
  haptics.warning();
  showToast("game.no_pattern_yet");
});

// --- spectate ------------------------------------------------------------

function enterSpectate(sync) {
  showScreen("game");
  el("spectate-banner").classList.remove("hidden");
  el("your-card-section").classList.add("hidden");
  board.buildBoard(el("board"));
  board.markAllCalled(sync.called || []);
  updateStatStrip(sync);
  restoreCallBadge(sync.called || []);
  renderAnnouncementMarquee();
}

// Replaces the old "Reserve a card" button (services/gateway/app.py's
// /api/announcement is the admin-configurable source, see
// announcement_queries.py) -- that button re-synced the whole spectate
// view for zero real benefit, since join() can't assign a card mid-round
// and the player auto-advances into the next round regardless. Fetched
// once at boot, not per spectate entry: this text rarely changes within
// a single short-lived Mini App session, and re-fetching on every
// spectate entry would just be another pointless round trip.
let announcementText = null;

async function fetchAnnouncement() {
  try {
    const response = await fetch("/api/announcement", { headers: authHeader() });
    if (!response.ok) return;
    const data = await response.json();
    announcementText = data.text || null;
  } catch {
    /* no banner shown -- not critical to any real functionality */
  }
}

function renderAnnouncementMarquee() {
  const marquee = el("announcement-marquee");
  if (!announcementText) {
    marquee.classList.add("hidden");
    return;
  }
  el("announcement-marquee-text").textContent = announcementText;
  el("announcement-marquee-text-repeat").textContent = announcementText;
  marquee.classList.remove("hidden");
}

// --- invite / referral ----------------------------------------------------
//
// services/bot/handlers.py::cmd_invite() already sends this same
// ?start=ref_{telegram_id} link + join count over chat, but only to a
// player who thinks to type /invite there -- this is the same data, one
// tap away, on the screen every player actually spends their time on.
// Sharing goes through Telegram's own native share sheet (t.me/share/url,
// opened via tg.openTelegramLink) rather than a raw copy-paste link: the
// resulting message uses Telegram's own preview of the bot's profile
// (name/photo/short description), so it looks like a real invite, not a
// pasted URL -- one tap fans out to any chat, group, or channel the
// player picks, with zero server-side involvement in the fan-out itself.
let inviteLink = null;
let inviteReferralCount = 0;

async function fetchInviteSummary() {
  try {
    const response = await fetch("/api/invite", { headers: authHeader() });
    if (!response.ok) return;
    const data = await response.json();
    inviteLink = data.link || null;
    inviteReferralCount = data.referral_count || 0;
  } catch {
    /* invite panel just shows nothing to share -- not critical to any real functionality */
  }
}

el("invite-btn").addEventListener("click", async () => {
  haptics.lightTap();
  const panel = el("invite-panel");
  const opening = panel.classList.contains("hidden");
  panel.classList.toggle("hidden");
  if (opening) {
    // Re-fetched on every open, unlike the announcement text above -- a
    // friend joining via this exact link is the one thing this panel
    // exists to show, and it can genuinely change between opens within
    // the same session.
    await fetchInviteSummary();
    el("invite-count").textContent = t("invite.count", { count: inviteReferralCount });
  }
});

el("invite-share-btn").addEventListener("click", () => {
  if (!inviteLink) {
    showToast("invite.no_link");
    return;
  }
  haptics.mediumTap();
  const shareUrl =
    `https://t.me/share/url?url=${encodeURIComponent(inviteLink)}` +
    `&text=${encodeURIComponent(t("invite.share_text"))}`;
  if (tg && tg.openTelegramLink) {
    tg.openTelegramLink(shareUrl);
  } else {
    window.open(shareUrl, "_blank");
  }
});

// A real production incident, caught on video: a player who took a card
// during a lobby that then failed to fill (too few players) saw the
// countdown hit 0 and just freeze there forever -- nothing was telling an
// already-connected client the round had voided (every OTHER termination
// path broadcasts round_end; this was the one silent one, now fixed
// server-side too). Mirrors state_sync's own "voided"/"done" handling: a
// clean bounce back to the room list, not a misleading result screen
// (unlike round_end, this round never actually started -- there's no
// "no winner" to report, just "not enough players joined").
ws.on("round_voided", (msg) => {
  // Same round-identity guard as ws.on("call")/ws.on("state_sync") above:
  // this connection should only ever be subscribed to the room currently
  // on screen, but a stray broadcast for a room already navigated away
  // from can still be in flight when it lands. Without this, a voided
  // notice for an abandoned room would bounce the player off a different
  // room they're actually still watching live.
  if (!getState().round || msg.round_id !== getState().round.round_id) return;
  showToast("lobby.round_voided_underfilled");
  showScreen("rooms");
  refreshRoomList();
});

// --- result (SETTLING) --------------------------------------------------

ws.on("round_end", (msg) => {
  // Same round-identity guard as ws.on("call")/ws.on("state_sync")/
  // ws.on("round_voided") above -- a stray round_end for a room this
  // connection has already left (but whose broadcast was already in
  // flight) would otherwise render the wrong room's winners/payout onto
  // the result screen and corrupt the real-money session reality-check
  // total below with someone else's round outcome.
  const preState = getState();
  if (!preState.round || msg.round_id !== preState.round.round_id) return;
  el("spectate-banner").classList.add("hidden");
  el("your-card-section").classList.remove("hidden");
  el("fairness-panel").classList.add("hidden");
  setState({ lastResult: msg });

  const state = getState();
  const userId = state.user ? state.user.id : null;
  // A player can win on more than one of their own cards in the same
  // round now -- summing every one of their own winning entries (not
  // just the first) is what keeps the reality-check total and the
  // result screen's own amount correct once that happens.
  const myWins = (msg.winners || []).filter((w) => w.user_id === userId);
  const stake = state.round ? state.round.stake : "0";
  // Spectators (joined with no card) never staked this round -- don't
  // touch the reality-check total for them.
  const participated = Boolean(state.round && state.round.your_cards && state.round.your_cards.length > 0);

  if (participated) {
    // A losing player can hold more than one card now -- every one of
    // them staked real money this round, so a flat single `stake` here
    // (the pre-multi-card assumption) silently under-reported the real
    // loss by a factor of however many cards they held. Mirrors the
    // win-side fix just above: settlement is all-or-nothing per round,
    // so "no winning card of mine" means every one of your_cards' own
    // stakes was genuinely lost, not just one.
    const delta =
      myWins.length > 0
        ? myWins.reduce((sum, w) => sum + parseFloat(w.amount), 0)
        : (msg.winners || []).length > 0
          ? -parseFloat(stake) * state.round.your_cards.length
          : 0; // no winner: every stake was refunded, net zero
    setState({ sessionNetPosition: state.sessionNetPosition + delta });
  }

  showScreen("result");
  el("result-confetti").innerHTML = "";
  const pill = el("result-winner-pill");
  pill.classList.add("hidden");
  const shown = myWins[0] || (msg.winners || [])[0] || null;
  if (myWins.length > 0) {
    const totalAmount = myWins.reduce((sum, w) => sum + parseFloat(w.amount), 0);
    el("result-title").textContent = t("result.win_title");
    el("result-title").classList.add("win");
    pill.textContent = (state.user && state.user.name) || t("result.you");
    pill.classList.remove("hidden");
    el("result-amount").textContent = `+ ${totalAmount.toFixed(2)} ETB`;
    el("result-amount").classList.add("win");
    el("result-meta").textContent = myWins
      .map((w) => t("result.card_row", { card: w.card_no, pattern: w.pattern }))
      .join(" · ");
    spawnConfetti();
    haptics.success();
  } else if (shown) {
    // A winner identifier, not just a bare amount -- every other player
    // in the room previously only ever saw "someone won this much,"
    // with no sense of who. display_name is the same public identity
    // this codebase already shows a player to everyone else (admin
    // console, bot messages), not new exposure. Pulled out of the title
    // into its own pill so both "you won" and "they won" share the same
    // celebratory heading + identity-badge layout.
    el("result-title").textContent = t("result.round_winner_title");
    el("result-title").classList.remove("win");
    if (shown.display_name) {
      pill.textContent = t("result.other_winner", { name: shown.display_name });
      pill.classList.remove("hidden");
    }
    el("result-amount").textContent = `${shown.amount} ETB`;
    el("result-amount").classList.remove("win");
    el("result-meta").textContent = t("result.card_row", { card: shown.card_no, pattern: shown.pattern });
  } else {
    el("result-title").textContent = "";
    el("result-title").classList.remove("win");
    el("result-amount").textContent = t("result.no_winner");
    el("result-amount").classList.remove("win");
    el("result-meta").textContent = "";
  }

  // The actual winning card, not just a text summary -- spec: "Render a
  // proper Bingo card preview," winning cells strongly highlighted.
  // Every winner in this broadcast carries their own grid (round_engine
  // .py's already-in-memory card_pool), so this works identically for
  // "I won" and "someone else won" -- no separate code path needed.
  if (shown && shown.grid) {
    card.renderStaticCard(el("result-card"), shown.grid, state.round ? state.round.called : [], shown.pattern);
    el("result-card-panel").classList.remove("hidden");
  } else {
    el("result-card-panel").classList.add("hidden");
  }
  renderSessionTotal();
  startAutoContinue();
});

// Reality check (spec section 12): "net position this session, shown
// plainly" -- right on the results screen, every time, not buried in the
// wallet where a losing player has no reason to go look for it.
function renderSessionTotal() {
  const total = getState().sessionNetPosition;
  const node = el("result-session");
  const sign = total > 0 ? "+" : total < 0 ? "-" : "";
  node.textContent = t("result.session_total", { sign, amount: Math.abs(total).toFixed(2) });
  node.classList.remove("positive", "negative");
  if (total > 0) node.classList.add("positive");
  else if (total < 0) node.classList.add("negative");
}

// Purely decorative -- a handful of falling rectangles in the B/I/N/G/O
// column colors, cleared and rebuilt fresh at the top of every round_end
// (see above) so a loss right after a win never inherits stale pieces.
function spawnConfetti() {
  const container = el("result-confetti");
  const colors = ["var(--col-b)", "var(--col-i)", "var(--col-n)", "var(--col-g)", "var(--col-o)"];
  for (let i = 0; i < 28; i++) {
    const piece = document.createElement("i");
    piece.style.left = `${Math.random() * 100}%`;
    piece.style.background = colors[i % colors.length];
    piece.style.animationDuration = `${1.4 + Math.random() * 1.1}s`;
    piece.style.animationDelay = `${Math.random() * 0.35}s`;
    container.appendChild(piece);
  }
}

// Auto-return to the lobby a fixed number of seconds after any round
// result (win, someone-else-won, or no-winner) -- today nothing ever
// moves a player off this screen except their own tap, which is exactly
// the kind of "player can get stuck" gap the round-lifecycle hardening
// pass was about. This is purely a local UI timer with no claim about
// real server timing: it just performs the same real re-sync a manual
// tap does (goToNextRound() -> enterRoom() -> ws.joinRoom()), landing
// wherever the server's round genuinely is by the time it fires.
const AUTO_CONTINUE_SECONDS = 10;
let autoContinueInterval = null;

function stopAutoContinue() {
  if (autoContinueInterval) {
    clearInterval(autoContinueInterval);
    autoContinueInterval = null;
  }
  el("result-autocontinue").classList.add("hidden");
}

function startAutoContinue() {
  stopAutoContinue();
  const bar = el("result-autocontinue");
  const fill = el("result-autocontinue-fill");
  const label = el("result-autocontinue-label");
  bar.classList.remove("hidden");
  // Restart the CSS animation from a clean state each time this screen
  // shows -- reusing the same element without this reset would just keep
  // whatever scale the previous round's animation ended on.
  fill.style.animation = "none";
  void fill.offsetWidth;
  fill.style.animation = `result-autocontinue-shrink ${AUTO_CONTINUE_SECONDS}s linear forwards`;

  let secondsLeft = AUTO_CONTINUE_SECONDS;
  label.textContent = t("result.autocontinue", { seconds: secondsLeft });
  autoContinueInterval = setInterval(() => {
    if (getState().screen !== "result") {
      stopAutoContinue();
      return;
    }
    secondsLeft -= 1;
    if (secondsLeft <= 0) {
      stopAutoContinue();
      goToNextRound();
      return;
    }
    label.textContent = t("result.autocontinue", { seconds: secondsLeft });
  }, 1000);
}

function goToNextRound() {
  const state = getState();
  if (state.currentRoomId !== null) enterRoom(state.currentRoomId);
  else showScreen("rooms");
}

el("play-next-btn").addEventListener("click", () => {
  stopAutoContinue();
  goToNextRound();
});

// --- provably-fair verification ------------------------------------------
// Spec section 14 (definition of done): "a player can independently verify
// any round's draw from the published seed." The server_seed is only ever
// revealed once a round is terminal (round_engine.py persists it at that
// point, not before -- see DECISIONS.md, Phase 7); this panel is that
// reveal, plus the app's own recomputation of it, surfaced to the player.

el("verify-draw-btn").addEventListener("click", async () => {
  const result = getState().lastResult;
  const panel = el("fairness-panel");
  if (!result || result.round_id == null) return;

  // A player actively reading the fairness reveal shouldn't get yanked
  // back to the lobby out from under them by the same countdown that
  // otherwise auto-advances an idle result screen.
  stopAutoContinue();
  el("fairness-verified").textContent = "";
  el("fairness-hash").textContent = "";
  el("fairness-seed").textContent = "";
  panel.classList.remove("hidden");

  try {
    const response = await fetch(`/api/rounds/${result.round_id}/fairness`, { headers: authHeader() });
    if (!response.ok) {
      el("fairness-verified").textContent = t("fairness.error");
      return;
    }
    const data = await response.json();
    if (!data.revealed) {
      el("fairness-verified").textContent = t("fairness.not_yet");
      el("fairness-hash").textContent = data.server_seed_hash || "";
      return;
    }
    const verifiedEl = el("fairness-verified");
    verifiedEl.textContent = data.verified ? t("fairness.yes") : t("fairness.no");
    verifiedEl.className = data.verified ? "verified-yes" : "verified-no";
    el("fairness-hash").textContent = data.server_seed_hash;
    el("fairness-seed").textContent = data.server_seed;
  } catch {
    el("fairness-verified").textContent = t("fairness.error");
  }
});

el("fairness-close-btn").addEventListener("click", () => {
  el("fairness-panel").classList.add("hidden");
});

// --- wallet --------------------------------------------------------------

// Real reported bug: text on the wallet's Telebirr destination card was
// unreadable in a real Telegram client, even though it renders correctly
// here (this file's own stub tests always send an empty themeParams, so
// none of this code path ever ran against them). Root cause: Telegram's
// own theme_params object is not guaranteed to supply every color field
// together -- a client that sends a light bg_color with no matching
// text_color left this function's old code silently keeping the dark-
// mode default (near-white) text color in place on top of a newly light
// background. Computing a guaranteed-contrasting fallback from whichever
// background color actually won closes this regardless of which fields
// a given Telegram client happens to send.
function relativeLuminance(hexColor) {
  const hex = hexColor.replace("#", "");
  const channels = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastingTextColor(bgHexColor) {
  try {
    // This app's own light/dark text tokens (tokens.css) -- reusing them
    // keeps a themed wallet screen's fallback text on-brand instead of a
    // generic black/white.
    return relativeLuminance(bgHexColor) > 0.5 ? "#0B0E14" : "#F2F5FA";
  } catch {
    return "#F2F5FA"; // an unparseable color is vanishingly unlikely (Telegram always sends #rrggbb), but never crash the wallet over it
  }
}

function applyWalletTheme() {
  const wallet = el("screen-wallet");
  if (!tg || !tg.themeParams) return;
  const p = tg.themeParams;
  if (p.bg_color) wallet.style.setProperty("--bg", p.bg_color);
  if (p.secondary_bg_color) wallet.style.setProperty("--surface", p.secondary_bg_color);
  const bgForContrast = p.bg_color || p.secondary_bg_color;
  const fallbackText = bgForContrast ? contrastingTextColor(bgForContrast) : null;
  if (p.text_color) {
    wallet.style.setProperty("--text", p.text_color);
  } else if (fallbackText) {
    wallet.style.setProperty("--text", fallbackText);
  }
  if (p.hint_color) {
    wallet.style.setProperty("--muted", p.hint_color);
  } else if (fallbackText) {
    wallet.style.setProperty("--muted", fallbackText);
  }
  if (p.button_color) wallet.style.setProperty("--accent", p.button_color);
}

// The three balance figures the server ever sends (cash/bonus/locked) are
// always rendered as-is, verbatim strings straight from the backend --
// never recomputed here. Total/available are a *display-only* sum of
// those same three authoritative numbers (matching packages/core/ledger
// .py's own available() -- (cash+bonus)-locked -- exactly), computed
// fresh every time this runs so they can never drift from what the
// breakdown row right below them shows. This function is the one place
// all three call sites (initial open, a Telebirr redemption's own
// refresh, and the live balance_update push) update the balance UI, so a
// future field can never be added to just one of them by mistake.
function renderWalletBalances(data) {
  el("wallet-cash").textContent = `${data.cash} ETB`;
  el("wallet-bonus").textContent = `${data.bonus} ETB`;
  el("wallet-locked").textContent = `${data.locked} ETB`;

  const cash = Number(data.cash) || 0;
  const bonus = Number(data.bonus) || 0;
  const locked = Number(data.locked) || 0;
  const total = cash + bonus + locked;
  const available = cash + bonus - locked;
  el("wallet-total").textContent = `${total.toFixed(2)} ETB`;
  el("wallet-available").textContent = `${available.toFixed(2)} ETB`;

  const pill = el("wallet-status-toggle");
  const isActive = locked <= 0;
  el("wallet-available-status").textContent = t(isActive ? "wallet.status_active" : "wallet.status_locked");
  pill.classList.toggle("status-locked", !isActive);
}

function toggleWalletBreakdown() {
  const breakdown = el("wallet-breakdown");
  const expanded = breakdown.classList.toggle("hidden") === false;
  el("wallet-status-toggle").setAttribute("aria-expanded", String(expanded));
}

async function openWallet() {
  showScreen("wallet");
  applyWalletTheme();
  try {
    const response = await fetch("/api/me", { headers: authHeader() });
    if (response.ok) {
      renderWalletBalances(await response.json());
    }
  } catch {
    /* wallet screen just shows whatever it already had */
  }
  await applyPaymentAvailability();
}

// P1: the backend, not this file, decides which rail is live --
// payment_provider_availability, read fresh every time the wallet
// opens, so an admin flipping a toggle takes effect for the very next
// player who opens their wallet, not just on a future deploy.
// Set by applyPaymentAvailability() below -- which of the two possible
// "already open by default" deposit sections is currently showing
// (automatic/Chapa, or Telebirr when Chapa is off), so the shared
// manual/automatic toggle handlers further down know which one to hide
// or restore without hardcoding an assumption that automatic is always
// the default.
let depositDefaultIsTelebirr = false;
let telebirrDestinationLoaded = false;

// Product decision (2026-09-14, DECISIONS.md): Manual Deposit is no
// longer offered as a player-visible *choice* for deposits -- only as an
// absolute last resort if neither automatic (Chapa) nor Telebirr is
// available at all, so a real-money system is never left with zero
// deposit path over a UI decision. Flip this back to `false` to restore
// it as a normal, always-reachable option (its own code, destinations,
// and backend are all untouched either way -- this only ever controls
// whether the deposit tab's toggle button/section for it can appear).
const DEPOSIT_MANUAL_HIDDEN_FROM_UI = true;

async function applyPaymentAvailability() {
  try {
    const response = await fetch("/api/payment-methods", { headers: authHeader() });
    if (!response.ok) return;
    const methods = await response.json();

    const depositHasAutomatic = methods.deposit.includes("chapa");
    const depositHasManual = methods.deposit.includes("manual") && !DEPOSIT_MANUAL_HIDDEN_FROM_UI;
    const depositManualIsLastResort = methods.deposit.includes("manual") && DEPOSIT_MANUAL_HIDDEN_FROM_UI;
    // Telebirr SMS-evidence deposits: instant, reference-only, no admin
    // review -- ships disabled by default (payment_provider_availability
    // seeds it off), so this is a no-op until an admin turns it on.
    // Whenever it's on and chapa (the other fully-automatic rail) is off,
    // it takes over as the default-open section instead of Manual
    // Deposit -- real production confusion (DECISIONS.md, 2026-09-14)
    // came from Telebirr and Manual sitting side by side with equal
    // visual weight, so a player heading straight for their Telebirr
    // payment kept landing in the always-review Manual form instead.
    const depositHasTelebirr = methods.deposit.includes("telebirr_sms");
    depositDefaultIsTelebirr = !depositHasAutomatic && depositHasTelebirr;

    if (!depositHasAutomatic) {
      el("deposit-automatic-section").classList.add("hidden");
      el("deposit-automatic-toggle-btn").classList.add("hidden");
      el("deposit-telebirr-toggle-btn").classList.add("hidden"); // reached directly below, not via a toggle

      if (depositDefaultIsTelebirr) {
        el("deposit-telebirr-section").classList.remove("hidden");
        el("deposit-manual-section").classList.add("hidden");
        el("deposit-manual-toggle-btn").classList.toggle("hidden", !depositHasManual);
        // Nothing to go "back" to -- Telebirr is the permanent default
        // page here (Chapa is off, and Manual is hidden per the product
        // decision above), not something reached by toggling away from
        // another section, so a "Back" button pointing nowhere real
        // would just be clutter on what's meant to be one clean page.
        el("deposit-telebirr-back-btn").classList.add("hidden");
        if (!telebirrDestinationLoaded) {
          telebirrDestinationLoaded = true;
          await loadTelebirrDestination();
        }
      } else if (depositHasManual || depositManualIsLastResort) {
        // Nothing better to toggle *from* -- go straight to the manual
        // panel, permanently, rather than showing a toggle button that
        // would only ever lead to a dead end. Reached even when Manual
        // is otherwise hidden from the UI (DEPOSIT_MANUAL_HIDDEN_FROM_UI)
        // -- a real-money system should never show "not available" when
        // a real, configured deposit path still exists, even one that's
        // no longer offered as a first-choice option.
        el("deposit-manual-toggle-btn").classList.add("hidden");
        el("deposit-manual-section").classList.remove("hidden");
        if (!manualDestinationsLoaded) {
          manualDestinationsLoaded = true;
          await loadManualDestinations();
        }
      } else {
        el("deposit-manual-section").classList.add("hidden");
        setWalletStatus("deposit-status", "wallet.not_available", "error");
      }
    } else {
      // Chapa is on and stays the default -- Telebirr is reached via its
      // own toggle button, unchanged from before. Manual's own toggle
      // stays suppressed per DEPOSIT_MANUAL_HIDDEN_FROM_UI (chapa being
      // on means it's never the only path anyway, so there's no
      // last-resort case to fall back to here).
      el("deposit-telebirr-toggle-btn").classList.toggle("hidden", !depositHasTelebirr);
      if (!depositHasManual) {
        // Automatic works but manual doesn't (or isn't configured) --
        // never offer a toggle to a dead end.
        el("deposit-manual-toggle-btn").classList.add("hidden");
      }
    }

    const withdrawHasAutomatic = methods.withdraw.includes("chapa");
    const withdrawHasManual = methods.withdraw.includes("manual");
    const manualToggleRow = el("withdraw-manual-toggle-row");
    if (!withdrawHasAutomatic && withdrawHasManual) {
      // Only manual works -- every withdrawal is manual regardless of
      // the checkbox, so lock it checked and hide the now-meaningless
      // choice rather than leave a togglable control with only one
      // real answer.
      el("withdraw-manual-checkbox").checked = true;
      el("withdraw-manual-checkbox").disabled = true;
      manualToggleRow.classList.add("hidden");
    } else if (!withdrawHasAutomatic && !withdrawHasManual) {
      setWalletStatus("withdraw-status", "wallet.not_available", "error");
      el("withdraw-submit-btn").disabled = true;
    } else if (!withdrawHasManual) {
      manualToggleRow.classList.add("hidden");
    }
  } catch {
    /* wallet screen just shows whatever it already had -- the submit
       handlers' own error paths still catch a genuinely unavailable
       provider server-side either way. */
  }
}

function authHeader() {
  const raw = tg ? tg.initData : "";
  return raw ? { Authorization: `tma ${raw}` } : {};
}

function setWalletStatus(id, key, kind) {
  const node = el(id);
  node.textContent = key ? t(key) : "";
  node.classList.remove("error", "success");
  if (kind) node.classList.add(kind);
}

// Shared by the tab-click handler below and by boot()'s own deep-link
// check (a player tapping "Deposit" in the bot's menu should land
// directly on this tab, not on Balance with one more tap still needed)
// -- one place decides what "switch to tab X" actually does, so a future
// tab can never work correctly from a click but not from a deep link.
function switchToWalletTab(tabName) {
  document.querySelectorAll(".wallet-tab").forEach((tabEl) => {
    tabEl.classList.toggle("active", tabEl.dataset.tab === tabName);
  });
  document.querySelectorAll(".wallet-pane").forEach((pane) => pane.classList.add("hidden"));
  el(`wallet-pane-${tabName}`).classList.remove("hidden");
  if (tabName === "history") loadHistory();
}

document.querySelectorAll(".wallet-tab").forEach((tabEl) => {
  tabEl.addEventListener("click", () => switchToWalletTab(tabEl.dataset.tab));
});

el("open-wallet-btn").addEventListener("click", openWallet);
el("wallet-back-btn").addEventListener("click", () => showScreen("rooms"));
el("wallet-status-toggle").addEventListener("click", toggleWalletBreakdown);

// --- deposit ---------------------------------------------------------
// Spec 2.6: "On return: 'Confirming your deposit…' with live polling,
// never a premature success." Opening the checkout link is not a
// completion -- the player hasn't paid yet at that point -- so this
// tracks the deposit as pending and only ever calls it done once the
// real ledger credit actually lands. `balance_update` (already pushed
// live over this user's own Redis channel the moment
// services/payments/deposits.py posts the credit -- see the handler
// below) stands in for polling: genuinely live, and no new backend
// endpoint needed. Comparing the new cash figure against what it was
// before this specific deposit, by at least the deposited amount, is
// what keeps this honest against an unrelated balance_update (a
// same-session round settling, an admin adjustment) arriving while a
// deposit happens to be pending and getting mislabeled as this deposit's
// own confirmation.
let pendingDeposit = null;

document.querySelectorAll("#deposit-amount-chips .amount-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#deposit-amount-chips .amount-chip").forEach((c) => c.classList.remove("selected"));
    chip.classList.add("selected");
    el("deposit-amount-input").value = chip.dataset.amount;
  });
});

el("deposit-submit-btn").addEventListener("click", async () => {
  const amount = el("deposit-amount-input").value;
  if (!amount || Number(amount) <= 0) {
    setWalletStatus("deposit-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("deposit-submit-btn").disabled = true;
  setWalletStatus("deposit-status", "wallet.deposit_opening", null);
  try {
    const response = await fetch("/api/deposit", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ amount }),
    });
    if (response.status === 503) {
      setWalletStatus("deposit-status", "wallet.not_available", "error");
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("deposit-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    if (tg) tg.openLink(data.checkout_url);
    else window.open(data.checkout_url, "_blank");
    pendingDeposit = { amount: Number(amount), cashBefore: Number(getState().user?.balance ?? 0) };
    setWalletStatus("deposit-status", "wallet.deposit_confirming", null);
  } catch {
    setWalletStatus("deposit-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-submit-btn").disabled = false;
  }
});

// --- manual deposit (P1: keep taking deposits when the automatic
// provider is unavailable) -- a distinct panel, not another automatic
// -deposit field: the flow is genuinely different (pick a destination,
// pay externally, come back with a reference), not a variant of the
// same form.

let manualDestinationsLoaded = false;

el("deposit-manual-toggle-btn").addEventListener("click", async () => {
  // Hides whichever of the two is actually the current default --
  // normally automatic/Chapa, but Telebirr whenever Chapa is off and
  // Telebirr is on (see depositDefaultIsTelebirr, set by
  // applyPaymentAvailability()).
  el(depositDefaultIsTelebirr ? "deposit-telebirr-section" : "deposit-automatic-section").classList.add("hidden");
  el("deposit-manual-toggle-btn").classList.add("hidden");
  el("deposit-manual-section").classList.remove("hidden");
  el("deposit-automatic-toggle-btn").classList.remove("hidden");
  if (!manualDestinationsLoaded) {
    manualDestinationsLoaded = true;
    await loadManualDestinations();
  }
});

el("deposit-automatic-toggle-btn").addEventListener("click", async () => {
  el("deposit-manual-section").classList.add("hidden");
  el("deposit-automatic-toggle-btn").classList.add("hidden");
  if (depositDefaultIsTelebirr) {
    el("deposit-telebirr-section").classList.remove("hidden");
    if (!telebirrDestinationLoaded) {
      telebirrDestinationLoaded = true;
      await loadTelebirrDestination();
    }
  } else {
    el("deposit-automatic-section").classList.remove("hidden");
  }
  el("deposit-manual-toggle-btn").classList.remove("hidden");
});

// Telebirr SMS-evidence deposits -- a third, independent option. Whichever
// of the automatic/manual sections was actually showing when the player
// tapped into Telebirr is exactly what "back" should return them to, so
// this remembers it rather than assuming automatic is always the default.
let depositSectionBeforeTelebirr = "deposit-automatic-section";

el("deposit-telebirr-toggle-btn").addEventListener("click", async () => {
  depositSectionBeforeTelebirr = el("deposit-automatic-section").classList.contains("hidden")
    ? "deposit-manual-section"
    : "deposit-automatic-section";
  el(depositSectionBeforeTelebirr).classList.add("hidden");
  el("deposit-manual-toggle-btn").classList.add("hidden");
  el("deposit-telebirr-toggle-btn").classList.add("hidden");
  el("deposit-telebirr-section").classList.remove("hidden");
  // Real gap found alongside the color-contrast bug above: this specific
  // entry point (Telebirr reached as a secondary option, Chapa still on)
  // never actually called loadTelebirrDestination() at all -- unlike
  // every other path into this same section, which left the account
  // details permanently stuck on "loading" for anyone who got here via
  // this exact button.
  if (!telebirrDestinationLoaded) {
    telebirrDestinationLoaded = true;
    await loadTelebirrDestination();
  }
});

el("deposit-telebirr-back-btn").addEventListener("click", () => {
  el("deposit-telebirr-section").classList.add("hidden");
  el(depositSectionBeforeTelebirr).classList.remove("hidden");
  el("deposit-manual-toggle-btn").classList.remove("hidden");
  el("deposit-telebirr-toggle-btn").classList.remove("hidden");
});

// A player pasting the ENTIRE Telebirr confirmation SMS (not just the
// bare code) should work exactly as well as pasting only the reference
// -- less-technical players are far more likely to copy the whole
// message than to correctly select just one short code out of it. This
// mirrors services/payments/telebirr_parser.py's own server-side
// _REFERENCE_RE exactly (same anchor phrase, same character class, same
// length bounds) so client and server always agree on what counts as a
// reference -- if this ever finds a match, it's the same 10-character
// code the server's own parser already extracted from the real SMS when
// it first ingested it, not a new, independent guess.
function extractTelebirrReference(rawInput) {
  const match = rawInput.match(/transaction number is\s+([A-Za-z0-9]{6,20})/i);
  return match ? match[1] : rawInput.trim();
}

el("deposit-telebirr-submit-btn").addEventListener("click", async () => {
  const reference = extractTelebirrReference(el("deposit-telebirr-reference-input").value);
  if (!reference) {
    setWalletStatus("deposit-telebirr-status", "wallet.error.external_reference_required", "error");
    return;
  }
  el("deposit-telebirr-submit-btn").disabled = true;
  setWalletStatus("deposit-telebirr-status", "wallet.deposit_confirming", null);
  try {
    const response = await fetch("/api/wallet/deposits/telebirr/redeem", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ reference }),
    });
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("deposit-telebirr-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    // Section 130: never locally compute a new balance -- refresh the
    // authoritative wallet state from the server's own response (the
    // /api/me re-fetch just below), never data.amount added client-side.
    setWalletStatus("deposit-telebirr-status", "wallet.deposit_confirmed", "success");
    el("deposit-telebirr-reference-input").value = "";
    const meResponse = await fetch("/api/me", { headers: authHeader() });
    if (meResponse.ok) {
      renderWalletBalances(await meResponse.json());
    }
  } catch {
    setWalletStatus("deposit-telebirr-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-telebirr-submit-btn").disabled = false;
  }
});

// method_kind values a real admin can enter (services/admin/queries.py's
// create_manual_payment_destination_admin takes a free-ish string, not a
// closed enum) -- a plain bank emoji covers anything not in this small
// "recognizable brand" list rather than leaving an icon slot blank.
const DESTINATION_ICONS = { telebirr: "📱", cbe_birr: "🏦", cbe: "🏦", awash: "🏦", boa: "🏦" };

let selectedManualDestinationId = null;

async function loadManualDestinations() {
  const listEl = el("deposit-manual-destinations");
  try {
    const response = await fetch("/api/manual-payment-destinations", { headers: authHeader() });
    const destinations = await response.json();
    if (!response.ok || destinations.length === 0) {
      listEl.innerHTML = `<p class="wallet-note">${t("wallet.no_manual_destinations")}</p>`;
      return;
    }
    selectedManualDestinationId = destinations[0].id;
    listEl.innerHTML = `
      <div class="destination-list" id="deposit-manual-destination-list">
        ${destinations
          .map(
            (d) => `
          <button type="button" class="destination-card" data-id="${d.id}"
                  aria-pressed="${d.id === selectedManualDestinationId}">
            <span class="destination-icon">${DESTINATION_ICONS[d.method_kind] || "🏦"}</span>
            <span class="destination-info">
              <span class="destination-name">${escapeHtml(d.method_kind.replace(/_/g, " "))}</span>
              <span class="destination-ref">${escapeHtml(d.account_name)} · ${escapeHtml(d.account_ref)}</span>
            </span>
            <span class="destination-check"></span>
          </button>`
          )
          .join("")}
      </div>
      <p id="deposit-manual-instructions" class="wallet-note"></p>
    `;
    const instructionsEl = el("deposit-manual-instructions");
    const byId = new Map(destinations.map((d) => [d.id, d]));
    const updateInstructions = () => {
      const destination = byId.get(selectedManualDestinationId);
      instructionsEl.textContent = (destination && destination.instructions) || "";
    };
    // Real <button>s -- already fully keyboard-accessible (Tab + Enter/
    // Space) with no extra wiring, unlike the plain <div>s
    // makeKeyboardActivatable exists for elsewhere in this file.
    for (const card of listEl.querySelectorAll(".destination-card")) {
      card.addEventListener("click", () => {
        selectedManualDestinationId = Number(card.dataset.id);
        for (const other of listEl.querySelectorAll(".destination-card")) {
          other.setAttribute("aria-pressed", String(other === card));
        }
        updateInstructions();
      });
    }
    updateInstructions();
  } catch {
    listEl.innerHTML = `<p class="wallet-note">${t("wallet.error.generic")}</p>`;
  }
}

// The automatic Telebirr flow shows every active Telebirr destination an
// admin has configured -- not just one. The server's own recipient match
// (services/payments/telebirr_ingest.py::_find_matching_recipient)
// already accepts a payment to ANY of them, so if an admin adds a second
// number (a new till, a backup account, whatever the real reason), a
// player needs to see it too, not just whichever one happened to be
// added first -- a real gap found live, 2026-09-14: a newly-added
// destination was invisible here because this only ever looked up a
// single match. Reuses the exact same /api/manual-payment-destinations
// data and visual language as the manual picker above; each one gets its
// own card and its own copy button, since paying into any of them works.
async function loadTelebirrDestination() {
  const containerEl = el("deposit-telebirr-destination");
  try {
    const response = await fetch("/api/manual-payment-destinations", { headers: authHeader() });
    const allDestinations = await response.json();
    const destinations = response.ok ? allDestinations.filter((d) => d.method_kind === "telebirr") : [];
    if (destinations.length === 0) {
      containerEl.innerHTML = `<p class="wallet-note">${t("wallet.no_manual_destinations")}</p>`;
      return;
    }
    containerEl.innerHTML = destinations
      .map(
        (d, i) => `
      <div class="destination-display">
        <span class="destination-icon">${DESTINATION_ICONS.telebirr}</span>
        <span class="destination-info">
          <span class="destination-name">Telebirr</span>
          <span class="destination-ref">${escapeHtml(d.account_name)} · ${escapeHtml(d.account_ref)}</span>
        </span>
        <button type="button" class="destination-copy-btn" data-index="${i}">${t("wallet.telebirr_copy")}</button>
      </div>`
      )
      .join("");
    for (const btn of containerEl.querySelectorAll(".destination-copy-btn")) {
      btn.addEventListener("click", async (event) => {
        const target = event.currentTarget;
        const destination = destinations[Number(target.dataset.index)];
        try {
          await navigator.clipboard.writeText(destination.account_ref);
          target.textContent = t("wallet.telebirr_copied");
          target.classList.add("copied");
          setTimeout(() => {
            target.textContent = t("wallet.telebirr_copy");
            target.classList.remove("copied");
          }, 2000);
        } catch {
          /* clipboard access denied or unavailable -- the number is
             still shown in plain text above, so this is a convenience,
             not the only way to get it */
        }
      });
    }
  } catch {
    containerEl.innerHTML = `<p class="wallet-note">${t("wallet.error.generic")}</p>`;
  }
}

el("deposit-manual-submit-btn").addEventListener("click", async () => {
  const amount = el("deposit-manual-amount-input").value;
  const reference = el("deposit-manual-reference-input").value.trim();
  const destinationId = selectedManualDestinationId;
  if (!amount || Number(amount) <= 0 || !destinationId || !reference) {
    setWalletStatus("deposit-manual-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("deposit-manual-submit-btn").disabled = true;
  setWalletStatus("deposit-manual-status", "wallet.deposit_opening", null);
  try {
    const response = await fetch("/api/deposit/manual", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ amount, manual_destination_id: destinationId, external_reference: reference }),
    });
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("deposit-manual-status", `wallet.error.${data.detail || "generic"}`, "error");
      return;
    }
    setWalletStatus("deposit-manual-status", "wallet.manual_deposit_submitted", "success");
    el("deposit-manual-amount-input").value = "";
    el("deposit-manual-reference-input").value = "";
  } catch {
    setWalletStatus("deposit-manual-status", "wallet.error.generic", "error");
  } finally {
    el("deposit-manual-submit-btn").disabled = false;
  }
});

// --- withdraw ----------------------------------------------------------
// The summary card is a pure, read-only reflection of the three inputs
// below it plus the request's own outcome -- no new data, nothing the
// submit handler doesn't already have. Kept live via "input" listeners
// so it updates as the player types, matching the reference mockups'
// own "review before you submit" pattern.

function setWithdrawSummaryStatus(text, kind) {
  const pill = el("withdraw-summary-status");
  pill.textContent = text;
  pill.classList.remove("pending", "success");
  if (kind) pill.classList.add(kind);
}

function updateWithdrawSummary() {
  const amount = el("withdraw-amount-input").value;
  const accountRef = el("withdraw-account-input").value.trim();
  const holderName = el("withdraw-name-input").value.trim();
  el("withdraw-summary-amount").textContent = `${amount || 0} ETB`;
  el("withdraw-summary-account").textContent = accountRef || t("wallet.summary_not_provided");
  el("withdraw-summary-holder").textContent = holderName || t("wallet.summary_not_provided");
}

for (const id of ["withdraw-amount-input", "withdraw-account-input", "withdraw-name-input"]) {
  el(id).addEventListener("input", updateWithdrawSummary);
}

el("withdraw-submit-btn").addEventListener("click", async () => {
  const amount = el("withdraw-amount-input").value;
  const accountRef = el("withdraw-account-input").value.trim();
  const holderName = el("withdraw-name-input").value.trim();
  const manual = el("withdraw-manual-checkbox").checked;
  if (!amount || Number(amount) <= 0 || !accountRef || !holderName) {
    setWalletStatus("withdraw-status", "wallet.error.invalid_amount", "error");
    return;
  }
  el("withdraw-submit-btn").disabled = true;
  setWalletStatus("withdraw-status", "wallet.withdraw_submitting", null);
  setWithdrawSummaryStatus(t("wallet.withdraw_submitting"), "pending");
  try {
    const response = await fetch("/api/withdraw", {
      method: "POST",
      headers: { ...authHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({
        amount,
        account_ref: accountRef,
        holder_name: holderName,
        provider: manual ? "manual" : "chapa",
      }),
    });
    if (response.status === 503) {
      setWalletStatus("withdraw-status", "wallet.not_available", "error");
      setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
      return;
    }
    const data = await response.json();
    if (!response.ok) {
      setWalletStatus("withdraw-status", `wallet.error.${data.detail || "generic"}`, "error");
      setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
      return;
    }
    const approved = data.status === "approved";
    setWalletStatus("withdraw-status", approved ? "wallet.withdraw_approved" : "wallet.withdraw_review", "success");
    setWithdrawSummaryStatus(
      approved ? t("wallet.summary_approved") : t("wallet.summary_pending_approval"),
      approved ? "success" : "pending"
    );
  } catch {
    setWalletStatus("withdraw-status", "wallet.error.generic", "error");
    setWithdrawSummaryStatus(t("wallet.summary_not_submitted"), null);
  } finally {
    el("withdraw-submit-btn").disabled = false;
  }
});

// --- history -------------------------------------------------------------
// Spec 2.6: "History: rounds and transactions, filterable, each linking
// to its detail." Transactions and per-round detail links aren't built
// yet (no transaction-listing endpoint or round-detail screen exists),
// but the filter itself is a real, self-contained gap: the last 10
// rounds fetched here already carry a won/lost outcome, so filtering by
// it needs no new backend surface -- reusing the deposit screen's own
// .amount-chip look for a native, already-keyboard-accessible <button>
// rather than inventing a new control style.

let historyRows = [];
let historyFilter = "all";

async function loadHistory() {
  try {
    const response = await fetch("/api/history", { headers: authHeader() });
    if (!response.ok) return;
    historyRows = await response.json();
    renderHistory();
  } catch {
    /* history pane just keeps whatever it already had */
  }
}

function renderHistory() {
  const list = el("history-list");
  list.innerHTML = "";
  if (historyRows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "wallet-note";
    empty.textContent = t("wallet.history_empty");
    list.appendChild(empty);
    return;
  }
  const filtered = historyRows.filter((row) => {
    if (historyFilter === "won") return row.won;
    if (historyFilter === "lost") return !row.won;
    return true;
  });
  if (filtered.length === 0) {
    const empty = document.createElement("p");
    empty.className = "wallet-note";
    empty.textContent = t("wallet.history_filter_empty");
    list.appendChild(empty);
    return;
  }
  for (const row of filtered) {
    const line = document.createElement("div");
    line.className = row.won ? "history-row won" : "history-row";
    const outcome = row.won
      ? t("wallet.history_won", { amount: row.won_amount })
      : t("wallet.history_lost");
    const dot = document.createElement("span");
    dot.className = "history-dot";
    const roundLabel = document.createElement("span");
    roundLabel.className = "history-main";
    roundLabel.textContent = t("wallet.history_round", { seq: row.seq, stake: row.stake });
    const outcomeLabel = document.createElement("span");
    outcomeLabel.className = "history-meta";
    outcomeLabel.textContent = outcome;
    line.appendChild(dot);
    line.appendChild(roundLabel);
    line.appendChild(outcomeLabel);
    list.appendChild(line);
  }
}

document.querySelectorAll("#history-filter-chips .amount-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll("#history-filter-chips .amount-chip").forEach((c) => c.classList.remove("selected"));
    chip.classList.add("selected");
    historyFilter = chip.dataset.filter;
    renderHistory();
  });
});

// --- toasts / errors -------------------------------------------------

function showToast(messageOrKey) {
  const toast = el("toast");
  // A translation key never contains a space ("error.generic"); an
  // already-resolved message might still contain a "." (a sentence's full
  // stop) without being one, so check for both rather than "." alone.
  const looksLikeKey = messageOrKey.includes(".") && !messageOrKey.includes(" ");
  toast.textContent = looksLikeKey ? t(messageOrKey) : messageOrKey;
  toast.classList.add("visible");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.remove("visible"), 2500);
}

ws.on("error", (msg) => showToast(msg.message_am || msg.code));

// A deposit (or any other out-of-round balance change) pushes this over the
// user's own `user:{id}` channel (services/payments/deposits.py) -- the
// header balance and, if it's currently open, the wallet screen both pick
// it up live instead of waiting for the player to reopen the wallet tab.
ws.on("balance_update", (msg) => {
  const state = getState();
  if (state.user) setState({ user: { ...state.user, balance: msg.cash } });
  if (getState().screen === "wallet") {
    renderWalletBalances(msg);
  }
  if (getState().screen === "lobby") {
    el("lobby-balance-amount").textContent = `${msg.cash} ETB`;
  }
  if (pendingDeposit && Number(msg.cash) - pendingDeposit.cashBefore >= pendingDeposit.amount - 0.01) {
    setWalletStatus("deposit-status", "wallet.deposit_confirmed", "success");
    pendingDeposit = null;
  }
});

// --- session-time reminders (spec section 12) -----------------------------
// "You've been playing 60 minutes" -- a plain awareness nudge, not an
// enforcement control (self-exclusion/cool-off/limits are already
// server-enforced -- see packages/core/responsible_gaming.py). Purely
// client-side and resets on reload by design, the same "this session"
// framing the reality check above uses.

const SESSION_REMINDER_MINUTES = [60, 120, 180];

function checkSessionReminder() {
  const state = getState();
  const elapsedMinutes = Math.floor((Date.now() - state.sessionStartedAt) / 60000);
  for (const threshold of SESSION_REMINDER_MINUTES) {
    if (elapsedMinutes >= threshold && !state.sessionRemindersShown.has(threshold)) {
      state.sessionRemindersShown.add(threshold);
      showToast(t("session.reminder", { minutes: threshold }));
      haptics.warning();
    }
  }
}

setInterval(checkSessionReminder, 60000);

// --- Telegram back button -------------------------------------------------

if (tg) {
  tg.BackButton.onClick(() => {
    const state = getState();
    if (state.screen === "wallet" || state.screen === "lobby" || state.screen === "game") {
      showScreen("rooms");
    } else if (state.screen === "result") {
      showScreen("rooms");
    }
    // showScreen("rooms") now does send a real WebSocket message: it
    // unsubscribes this connection from the room's live broadcasts (see
    // showScreen()'s own comment above -- the cross-room call-leak fix).
    // That's still safe to do from Back: services/gateway/connection.py's
    // _handle_leave() only ever discards the broadcast subscription, never
    // round_entries/cards/auto_mark, so the round keeps running and
    // settling server-side exactly as before -- the player just stops
    // *watching* it (same real-money outcome as backgrounding the app).
    // The one thing this does give up is live updates for that round: if
    // the player doesn't tap back into this room before it ends, they
    // won't see its round_end here and must check the room again (or their
    // balance) to find out how it settled, rather than getting a result
    // screen for a round they've already navigated away from.
  });
}

// --- boot -----------------------------------------------------------------

// The splash's own entrance animation (index.html's inlined <style>) takes
// ~900ms; on a fast connection boot() can otherwise reach a terminal state
// well before that finishes, cutting the animation off mid-pop -- holding
// the splash for this minimum feels deliberate instead of flashed-then
// -yanked. Enforced as elapsed-time-since-start, not a flat extra delay, so
// a slow connection (where boot() already took longer than this) never
// waits any additional time it didn't already spend.
const SPLASH_MIN_VISIBLE_MS = 1100;
const splashShownAt = Date.now();

function hideSplash() {
  const splash = document.getElementById("splash-screen");
  if (!splash || splash.dataset.hidden) return;
  splash.dataset.hidden = "1";
  const wait = Math.max(0, SPLASH_MIN_VISIBLE_MS - (Date.now() - splashShownAt));
  setTimeout(() => {
    splash.classList.add("splash-hidden");
    setTimeout(() => splash.remove(), 550);
  }, wait);
}

async function boot() {
  if (tg) {
    tg.ready();
    tg.expand();
  }
  const preferredLanguage = tg && tg.initDataUnsafe && tg.initDataUnsafe.user
    ? tg.initDataUnsafe.user.language_code
    : "am";
  await initI18n(preferredLanguage);
  setLanguage(preferredLanguage);
  applyStaticTranslations();

  const initData = tg ? tg.initData : "";
  const hasInitData = Boolean(initData && initData.trim());
  if (!hasInitData) {
    // Show an explicit auth-failure shell so the player never sees a
    // featureless black screen if Telegram did not provide initData. A
    // fresh element, not el("boot-shell") -- no such id exists in
    // index.html, so that lookup returned null and threw on the next
    // line, silently reproducing the exact blank screen this was meant
    // to fix.
    //
    // Launch-readiness audit finding: this branch is reached both by a
    // genuine Telegram auth glitch (stale initData) and by simply opening
    // the Mini App's URL directly in a plain browser -- the far more
    // common case once the app is reachable outside a single Telegram
    // client (spec section 5). The old copy here (error.generic +
    // connection.connect_failed, "Unable to connect to the game server.
    // Tap to retry.") is only true for the first case; showing it for the
    // second tells a player their network/server is broken when nothing
    // is, and offering a reload as the fix is actively misleading -- it
    // reloads into this exact same dead end, since the real fix (open
    // the bot's menu button inside Telegram) isn't something a reload
    // can do. No retry action is shown for this specific cause; a
    // stale/expired initData still surfaces via the "connection.expired"
    // banner in ws.js, unchanged, which does have a real fix (reopen).
    //
    // A real-browser test (tests/integration/test_miniapp_e2e.py) caught
    // that wrapping the styled ".boot-shell" (position: fixed) inside an
    // unstyled outer "#boot-shell" div left that outer div with zero
    // height -- a position:fixed child contributes nothing to its
    // parent's in-flow box. It still rendered correctly for a sighted
    // human (the fixed child paints over the whole viewport regardless of
    // its parent's own box), but any code checking the outer element's
    // own visibility -- a test's wait_for_selector, or assistive tech --
    // sees a hidden, zero-size element. Styling id="boot-shell" directly,
    // with no separate wrapper, avoids the mismatch entirely.
    const shell = document.createElement("div");
    shell.id = "boot-shell";
    shell.className = "boot-shell";
    shell.innerHTML = `
      <div class="boot-shell-title">${t("error.not_in_telegram_title")}</div>
      <div class="boot-shell-body">${t("error.not_in_telegram_body")}</div>
    `;
    document.body.appendChild(shell);
    hideSplash();
    return;
  }

  ws.connect(initData);
  let user;
  try {
    user = await ws.waitForAuth();
  } catch {
    // A terminal auth failure (stale initData, most commonly) already
    // drives the "connection.expired" reload banner via the
    // connection-state subscriber above, independent of this promise --
    // nothing left to do here except stop, rather than hang forever
    // awaiting a user that ws.js now knows will never arrive.
    hideSplash();
    return;
  }
  setState({ user });
  // The language_code hint drove the boot screen above; now that the
  // server told us the real users.language, that value wins (spec 7.5).
  await applyServerLanguage(user.language);
  applyStaticTranslations();
  document.getElementById("balance-amount").textContent = `${user.balance} ETB`;
  showScreen("rooms");
  refreshRoomList();
  fetchAnnouncement();
  fetchInviteSummary();
  // Warms the browser's cache for all 75 call clips well before any
  // room's first call -- see voiceCaller.preloadAll()'s own docstring
  // for why this specifically helps on a weak connection.
  voiceCaller.preloadAll();

  // Deep link from the bot: /deposit's own "open the wallet" button
  // (services/bot/keyboards.py::open_wallet_keyboard) appends
  // "?target=deposit" to the Mini App URL so tapping it lands the player
  // directly on the Deposit tab -- not on Balance with a further,
  // easy-to-miss tap still needed (real confusion this exact session
  // spent hours untangling came from players never finding the right
  // screen at all).
  //
  // Deliberately read from the query string, not the hash: a first
  // attempt used "#deposit", which two separate real-device tests
  // (2026-09-14) proved never survives inside actual Telegram --
  // Telegram's own client needs that same fragment to deliver its WebApp
  // initData (tg.initData above, populated from
  // "#tgWebAppData=...&tgWebAppVersion=...&tgWebAppPlatform=...") and
  // evidently reconstructs or otherwise owns that fragment wholesale, so
  // "#deposit" never reached this check at all. The query string is a
  // part of the URL Telegram has no reason to touch.
  //
  // Cleared immediately via replaceState so it can never re-trigger on
  // an in-app reload or re-fire if the player later switches away and
  // Telegram's own Back button brings them back to this same screen.
  const deepLinkTarget = new URLSearchParams(location.search).get("target");
  if (deepLinkTarget === "deposit") {
    history.replaceState(null, "", location.pathname + location.hash);
    await openWallet();
    switchToWalletTab("deposit");
  }

  hideSplash();
}

subscribe((state) => {
  if (state.user) {
    document.getElementById("balance-amount").textContent = `${state.user.balance} ETB`;
  }
});

boot();
