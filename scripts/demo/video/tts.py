"""Synthesise one narration WAV per enabled scene with Piper, and record durations.

Piper is **not** deterministic: re-synthesising identical text yields a different waveform
and a slightly different duration. Re-running over every scene would therefore change the
whole narration track — and force a full re-watch — even when only one line was edited. So
each WAV is cached against a SHA-256 of the text that produced it, and only genuinely
changed scenes are re-synthesised.

Scenes with `enabled=False` are skipped: their footage does not exist, so synthesising them
would put audio in `durations.json` for a scene the build cannot render.

**The voice model is not vendored here.** `en_US-ryan-high.onnx` is 120 MB; this repository
points at the copy already on the machine rather than carrying a second one. Override either
path if it moves:

    KI_PIPER=/path/to/piper KI_PIPER_VOICE=/path/to/en_US-ryan-high.onnx python3 tts.py

    python3 tts.py            # only scenes whose narration changed
    python3 tts.py --force    # re-synthesise everything
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import wave

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import scenes  # noqa: E402

_NOVA = pathlib.Path("/home/mohsen/scratch/repos/nova/experiments/azure-2026-08-27/video")
PIPER = pathlib.Path(os.environ.get("KI_PIPER", _NOVA / ".tts/bin/piper"))
VOICE = pathlib.Path(os.environ.get("KI_PIPER_VOICE", _NOVA / "voices/en_US-ryan-high.onnx"))

AUD = V / "audio"
STAMPS = AUD / ".narration-hashes.json"
FORCE = "--force" in sys.argv

for tool, what in ((PIPER, "piper binary"), (VOICE, "voice model")):
    if not tool.exists():
        sys.exit(f"{what} not found at {tool} — set KI_PIPER / KI_PIPER_VOICE")

AUD.mkdir(exist_ok=True)
stamps = json.loads(STAMPS.read_text(encoding="utf-8")) if STAMPS.exists() else {}
durations: dict[str, float] = {}
made = skipped = 0

for sc in scenes.SCENES:
    if not sc.get("enabled", True):
        continue
    out = AUD / f"{sc['id']}.wav"
    text = " ".join(sc["narration"].split())          # one line, one utterance
    digest = hashlib.sha256(text.encode()).hexdigest()

    if not FORCE and out.exists() and stamps.get(sc["id"]) == digest:
        with wave.open(str(out)) as w:
            durations[sc["id"]] = round(w.getnframes() / w.getframerate(), 3)
        skipped += 1
        continue

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(text + "\n")
        tmp = fh.name
    subprocess.run(
        [str(PIPER), "-m", str(VOICE), "-i", tmp, "-f", str(out),
         "--length-scale", "1.04", "--sentence-silence", "0.32"],
        check=True, capture_output=True)
    pathlib.Path(tmp).unlink()
    with wave.open(str(out)) as w:
        d = w.getnframes() / w.getframerate()
    durations[sc["id"]] = round(d, 3)
    stamps[sc["id"]] = digest
    made += 1
    print(f"  synthesised {sc['id']:<16} {d:6.2f}s   ({len(text.split())} words)")

STAMPS.write_text(json.dumps(stamps, indent=2, sort_keys=True), encoding="utf-8")
(V / "durations.json").write_text(json.dumps(durations, indent=2), encoding="utf-8")
t = sum(durations.values())
print(f"\n{made} synthesised, {skipped} reused unchanged, "
      f"{sum(1 for s in scenes.SCENES if not s.get('enabled', True))} skipped (not enabled)")
print(f"TOTAL narration: {t:.1f}s = {int(t // 60)}m{int(t % 60):02d}s")
