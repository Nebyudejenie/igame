"""Generates the 75 real Bingo call clips (B_01.mp3 ... O_75.mp3) from
MANIFEST.json via Microsoft Edge's free Neural TTS (`edge-tts`), voice
am-ET-AmehaNeural -- a real, Microsoft-maintained native Amharic (Male)
voice. Chosen after Addis AI's TTS API (tried first) proved unreliable on
these short, 2-3 word phrases: some inputs deterministically produced
8-11 second garbled hallucinations instead of the intended clip (same
input, same broken output every time -- not something a retry loop can
fix). Every clip from edge-tts was spot-checked by round-tripping it
through Addis AI's own Speech-to-Text (a neutral, independent check) and
came back clean.

The generated .mp3 files are deliberately not committed to git (this
directory's own .gitignore excludes them, per README.md) -- this script
is what actually produces them, with the resulting files deployed
straight to each environment rather than carried through version control.

Run: `python3 web/miniapp/audio/calls/generate_call_audio.py`
No API key needed -- edge-tts talks to Microsoft's free public endpoint.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import edge_tts

VOICE = "am-ET-AmehaNeural"  # Microsoft's native male Amharic Neural voice.
RATE = "-10%"  # Slightly slower than default -- a natural, unhurried caller.
PITCH = "-12Hz"  # Deeper, bolder tone.
VOLUME = "+10%"  # A touch louder for clarity over game audio/haptics.

# 'B'/'I'/'N'/'G'/'O' spoken as their own English letter *names*, spelled
# phonetically in Amharic script -- keeps every clip in-script for the
# TTS engine (never a bare Latin character, which is what triggered
# Addis AI's hallucinations) regardless of which engine renders it.
LETTER_PREFIX = {"B": "ቢ", "I": "አይ", "N": "ኤን", "G": "ጂ", "O": "ኦ"}

HERE = Path(__file__).parent
MANIFEST_PATH = HERE / "MANIFEST.json"

# A generous but bounded concurrency limit -- edge-tts talks to a public
# Microsoft endpoint with no documented rate limit, but hammering it with
# all 75 requests genuinely concurrently would be an unfriendly, spiky
# burst for zero real speed benefit at this small a volume.
MAX_CONCURRENT_REQUESTS = 5


async def generate_one(semaphore: asyncio.Semaphore, filename: str, text: str) -> tuple[str, Exception | None]:
    async with semaphore:
        try:
            communicate = edge_tts.Communicate(
                text=text, voice=VOICE, rate=RATE, pitch=PITCH, volume=VOLUME
            )
            out_path = HERE / f"{filename}.mp3"
            await communicate.save(str(out_path))
            return filename, None
        except Exception as exc:  # noqa: BLE001 -- reported by the caller, not raised here
            return filename, exc


async def main_async() -> int:
    manifest: dict[str, str] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    results = await asyncio.gather(
        *(generate_one(semaphore, filename, text) for filename, text in manifest.items())
    )

    ok = 0
    failed: list[str] = []
    for filename, error in sorted(results):
        if error is None:
            size = (HERE / f"{filename}.mp3").stat().st_size
            print(f"{filename} <- {manifest[filename]!r} ({size} bytes)")
            ok += 1
        else:
            print(f"{filename} FAILED: {error}", file=sys.stderr)
            failed.append(filename)

    print(f"\n{ok}/{len(manifest)} generated successfully.")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
