# Bingo call audio clips

This directory holds one pre-generated `.mp3` per Bingo number, played by
`web/miniapp/js/voice.js` when the engine calls that number. No `.mp3`
files are committed here (`.gitignore` excludes them) — this directory
ships only the naming/text contract so real audio can be dropped in
later, from any source, with zero code changes.

## Naming

`{LETTER}_{NN}.mp3` — the Bingo letter (English, uppercase), an
underscore, and the number zero-padded to two digits:

```
B_01.mp3 ... B_15.mp3
I_16.mp3 ... I_30.mp3
N_31.mp3 ... N_45.mp3
G_46.mp3 ... G_60.mp3
O_61.mp3 ... O_75.mp3
```

Requested by `voice.js` at `/audio/calls/{LETTER}_{NN}.mp3`, served
automatically by the gateway's existing static mount — no route changes
needed.

## What each clip should say

`MANIFEST.json` maps every filename to its exact script, e.g.
`"G_56": "ጂ , ሃምሳ ስድስት"` — **the Bingo letter spoken as its own English
letter name, spelled phonetically in Amharic script (never a bare Latin
character -- see "Real TTS findings" below for why), a comma for a
natural pause, then the number spoken in Amharic.** This is the file to
hand to a TTS vendor or a voice actor. The number words are generated
from two real sources, never hand-typed, so they can't drift from them:

- the Bingo letter ranges in `packages/core/bingo.py` (`letter_for()`)
- the Amharic number words in `web/miniapp/js/amharic_numbers.js`

`tests/unit/test_amharic_number_mapping.py` verifies all three (the JS
word map, `MANIFEST.json`, and `bingo.py`'s ranges) stay consistent with
each other. If either source ever changes, regenerate `MANIFEST.json`
with:

```bash
python3 -c "
import json, subprocess
from packages.core.bingo import letter_for
words = json.loads(subprocess.run(
    ['node', 'tests/frontend/dump_amharic_numbers.mjs'], capture_output=True, text=True, check=True
).stdout)
LETTER_PREFIX = {'B': 'ቢ', 'I': 'አይ', 'N': 'ኤን', 'G': 'ጂ', 'O': 'ኦ'}  # see 'Real TTS findings' below
manifest = {
    f'{letter_for(n)}_{n:02d}': f'{LETTER_PREFIX[letter_for(n)]} , {words[str(n)]}'
    for n in range(1, 76)
}
json.dump(manifest, open('web/miniapp/audio/calls/MANIFEST.json', 'w'), ensure_ascii=False, indent=2, sort_keys=True)
"
```

## Real TTS findings

Two engines were tried, each verified directly (not just guessed from
listening) by generating real audio and round-tripping it through Addis
AI's own Speech-to-Text to check what it actually said:

- **Addis AI's TTS API was tried first and rejected.** Exclamation marks
  made output worse (dropping them helped); worse, a bare `"B"`
  deterministically triggered an 8-11 second clip of completely
  unrelated garbled Amharic instead of the letter -- the *same* garbled
  output every time for the same input, so retrying was never going to
  fix it. Spelling `"B"` as `"ቢ"` ("bee") helped inconsistently: some
  B-column numbers came out clean, others (e.g. `"ቢ , አራት"`) still
  produced the same multi-second hallucination. This turned out to be
  general instability on short (2-3 word) prompts with that specific
  voice, not something fixable by rewording.
- **Microsoft Edge's free Neural TTS (`edge-tts` package, voice
  `am-ET-AmehaNeural`) is what's actually used.** Every letter spelled
  phonetically in Amharic (`LETTER_PREFIX` above -- not just `"B"`, on
  the theory that a bare Latin character is the more fragile input
  regardless of engine) with a comma pause before the number came back
  clean and correctly transcribed on every clip spot-checked. No API
  key needed -- it's a free public Microsoft endpoint.
- If the voice or engine ever changes again, re-run the same per-clip
  STT-verified check before trusting a new script is actually correct --
  this codebase has now hit the same "sounds fine to write, garbles in
  practice" failure mode once already.

`web/miniapp/audio/calls/generate_call_audio.py` is the real generation
script.

## Voice direction

A professional Bingo caller: energetic but clear, not rushed. The
letter and number should sound like two distinct beats, not run
together.

## Until real clips exist

`voice.js` handles a missing file gracefully: it logs one console
warning per missing filename and moves on to the next queued call.
Nothing in the game depends on these files existing.
