"""Assemble the KubeIntellect narrated demo: frames -> ffmpeg, narration -> WAV, mux.

Adapted from `nova/experiments/azure-2026-08-27/video/build.py`. Two changes: scenes with
`enabled=False` are skipped (their footage does not exist), and a terminal scene is loaded
with the line window it declares.

A scene lasts `narration + PAD_IN + PAD_OUT` and nothing else — the transcript is revealed
*over* the narration, so a longer transcript does not make the video longer. The runtime is
exactly as long as the words.
"""
from __future__ import annotations

import json, pathlib, subprocess, sys, wave

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import render as R  # noqa: E402
import scenes as S  # noqa: E402

FPS = R.FPS
PAD_IN, PAD_OUT = 0.45, 0.85
OUT = V / "out"; OUT.mkdir(exist_ok=True)
VIDEO = OUT / "kubeintellect-demo.mp4"


def build_audio(plan) -> pathlib.Path:
    """One WAV: PAD_IN silence + narration + PAD_OUT silence, per scene."""
    dst = OUT / "narration.wav"
    with wave.open(str(V / f"audio/{plan[0][0]['id']}.wav")) as w:
        params = w.getparams()
    rate, sw, ch = params.framerate, params.sampwidth, params.nchannels
    with wave.open(str(dst), "wb") as out:
        out.setnchannels(ch); out.setsampwidth(sw); out.setframerate(rate)
        for sc, dur in plan:
            with wave.open(str(V / f"audio/{sc['id']}.wav")) as w:
                data = w.readframes(w.getnframes())
            speech = len(data) // (sw * ch) / rate
            tail = max(0.0, dur - PAD_IN - speech)
            out.writeframes(b"\x00" * int(rate * PAD_IN) * sw * ch)
            out.writeframes(data)
            out.writeframes(b"\x00" * int(rate * tail) * sw * ch)
    return dst


def state_key(sc, t, dur, lines):
    """Frames sharing a key are visually identical apart from the progress bar."""
    if sc["kind"] == "card":
        ease = min(1.0, round(t / 0.55, 2))
        n = len(sc.get("bullets") or [])
        vis = sum(1 for i in range(n)
                  if t >= 0.7 + i * (dur * 0.45 / max(1, n)) + 0.5)
        part = round(min(1.0, max(0.0, (t - 0.7) % 1.0)), 1) if vis < n else 0
        # links fade in on their own schedule; without them in the key every
        # frame after the 0.55 s title fade collapses onto one cached image.
        links = tuple(round(max(0.0, min(1.0, (t - (1.15 + i * 0.4)) / 0.5)), 2)
                      for i in range(len(sc.get("links") or [])))
        return ("c", ease, vis, part, links)
    if sc["kind"] == "terminal":
        reveal = min(1.0, t / (dur * 0.82) if dur else 1.0)
        shown = max(1, int(round(reveal * len(lines))))
        return ("t", shown, int(t * 2.4) % 2 if reveal < 1.0 else 2)
    if sc["kind"] == "flow":
        # only the lit-stage count and the fade change; everything else is static
        span = dur * 0.62
        active = min(len(R.FLOW_NODES) - 1, int(t / span * len(R.FLOW_NODES))) if span else 0
        return ("f", min(1.0, round(t / 0.5, 2)), active)
    seq = R.shot_frames(sc["source"])
    if seq:
        # a clip advances per source frame; rounding scene-time to 0.1 s would drop it to 10 fps
        return ("s", R.shot_frame_index(sc, t, dur, len(seq)))
    return ("s", round(t, 1))


def main() -> None:
    durations = json.loads((V / "durations.json").read_text(encoding="utf-8"))
    enabled = [sc for sc in S.SCENES if sc.get("enabled", True)]
    plan = [(sc, durations[sc["id"]] + PAD_IN + PAD_OUT) for sc in enabled]
    total = sum(d for _, d in plan)
    print(f"target: {total:.1f}s = {int(total//60)}m{int(total%60):02d}s")

    audio = build_audio(plan)
    print(f"narration track: {audio}")

    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{R.W}x{R.H}", "-r", str(FPS), "-i", "-",
         "-i", str(audio),
         "-vf", f"fade=t=in:st=0:d=1.0,fade=t=out:st={total - 1.2:.2f}:d=1.2",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
         "-g", str(FPS * 2), "-movflags", "+faststart",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-shortest", str(VIDEO)],
        stdin=subprocess.PIPE)

    n = 0
    elapsed = 0.0
    for sc, dur in plan:
        lines = (R.load_transcript(sc["source"], sc.get("lines"))
                 if sc["kind"] == "terminal" else [])
        nf = int(round(dur * FPS))
        last_key = None
        base = None
        for i in range(nf):
            t = i / FPS
            progress = (elapsed + t) / total
            key = state_key(sc, t, dur, lines)
            if key != last_key or base is None:
                if sc["kind"] == "card":
                    base = R.render_card(sc, t, dur, 0.0)
                elif sc["kind"] == "terminal":
                    base = R.render_terminal(sc, t, dur, 0.0, lines)
                elif sc["kind"] == "flow":
                    base = R.render_flow(sc, t, dur, 0.0)
                else:
                    base = R.render_shot(sc, t, dur, 0.0)
                last_key = key
            frame = base.copy()
            R.ImageDraw.Draw(frame).rectangle(
                (0, R.H - 5, int(R.W * progress), R.H), fill=R.ACCENT)
            ff.stdin.write(frame.tobytes())
            n += 1
        elapsed += dur
        print(f"  {sc['id']:<16} {dur:6.2f}s  {nf:5d} frames")

    ff.stdin.close()
    rc = ff.wait()
    print(f"\nffmpeg exit {rc}; {n} frames written")
    if rc != 0:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
