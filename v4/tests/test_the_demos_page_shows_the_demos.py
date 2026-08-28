"""The demos page must actually show the demos it documents.

`scripts/demo/DEMOS.md` spent its whole life describing eight recorded scenarios in 352 lines
and embedding **no image at all**. Sixteen rendered GIFs and two browser-UI GIFs were committed
to the public repository, referenced only by a directory listing at the bottom of the page, and
displayed to nobody. Rendering an asset and committing it are not the same act as showing it,
and nothing failed when the last step was skipped -- the GIFs were present, correct, and
unreachable, which is indistinguishable from not having recorded them.

That had already happened twice before on other assets, which is why it is a gate now rather
than a fix. Two directions are checked, because only both together mean "the page works":

* every GIF of the **current** corpus is embedded somewhere on the page -- a new recording that
  nobody links is caught at the point it is added, not months later;
* every `src` the page embeds resolves to a file on disk -- a renamed or deleted recording is
  caught before it reaches a reader as a broken image.

The superseded `gifs/` corpus is deliberately exempt: the page keeps it as a record and says so.
"""
from __future__ import annotations

import re
from pathlib import Path

_DEMO_DIR = Path(__file__).resolve().parents[2] / "scripts" / "demo"
_PAGE = _DEMO_DIR / "DEMOS.md"

_SRC_RE = re.compile(r'<img\s[^>]*src="([^"]+)"', re.I)
_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _embedded() -> set[str]:
    text = _PAGE.read_text(encoding="utf-8")
    return set(_SRC_RE.findall(text)) | set(_MD_RE.findall(text))


def test_every_current_corpus_gif_is_embedded() -> None:
    """A recording the page does not show is a recording that was not published."""
    embedded = {s.split("/")[-1] for s in _embedded()}
    expected = sorted(p.name for p in (_DEMO_DIR / "gifs-kq").glob("*.gif"))
    assert expected, "the kq corpus is missing entirely -- gifs-kq/ holds no GIF"
    missing = [name for name in expected if name not in embedded]
    assert not missing, (
        "rendered, committed, and shown on no page: "
        + ", ".join(f"gifs-kq/{n}" for n in missing)
        + " -- embed them in DEMOS.md"
    )


def test_the_browser_ui_recording_is_embedded() -> None:
    """The chat UI is the surface most people meet first; it was the least visible."""
    embedded = {s.split("/")[-1] for s in _embedded()}
    for name in sorted(p.name for p in (_DEMO_DIR / "chat-ui").glob("*.gif")):
        assert name in embedded, f"chat-ui/{name} is committed but embedded nowhere"


def test_every_embedded_path_resolves() -> None:
    """The other direction: a rename must not leave a reader with a broken image."""
    broken = [
        src
        for src in sorted(_embedded())
        if not src.startswith(("http://", "https://", "data:"))
        and not (_DEMO_DIR / src).is_file()
    ]
    assert not broken, f"DEMOS.md embeds paths that do not exist: {broken}"
