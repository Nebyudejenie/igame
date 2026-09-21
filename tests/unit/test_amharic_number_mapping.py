"""Verifies all 75 Bingo numbers have correct Amharic pronunciation and the
correct Bingo letter -- the feature's own explicit closing requirement.
Cross-checks three real sources against each other (never a hand-copied
range table or word list that could silently drift):

- web/miniapp/js/amharic_numbers.js's real AMHARIC_NUMBER_WORDS map, read
  via a node subprocess (tests/frontend/dump_amharic_numbers.mjs), not
  re-implemented in Python.
- packages/core/bingo.py's real letter_for(), the same function the
  engine itself uses to build the "call" WS message the client renders.
- web/miniapp/audio/calls/MANIFEST.json, the file-naming/text contract a
  real TTS pass or voice actor would work from.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from packages.core.bingo import letter_for

REPO_ROOT = Path(__file__).parent.parent.parent
DUMP_SCRIPT = REPO_ROOT / "tests" / "frontend" / "dump_amharic_numbers.mjs"
SMOKE_TEST = REPO_ROOT / "tests" / "frontend" / "test_amharic_numbers.mjs"
MANIFEST_PATH = REPO_ROOT / "web" / "miniapp" / "audio" / "calls" / "MANIFEST.json"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _amharic_words() -> dict[int, str]:
    result = subprocess.run(["node", str(DUMP_SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return {int(k): v for k, v in json.loads(result.stdout).items()}


def test_the_js_smoke_test_itself_passes():
    result = subprocess.run(["node", str(SMOKE_TEST)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_every_number_1_to_75_has_a_nonempty_amharic_word():
    words = _amharic_words()
    assert set(words) == set(range(1, 76))
    for n, word in words.items():
        assert isinstance(word, str) and word.strip(), f"number {n} has no Amharic word"


def test_manifest_filenames_match_bingo_letter_ranges_exactly():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(manifest) == 75

    expected_keys = set()
    for n in range(1, 76):
        letter = letter_for(n)
        key = f"{letter}_{n:02d}"
        expected_keys.add(key)
        assert key in manifest, f"manifest is missing {key}"

    assert set(manifest) == expected_keys, (
        f"manifest keys don't match bingo.py's ranges exactly: "
        f"extra={set(manifest) - expected_keys}, missing={expected_keys - set(manifest)}"
    )


# Every Bingo letter spoken as its own English letter *name*, spelled
# phonetically in Amharic script -- keeps every clip in-script for
# whichever TTS engine renders it (services/... no, this is a static
# audio asset, not app code -- see generate_call_audio.py) rather than a
# bare Latin character. A bare 'B' specifically was confirmed (against
# Addis AI's TTS, tried first) to deterministically produce 8-11s of
# unrelated garbled Amharic instead of the letter; spelling every letter
# out phonetically is the voice-agnostic fix, not a workaround for one
# engine's own quirk.
LETTER_PREFIX = {"B": "ቢ", "I": "አይ", "N": "ኤን", "G": "ጂ", "O": "ኦ"}


def test_manifest_scripts_say_the_english_letter_and_amharic_number():
    words = _amharic_words()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for n in range(1, 76):
        letter = letter_for(n)
        key = f"{letter}_{n:02d}"
        expected = f"{LETTER_PREFIX[letter]} , {words[n]}"
        assert manifest[key] == expected, (
            f"{key}: manifest says {manifest[key]!r}, expected {expected!r}"
        )


@pytest.mark.parametrize(
    "number,letter,word",
    [
        (7, "B", "ሰባት"),
        (22, "I", "ሃያ ሁለት"),
        (43, "N", "አርባ ሦስት"),
        (56, "G", "ሃምሳ ስድስት"),
        (74, "O", "ሰባ አራት"),
    ],
)
def test_the_five_worked_examples_from_the_feature_request(number, letter, word):
    assert letter_for(number) == letter
    words = _amharic_words()
    assert words[number] == word
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest[f"{letter}_{number:02d}"] == f"{LETTER_PREFIX[letter]} , {word}"
