// Minimal pub/sub state container. No framework needed for a state tree
// this small (spec: "avoid heavy frameworks; vanilla + a tiny reactive
// layer... is plenty").

const state = {
  connection: "connecting", // connecting | connected | reconnecting | offline | auth_failed | connect_failed
  user: null, // {id, name, balance}
  serverTimeOffsetMs: 0, // server_time - Date.now(), refreshed on every message that carries one
  screen: "rooms", // rooms | lobby | game | result | wallet
  rooms: [],
  onlineCount: 0, // real WebSocket headcount from the server's own "rooms" push, never bots
  currentRoomId: null,
  round: null, // last state_sync payload for the joined room
  yourCardGrid: null, // 5x5 grid for the card this user holds, once known
  autoMark: true,
  lockedOut: false,
  lastResult: null,
  // Reality check (spec section 12): a running, display-only total of
  // stakes vs. winnings since this app instance was opened. Never
  // authoritative -- the ledger is; this resets on reload by design, the
  // same way "this session" reads to a player checking a results screen.
  sessionStartedAt: Date.now(),
  sessionNetPosition: 0,
  sessionRemindersShown: new Set(),
};

const listeners = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  Object.assign(state, patch);
  for (const listener of listeners) listener(state);
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function serverNow() {
  return Date.now() + state.serverTimeOffsetMs;
}
