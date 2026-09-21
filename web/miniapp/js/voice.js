// Bingo number-calling voice announcer. Plays a pre-generated audio clip
// ("B! ሰባት!") whenever a number is called, via a small FIFO queue so
// rapid calls never overlap or interrupt each other. See
// web/miniapp/audio/calls/README.md for the audio-file contract this
// module reads from.
//
// No speech-synthesis fallback on purpose: browser speechSynthesis
// Amharic support is inconsistent-to-absent on real devices, and a
// mispronounced/garbled fallback would be worse than the silent
// skip-with-warning this module already does when a clip is missing.
// Every public method is wrapped so an audio failure (missing file,
// unsupported format, autoplay block) never breaks gameplay.

const AUDIO_BASE = "/audio/calls";
const warnedMissing = new Set();

function pad(n) {
  return String(n).padStart(2, "0");
}

function clipUrl(letter, number) {
  return `${AUDIO_BASE}/${letter}_${pad(number)}.mp3`;
}

// Deliberately a small local duplicate, not a shared import -- this
// module has no dependency on app.v6.js today, and a fixed, permanent
// rule (spec: B 1-15, I 16-30, N 31-45, G 46-60, O 61-75) is cheap to
// repeat and not worth coupling two otherwise-independent files over.
function letterForNumber(n) {
  return ["B", "I", "N", "G", "O"][Math.floor((n - 1) / 15)];
}

// A queued-but-not-yet-played call this stale on a slow connection is
// more confusing than useful -- by the time it would play, the board has
// moved on several calls already. Capped low (not zero: a brief network
// blip should still let the very next call or two catch up), this keeps
// the announcer from ever drifting far behind reality instead of slowly
// accumulating an ever-growing backlog on a weak connection.
const MAX_QUEUE_LENGTH = 2;

class VoiceCaller {
  constructor() {
    this._queue = [];
    this._announced = new Set();
    this._playing = false;
    // The in-flight Audio element, if any -- tracked so resetRound()/
    // setEnabled(false) can actually silence it. Real reported bug:
    // switching rooms only ever cleared the *pending* queue; an already-
    // playing (or, on a slow connection, still buffering) clip from the
    // room just left kept going regardless, so a player who'd already
    // moved to a different room's board could still hear the old room's
    // call finish -- worse the slower the connection, since a clip can
    // spend several real seconds loading before it even starts.
    this._currentAudio = null;
    this._enabled = true;
    this._volume = 1;
    this._speed = 1;
    this._unlocked = false;
    this._lastCall = null;
  }

  setEnabled(enabled) {
    this._enabled = !!enabled;
    if (!this._enabled) {
      this._queue = [];
      this._stopCurrent();
    }
  }

  isEnabled() {
    return this._enabled;
  }

  setVolume(volume) {
    this._volume = Math.min(1, Math.max(0, volume));
  }

  getVolume() {
    return this._volume;
  }

  setSpeed(speed) {
    this._speed = Math.min(2, Math.max(0.5, speed));
  }

  getSpeed() {
    return this._speed;
  }

  // Resets per-round dedup state -- call on room join / new round, the
  // same lifecycle point the "called" number set itself resets at. Also
  // silences whatever's currently playing/loading (see _currentAudio's
  // own comment) -- leaving a room must mean its audio stops too, not
  // just that no *new* audio for it gets queued.
  resetRound() {
    this._announced.clear();
    this._queue = [];
    this._stopCurrent();
  }

  _stopCurrent() {
    if (this._currentAudio) {
      // Detach handlers first -- pause() can itself fire onended/onerror
      // in some browsers, which would otherwise re-enter _pump() with
      // exactly the stale state this method exists to clear.
      this._currentAudio.onended = null;
      this._currentAudio.onerror = null;
      try {
        this._currentAudio.pause();
      } catch {
        // Best-effort -- the element is being discarded either way.
      }
      this._currentAudio = null;
    }
    this._playing = false;
  }

