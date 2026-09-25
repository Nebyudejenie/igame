// WebSocket client matching services/gateway's protocol exactly (spec
// section 6). One connection for the whole app lifetime; reconnection is
// transparent to callers -- re-authenticates and re-joins whatever room
// was active, and the server's state_sync reply is what actually restores
// the player's place, not anything remembered client-side (spec principle
// 7: "A player's session can die at any moment. Reconnection must restore
// full state from the server in one message.")

import { setState, getState } from "./state.js";

const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const PING_INTERVAL_MS = 20000;
// A production incident (Mini App opens to a permanently blank screen)
// traced to a gap the existing terminal-close-code handling below
// doesn't cover: a WebSocket that never successfully opens at all --
// wrong URL, a reverse-proxy/tunnel not forwarding the Upgrade header,
// a firewall -- as opposed to one that opens and then gets a terminal
// close code (bad initData). That case has no close event with a
// terminal code to react to; it just keeps retrying forever with
// backoff, and nothing ever settles waitForAuth()'s promise. app.js's
// boot() awaits that promise exactly once, before any screen has ever
// rendered, so a connection that can never succeed left the player
// looking at a permanently blank page with nothing telling them
// anything was wrong. This bounds only the very first wait -- once
// authenticated, reconnection during active gameplay still retries
// forever with no timeout, exactly as the spec's own reconnection
// principle requires.
const INITIAL_AUTH_TIMEOUT_MS = 20000;

// services/gateway/connection.py's own _handshake() close codes for a
// handshake that will *never* succeed on retry: 4000 (malformed/
// unexpected first frame), 4001 (no auth frame within the timeout), 4003
// (initData rejected -- most commonly past Telegram's own validity
// window). A code review pass caught that every close code was retried
// identically: a client whose captured initData had gone stale kept
// retrying forever (backed off, after the fix above, but still forever)
// against a handshake that can only ever fail again, with nothing telling
// the player why nothing was happening. initData is captured once, at
// connect() time, from whatever Telegram handed the page on load -- this
// module has no way to obtain a fresh one without the page itself
// reloading (Telegram doesn't push an updated initData into an
// already-open WebView), so retrying is not just pointless here, it can
// never work.
export const _TERMINAL_CLOSE_CODES = new Set([4000, 4001, 4003]);

let socket = null;
let initData = null;
let pingTimer = null;
let reconnectTimer = null;
let reconnectAttempts = 0;
let authResolvers = [];
const messageHandlers = new Map(); // type -> Set<fn>

function wsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

// Exponential backoff with full jitter (a code review pass caught this
// was previously a flat 1s retry, forever, with zero randomization): every
// client that dropped its connection at the same moment -- a gateway
// restart, a shared network blip affecting a whole room -- was retrying in
// lockstep, so they'd all hit the gateway again in the same tight burst
// right as it's most fragile (cold caches, connection pool still warming
// up). Doubling the delay per failed attempt, capped, and picking a
// *random* point in [0, cap] rather than the cap itself decorrelates
// those simultaneous retries instead of just spacing out one client's own.
export function _reconnectDelayForAttempt(attempt) {
  const cap = Math.min(RECONNECT_MAX_DELAY_MS, RECONNECT_BASE_DELAY_MS * 2 ** attempt);
  return Math.random() * cap;
}

export function on(type, handler) {
  if (!messageHandlers.has(type)) messageHandlers.set(type, new Set());
  messageHandlers.get(type).add(handler);
  return () => messageHandlers.get(type).delete(handler);
}

// The only place authResolvers entries ever get resolved/rejected other
// than waitForAuth()'s own deadline firing -- always via this, so every
// entry's deadline timer (see waitForAuth()) gets cleared here too. A
// real bug this exact fix's own test caught: without clearing it, a
// promise settled here still left a real, un-cancelled setTimeout
// pending for the rest of its full timeoutMs, silently outliving the
// promise it no longer had anything to do (harmless in a browser tab,
// but genuinely dangling).
function _settleAuthResolvers(outcome, value) {
  for (const entry of authResolvers.splice(0)) {
    clearTimeout(entry.timer);
    if (outcome === "resolve") entry.resolve(value);
    else entry.reject(value);
  }
}

function dispatch(message) {
  // Epoch milliseconds only. One offset is shared by every countdown in the
  // app (Bingo lobby, rooms list, Keno betting), so a frame carrying epoch
  // *seconds* -- Keno's betting.open once did -- must not reset it: it
  // knocked every countdown ~55 years out until the next pong repaired it.
  if (typeof message.server_time === "number" && message.server_time > 1e12) {
    setState({ serverTimeOffsetMs: message.server_time - Date.now() });
  }
  const handlers = messageHandlers.get(message.t);
  if (handlers) for (const handler of handlers) handler(message);
}

export function connect(rawInitData) {
  initData = rawInitData;
  open();
}

