"""Runs tests/frontend/test_voice_caller.mjs -- a plain-node smoke test
for web/miniapp/js/voice.js, since there's no JS test framework anywhere
in this repo (the Mini App is deliberately framework-free vanilla JS) and
this is client-side audio-queue/timing logic with nothing for the Python
integration/unit suites to exercise directly.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "frontend" / "test_voice_caller.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_voice_caller_stops_stale_audio_and_caps_the_queue():
    result = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
