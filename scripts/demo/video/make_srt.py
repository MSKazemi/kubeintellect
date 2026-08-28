"""Generate a SubRip subtitle track aligned to the narration timeline.

Adapted from the nova pipeline; the substitution table lives in `scenes.py` as `SUBS`.
"""
import json, pathlib, re, sys, wave

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import scenes as S  # noqa: E402

PAD_IN, PAD_OUT = 0.45, 0.85
MAX_CHARS = 84                      # two comfortable lines


# The narration is written phonetically so Piper pronounces acronyms correctly. Subtitles
# are *read*, not heard, so undo that spelling here. The table lives in scenes.py next to
# the narration it applies to; it is applied longest-pattern-first because several overlap
# ("A I S R E" must not become "AI S R E").
SUBS = sorted(S.SUBS.items(), key=lambda kv: -len(kv[0]))


def readable(text: str) -> str:
    for a, b in SUBS:
        text = text.replace(a, b)
    return text


def stamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".replace(".", ",")


def chunks(text: str) -> list[str]:
    """Split narration into subtitle-sized cues on sentence then clause bounds."""
    parts, buf = [], ""
    for sent in re.split(r"(?<=[.?!])\s+", " ".join(text.split())):
        if len(buf) + len(sent) + 1 <= MAX_CHARS:
            buf = (buf + " " + sent).strip()
        else:
            if buf:
                parts.append(buf)
            while len(sent) > MAX_CHARS:
                cut = sent.rfind(" ", 0, MAX_CHARS)
                cut = cut if cut > 30 else MAX_CHARS
                parts.append(sent[:cut].strip()); sent = sent[cut:].strip()
            buf = sent
    if buf:
        parts.append(buf)
    return parts


def main() -> None:
    durations = json.loads((V / "durations.json").read_text(encoding="utf-8"))
    out, n, t = [], 1, 0.0
    for sc in S.SCENES:
        if not sc.get("enabled", True):
            continue
        speech = durations[sc["id"]]
        cues = chunks(readable(sc["narration"]))
        weights = [len(c) for c in cues]
        span = speech / sum(weights)
        cur = t + PAD_IN
        for c, w in zip(cues, weights):
            end = cur + w * span
            out.append(f"{n}\n{stamp(cur)} --> {stamp(end - 0.05)}\n{c}\n")
            n += 1
            cur = end
        t += speech + PAD_IN + PAD_OUT
    (V / "out/kubeintellect-demo.srt").write_text("\n".join(out), encoding="utf-8")
    print(f"{n-1} cues, ends at {stamp(t)}")


if __name__ == "__main__":
    main()