function open() {
  setState({ connection: getState().connection === "connected" ? "reconnecting" : "connecting" });
  socket = new WebSocket(wsUrl());

  socket.addEventListener("open", () => {
    reconnectAttempts = 0;
    send({ t: "auth", init_data: initData });
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.t === "authed") {
      setState({ connection: "connected", user: message.user });
      // Resolve with the user object, matching waitForAuth()'s other
      // branch (`Promise.resolve(getState().user)`) -- resolving with the
      // whole {t, user, server_time} envelope here silently broke every
      // caller's `user.balance` access (real bug, caught by an E2E test
      // actually reading the DOM instead of just checking the WS traffic).
      _settleAuthResolvers("resolve", message.user);
      const { currentRoomId } = getState();
      if (currentRoomId !== null) send({ t: "join", room_id: currentRoomId });
    }
    dispatch(message);
  });

  socket.addEventListener("close", (event) => {
    clearInterval(pingTimer);
    if (_TERMINAL_CLOSE_CODES.has(event.code)) {
      // Retrying can only ever fail again -- see _TERMINAL_CLOSE_CODES'
      // own comment above for why. Stop here instead of hammering the
      // gateway with a doomed reconnect loop forever; the player needs
      // to actually reload the Mini App (app.js's banner tells them so).
      setState({ connection: "auth_failed" });
      // A code review pass caught that nothing ever settled a
      // waitForAuth() promise still pending when a terminal failure hit --
      // "authed" is the only other place authResolvers gets drained, and
      // that message is never coming now. app.js's boot() awaits this
      // directly, so it hung on a stale/expired initData forever instead
      // of ever reaching the reload banner the connection-state
      // subscriber above already shows independently of this promise.
      _settleAuthResolvers("reject", new Error(`auth failed: close code ${event.code}`));
      return;
    }
    setState({ connection: "offline" });
    // 1012 = "service restart" (our own gateway's graceful-shutdown code,
    // spec section 6): reconnect immediately, no backoff needed -- the
    // server just told us it's about to be available again right away.
    const delay = event.code === 1012 ? 0 : _reconnectDelayForAttempt(reconnectAttempts++);
    reconnectTimer = setTimeout(open, delay);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });

  pingTimer = setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) send({ t: "ping", ts: Date.now() });
  }, PING_INTERVAL_MS);
}

export function disconnect() {
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  if (socket) socket.close();
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

// timeoutMs is overridable only so tests can drive this deterministically
// in well under INITIAL_AUTH_TIMEOUT_MS's real 20s -- every real caller
// (app.js's boot(), the only one) calls this with zero arguments.
export function waitForAuth(timeoutMs = INITIAL_AUTH_TIMEOUT_MS) {
  const state = getState();
  if (state.connection === "connected") return Promise.resolve(state.user);
  // A terminal failure that already happened before this call (e.g. a
  // caller re-checking auth after the banner appeared) has no "authed"
  // message coming either -- reject immediately rather than queuing a
  // resolver that would wait forever, same reasoning as the close-handler
  // rejection above.
  if (state.connection === "auth_failed") {
    return Promise.reject(new Error("auth failed"));
  }
  return new Promise((resolve, reject) => {
    const entry = { resolve, reject, timer: null };
    authResolvers.push(entry);
    // See INITIAL_AUTH_TIMEOUT_MS's own comment -- a flat client-side
    // deadline covers every way this could otherwise hang forever
    // (the socket never opens, it opens but "authed" never arrives,
    // etc.) without needing to know which one is actually happening.
    // _settleAuthResolvers() clears this same timer if the promise
    // settles for real (authed or a terminal close) before it ever
    // fires -- otherwise a real, un-cancelled timer would keep firing
    // (harmlessly, but genuinely dangling) for the rest of timeoutMs.
    entry.timer = setTimeout(() => {
      const index = authResolvers.indexOf(entry);
      if (index === -1) return; // already settled for real
      authResolvers.splice(index, 1);
      setState({ connection: "connect_failed" });
      reject(new Error("timed out waiting for the server to authenticate this session"));
    }, timeoutMs);
  });
}

export function requestRooms() {
  send({ t: "rooms" });
}

export function joinRoom(roomId) {
  setState({ currentRoomId: roomId });
  send({ t: "join", room_id: roomId });
}

export function leaveRoom(roomId) {
  send({ t: "leave", room_id: roomId });
  setState({ currentRoomId: null });
}

export function takeCard(roomId, cardNo) {
  send({ t: "take_card", room_id: roomId, card_no: cardNo, idem: `${roomId}-${cardNo}-${Date.now()}` });
}

export function dropCard(roomId, cardNo) {
  send({ t: "drop_card", room_id: roomId, card_no: cardNo });
}

export function setAuto(roomId, auto) {
  send({ t: "set_auto", room_id: roomId, auto });
}

export function claim(roundId, cardNo) {
  send({ t: "claim", round_id: roundId, card_no: cardNo });
}

export function mark(roundId, r, c) {
  send({ t: "mark", round_id: roundId, r, c });
}
