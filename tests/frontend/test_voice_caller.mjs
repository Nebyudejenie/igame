// Plain-node smoke test for web/miniapp/js/voice.js -- same no-framework
// discipline as test_reconnect_backoff.mjs. A fake global Audio (node has
// no real media API) gives full manual control over when a clip
// "finishes", which is exactly what's needed to exercise the real bug
// this file guards against: switching rooms while a clip is still
// loading/playing must actually silence it, not just stop new calls from
// being queued. Invoked from tests/unit/test_voice_caller.py.

import assert from "node:assert/strict";

class FakeAudio {
  constructor(src) {
    this.src = src;
    this.onended = null;
    this.onerror = null;
    this.paused = false;
    this.volume = 1;
    this.playbackRate = 1;
    this.preload = "";
    this._loaded = false;
    FakeAudio.instances.push(this);
  }

  play() {
    this.paused = false;
    return Promise.resolve();
  }

  pause() {
    this.paused = true;
  }

  load() {
    this._loaded = true;
  }
}
FakeAudio.instances = [];

globalThis.Audio = FakeAudio;

const { voiceCaller } = await import("../../web/miniapp/js/voice.js");

function reset() {
  FakeAudio.instances.length = 0;
  voiceCaller.resetRound();
  voiceCaller.setEnabled(true);
}

// --- switching rooms actually silences an in-flight clip -------------
// The real reported bug: resetRound() used to only clear the *pending*
// queue, never touching whatever was already playing/loading -- on a
// slow connection, that clip could keep going well after the player had
// already moved to a different room.
{
  reset();
  voiceCaller.announce("B", 7, 1);
  assert.equal(FakeAudio.instances.length, 1, "announce() must start playing immediately when idle");
  const playing = FakeAudio.instances[0];
  assert.equal(playing.paused, false, "the clip should be playing before any reset");

  voiceCaller.resetRound();

  assert.equal(playing.paused, true, "resetRound() must pause the in-flight clip, not just clear the queue");
  assert.equal(playing.onended, null, "resetRound() must detach onended so a late event can't re-fire it");
  assert.equal(playing.onerror, null, "resetRound() must detach onerror so a late event can't re-fire it");
}

// --- a stale callback firing anyway must be a no-op -------------------
// Defense in depth: even if some environment still invokes a handler
// after it's been nulled out (or a test/caller held its own reference to
// it), it must never resurrect already-discarded playback state.
{
  reset();
  voiceCaller.announce("I", 20, 1);
  const stale = FakeAudio.instances[0];
  const staleOnEnded = stale.onended; // captured before resetRound() nulls the live reference
  voiceCaller.resetRound();
  voiceCaller.announce("N", 31, 2); // a genuinely new, current clip
  const current = FakeAudio.instances[1];

  staleOnEnded(); // the late/stray event from the *old* element

  assert.equal(voiceCaller._currentAudio, current, "a stale onended must not clear or advance past the real current clip");
  assert.equal(current.paused, false, "the real current clip must be unaffected by the stale event");
}

// --- the queue never grows past its cap on a slow connection ----------
{
  reset();
  voiceCaller.announce("B", 1, 1); // starts playing immediately, never completes in this test
  voiceCaller.announce("B", 2, 2);
  voiceCaller.announce("B", 3, 3);
  voiceCaller.announce("B", 4, 4);
  voiceCaller.announce("B", 5, 5);

  assert.ok(voiceCaller._queue.length <= 2, `queue grew to ${voiceCaller._queue.length}, expected <= 2`);
  // The most recent calls must survive; the oldest backlog is what gets
  // dropped, so the announcer catches back up to the live board instead
  // of working through a growing pile of stale numbers.
  const queuedNumbers = voiceCaller._queue.map((c) => c.number);
  assert.deepEqual(queuedNumbers, [4, 5], `expected the newest queued calls to survive, got ${queuedNumbers}`);
}

// --- disabling voice mid-announcement silences it, same as resetRound --
{
  reset();
  voiceCaller.announce("G", 50, 1);
  const playing = FakeAudio.instances[0];
  voiceCaller.setEnabled(false);
  assert.equal(playing.paused, true, "setEnabled(false) must stop whatever's currently playing");
  assert.equal(voiceCaller._queue.length, 0);
}

// --- preloadAll warms every one of the 75 real clip URLs --------------
{
  reset();
  voiceCaller.preloadAll();
  assert.equal(FakeAudio.instances.length, 75, "preloadAll() must touch exactly one Audio per real clip");
  const urls = new Set(FakeAudio.instances.map((a) => a.src));
  assert.equal(urls.size, 75, "every preloaded URL must be distinct");
  assert.ok([...urls].every((u) => u.startsWith("/audio/calls/")), "every preloaded URL must be a real call clip path");
  assert.ok(FakeAudio.instances.every((a) => a._loaded), "preloadAll() must call .load() on every clip");
}

console.log("ok");