  // Must be invoked from a real user-gesture handler (the room-join
  // click) so later programmatic .play() calls aren't blocked by mobile
  // Safari / Telegram WebView autoplay policy.
  unlock() {
    if (this._unlocked) return;
    this._unlocked = true;
    try {
      const audio = new Audio();
      audio.muted = true;
      const p = audio.play();
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch {
      // Autoplay unlock is best-effort; a failure here just means the
      // first real announcement may need another gesture, not a bug.
    }
  }

  // Weak-connection resilience: all 75 possible clips together are well
  // under 1MB (~13KB apiece), small enough to fetch once, in the
  // background, long before any of them are actually needed. Without
  // this, the very first time a given number comes up, its clip starts
  // downloading only at that moment -- exactly the case a slow
  // connection turns into a late, out-of-sync, or (per MAX_QUEUE_LENGTH
  // above) dropped announcement. Warming the browser's own HTTP cache
  // ahead of time means a real call almost always plays from cache
  // instead of racing a fresh download against the room's call clock.
  // Fire-and-forget and best-effort throughout: a failed or slow prefetch
  // just means that one clip falls back to its normal on-demand fetch at
  // call-time, exactly today's existing behavior -- this can only help,
  // never regress anything.
  preloadAll() {
    // A player who has turned voice off gets none of the benefit of a
    // warm cache and only pays its real cost -- ~1MB of downloads on
    // whatever connection they have, weak or not, for clips they will
    // never hear. loadVoiceSettings() (app.v6.js) already restores the
    // persisted on/off preference before boot() ever reaches this call,
    // so this reflects the player's real, already-known choice, not a
    // stale default.
    if (!this._enabled) return;
    for (let n = 1; n <= 75; n++) {
      try {
        const audio = new Audio(clipUrl(letterForNumber(n), n));
        audio.preload = "auto";
        audio.load();
      } catch {
        // Best-effort -- see this method's own docstring.
      }
    }
  }

  announce(letter, number, callIndex) {
    if (!this._enabled) return;
    if (callIndex !== undefined && callIndex !== null) {
      if (this._announced.has(callIndex)) return;
      this._announced.add(callIndex);
    }
    this._queue.push({ letter, number });
    // A weak connection is exactly when clips take longest to load, which
    // is exactly when the queue would otherwise grow the most -- drop the
    // *oldest* backlog, never the call that just happened, so the
    // announcer catches back up to the live board instead of slowly
    // falling further behind it.
    while (this._queue.length > MAX_QUEUE_LENGTH) this._queue.shift();
    this._pump();
  }

  // Bypasses dedup on purpose -- an explicit replay request, not a
  // duplicate broadcast.
  replayLast() {
    if (!this._enabled || !this._lastCall) return;
    this._queue.push(this._lastCall);
    this._pump();
  }

  _pump() {
    if (this._playing || this._queue.length === 0) return;
    const call = this._queue.shift();
    this._playing = true;
    try {
      const audio = new Audio(clipUrl(call.letter, call.number));
      this._currentAudio = audio;
      audio.volume = this._volume;
      audio.playbackRate = this._speed;
      const advance = () => {
        // Only this exact element's own completion should advance the
        // queue -- if resetRound()/_stopCurrent() already discarded it
        // (this._currentAudio is null or points at a newer element), a
        // late onended/onerror from the stale one must be a no-op, not a
        // second, out-of-order _pump() call racing the real one.
        if (this._currentAudio !== audio) return;
        this._currentAudio = null;
        this._playing = false;
        this._pump();
      };
      audio.onended = advance;
      audio.onerror = () => {
        const key = `${call.letter}${call.number}`;
        if (!warnedMissing.has(key)) {
          warnedMissing.add(key);
          console.warn(`[voice] missing or unplayable audio clip for ${key}`);
        }
        advance();
      };
      const playPromise = audio.play();
      if (playPromise && typeof playPromise.catch === "function") {
        playPromise.catch(() => advance());
      }
      this._lastCall = call;
    } catch {
      this._currentAudio = null;
      this._playing = false;
      this._pump();
    }
  }
}

export const voiceCaller = new VoiceCaller();
