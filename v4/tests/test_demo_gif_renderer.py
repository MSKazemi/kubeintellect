"""The GIF renderer must report the file it actually wrote.

`scripts/demo/cast_to_gif.py` decides which terminal states become frames, and then stamps a
duration on each one. Both halves were wrong in a way that no eyeball catches:

* Frames were deduplicated on a *cell signature* -- the characters and attributes in the pyte
  buffer. Two different characters can paint identical pixels (every glyph the font lacks draws
  the same nothing, and a spinner cycling through such glyphs is exactly that case), so the
  frame list filled with pixel-identical entries. Pillow drops those on save and folds their
  durations into the frame before them. The renderer then printed a frame count and a length
  that were not the frame count and length of the GIF on disk -- 436 frames and 178s reported
  for a file holding 26 frames and 143s.
* Because the duplicates did not survive, `--min-frame-ms` and `--max-frame-ms` were applied to
  frames that were never written. A cap meant to compress a 50-second wait did nothing at all,
  and a floor meant to keep content on screen inflated the total instead.

These tests pin the invariant that makes the flags mean something: what the renderer says it
wrote is what a reader of the GIF gets.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
pytest.importorskip("pyte")
from PIL import Image, ImageSequence  # noqa: E402

_RENDERER = Path(__file__).resolve().parents[2] / "scripts" / "demo" / "cast_to_gif.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("cast_to_gif", _RENDERER)
    assert spec and spec.loader, f"cannot load {_RENDERER}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


renderer = _load_renderer()


def _write_cast(path: Path, events: list[tuple[float, str]], cols: int = 40, rows: int = 10) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"version": 2, "width": cols, "height": rows}) + "\n")
        for t, payload in events:
            fh.write(json.dumps([t, "o", payload]) + "\n")


def _render(cast: Path, out: Path, *flags: str) -> tuple[int, float]:
    """Run the renderer as the recorder does, and return what it *claims* it wrote."""
    argv = [str(_RENDERER), str(cast), str(out), *flags]
    old, sys.argv = sys.argv, argv
    try:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            renderer.main()
    finally:
        sys.argv = old
    line = buf.getvalue().strip()
    # "<out>: N frames, WxH, T.Ts"
    n = int(line.split(":")[-1].split("frames")[0].strip().split()[-1])
    seconds = float(line.rsplit(",", 1)[1].strip().rstrip("s"))
    return n, seconds


def _file_frames(out: Path) -> list[int]:
    im = Image.open(out)
    return [f.info.get("duration", 0) for f in ImageSequence.Iterator(im)]


def _blank_glyphs() -> list[str]:
    """Two distinct characters the demo font paints as nothing.

    The point of the test is a *pixel* collision between different cell contents. Asserting the
    collision exists rather than assuming it keeps the test from quietly passing on a machine
    whose font happens to carry these codepoints.
    """
    font, _ = renderer.load_fonts(14)
    found = []
    # Plane 14 tag characters, not the private-use planes: Nerd Fonts patch the PUA heavily,
    # so a search that starts there finds glyphs and the collision never happens.
    for cp in range(0xE0000, 0xE0080):
        ch = chr(cp)
        if font.getmask(ch).getbbox() is None:
            found.append(ch)
        if len(found) == 2:
            break
    return found


@pytest.fixture
def cast(tmp_path: Path) -> Path:
    """A cast that spends most of its length waiting, then paints in a burst.

    This is the shape of every real recording: the model thinks for tens of seconds and then
    streams an answer in a few hundred milliseconds. Uniform frames would not exercise either
    clamp.
    """
    p = tmp_path / "in.cast"
    _write_cast(p, [
        (0.0, "first line\r\n"),
        (0.2, "second line\r\n"),
        (12.0, "after a long wait\r\n"),   # 12s with nothing happening
        (12.2, "and one more\r\n"),
        (24.0, "done\r\n"),                # another long wait
    ])
    return p


def test_the_frame_count_it_reports_is_the_frame_count_in_the_file(cast: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.gif"
    reported, _ = _render(cast, out, "--fps", "8")
    assert reported == len(_file_frames(out)), (
        "the renderer counted frames Pillow then merged away; every duration flag below is "
        "being applied to frames that never reach the file"
    )


def test_the_length_it_reports_is_the_length_of_the_file(cast: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.gif"
    _, seconds = _render(cast, out, "--fps", "8")
    assert abs(sum(_file_frames(out)) / 1000 - seconds) < 0.05


def test_the_cap_compresses_the_waiting(cast: Path, tmp_path: Path) -> None:
    """A 12-second stare at an unchanged screen is dead weight in a demo."""
    out = tmp_path / "out.gif"
    _render(cast, out, "--fps", "8", "--max-frame-ms", "2000", "--tail-hold", "0")
    durations = _file_frames(out)
    assert max(durations) <= 2000, durations
    assert sum(durations) < 24_000, f"the wait survived the cap: {durations}"


def test_the_floor_keeps_a_painted_frame_on_screen(cast: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.gif"
    _render(cast, out, "--fps", "8", "--min-frame-ms", "700", "--tail-hold", "0")
    assert min(_file_frames(out)) >= 700, _file_frames(out)


def test_a_floor_above_the_cap_yields_the_floor_and_not_a_silent_inversion(
    cast: Path, tmp_path: Path,
) -> None:
    """Order matters. Applying the floor first would turn `--min 900 --max 400` into 400ms."""
    out = tmp_path / "out.gif"
    _render(cast, out, "--fps", "8", "--min-frame-ms", "900", "--max-frame-ms", "400",
            "--tail-hold", "0")
    assert set(_file_frames(out)) == {900}, _file_frames(out)


def test_two_cells_that_paint_the_same_pixels_are_one_frame(tmp_path: Path) -> None:
    """The spinner case: different characters, identical output, must not become two frames."""
    glyphs = _blank_glyphs()
    if len(glyphs) < 2:
        pytest.skip("this font paints every candidate codepoint; no pixel collision to test")
    a, b = glyphs
    cast = tmp_path / "spin.cast"
    _write_cast(cast, [
        (0.0, "steady text\r\n"),
        (0.5, a), (1.0, f"\b{b}"), (1.5, f"\b{a}"), (2.0, f"\b{b}"),
        (3.0, "\r\ndone\r\n"),
    ])
    out = tmp_path / "spin.gif"
    reported, _ = _render(cast, out, "--fps", "8")
    assert reported == len(_file_frames(out))
    assert reported <= 3, (
        f"a spinner the font cannot draw produced {reported} frames of identical pixels"
    )


def test_the_geometry_comes_from_the_cast_header(tmp_path: Path) -> None:
    """Nothing may second-guess the recorded width; that is the width the client laid out for."""
    cast = tmp_path / "wide.cast"
    _write_cast(cast, [(0.0, "x" * 90 + "\r\n")], cols=100, rows=12)
    out = tmp_path / "wide.gif"
    _render(cast, out, "--fps", "8", "--font-size", "14", "--pad", "0")
    font, _ = renderer.load_fonts(14)
    cw = int(round(font.getlength("M")))
    assert Image.open(out).size[0] == 100 * cw


# The characters the client actually paints: the Braille spinner, the gear that prefixes every
# tool line, the tick in the investigation plan, the arrow, the box-drawing the panels use, and
# the risk marker on the approval banner. No single monospace font on this machine covers all of
# them -- and the marker is on the one frame the whole corpus exists to show.
TERMINAL_UI_GLYPHS = "⠋⠙⠹⠸⚙✓→│╭╮╰╯─🟡"


@pytest.mark.parametrize("ch", list(TERMINAL_UI_GLYPHS))
def test_every_glyph_the_client_paints_resolves_to_a_real_one(ch: str) -> None:
    """Not "a font was found" -- "this character is not the .notdef box".

    ``getmask(ch).getbbox()`` is not None for .notdef either: the tofu box has ink. Testing
    coverage that way passes while the demo renders a row of boxes, which is exactly what it did.
    """
    chain = renderer.load_font_chain(14)
    pick = renderer.font_picker(chain)
    font = pick(ch, False)
    notdef = bytes(font.getmask("￿"))
    assert bytes(font.getmask(ch)) != notdef, (
        f"{ch!r} (U+{ord(ch):04X}) renders as the missing-glyph box in every font in the chain"
    )


def test_the_font_chain_is_more_than_one_family() -> None:
    """A one-font chain cannot fall back, and that is how the tofu got in.

    If this fails on a fresh machine the fix is to install the fonts, not to relax the test:
    a renderer with a single family will silently draw boxes for the spinner.
    """
    assert len(renderer.load_font_chain(14)) >= 2, (
        f"only {len(renderer.load_font_chain(14))} of {len(renderer.FONT_CANDIDATES)} font "
        "families loaded; check the paths in FONT_CANDIDATES"
    )


def test_a_named_font_pair_either_loads_or_is_absent_but_never_half_loads() -> None:
    """The bug this replaces: one filename template for four families that name weights
    differently, so a family whose bold did not match the template dropped out of the chain
    without a word.
    """
    from PIL import ImageFont
    for regular, bold in renderer.FONT_CANDIDATES:
        have = []
        for path in (regular, bold):
            try:
                ImageFont.truetype(path, 14)
                have.append(True)
            except OSError:
                have.append(False)
        assert have[0] == have[1], (
            f"half a family: regular={regular} present={have[0]}, bold={bold} present={have[1]}"
        )


def test_a_spinner_is_not_content(tmp_path: Path) -> None:
    """One cell oscillating forever is an animation, and an animation is dead time.

    This is what a real cast is mostly made of. Before the font chain the spinner glyphs had no
    coverage and all painted the same tofu, so pixel deduplication hid this by accident; giving
    them real glyphs turned 60% of every recording into frames and put the GIFs back over two
    minutes. The screen is what changed, not the renderer's job.
    """
    p = tmp_path / "spin.cast"
    events = [(0.0, "waiting ")]
    events += [(0.5 + i * 0.125, "\b" + "⠋⠙⠹⠸"[i % 4]) for i in range(160)]  # 20s of spinner
    events.append((22.0, "\rdone     \r\n"))
    _write_cast(p, events)
    n, _ = _render(p, tmp_path / "spin.gif", "--fps", "8", "--max-frame-ms", "2000")
    assert n <= 4, f"{n} frames for one cell of spinner and one line of content"
    assert n == len(_file_frames(tmp_path / "spin.gif"))


def test_text_arriving_one_character_at_a_time_is_content(tmp_path: Path) -> None:
    """The other half of the rule, and the reason the comparison is against the last *kept*
    frame rather than the last one sampled. A prompt typed at human speed changes one cell per
    frame -- exactly like a spinner -- but the changes accumulate, so within a few characters it
    has moved past the threshold. Comparing consecutive frames instead would collapse the typing
    into a single jump and lose the part of the demo where the operator asks the question.
    """
    p = tmp_path / "type.cast"
    events = [(0.5 + i * 0.125, ch) for i, ch in enumerate("why is payments-api failing?")]
    events.append((6.0, "\r\n"))
    _write_cast(p, events)
    n, _ = _render(p, tmp_path / "type.gif", "--fps", "8", "--idle-cells", "2")
    assert n >= 6, f"only {n} frames: typing was collapsed into the wait"


def test_the_idle_threshold_is_a_knob_and_not_a_constant(tmp_path: Path) -> None:
    """Raising it must drop frames, or the flag is decorative. Pinned because the rule is a
    heuristic: it was chosen by measuring the corpus (every real update changes >= 12 cells,
    the spinner exactly 1), and a corpus with a busier status line would need it moved.
    """
    p = tmp_path / "few.cast"
    # Each burst repaints a handful of cells -- above the default bar, below the raised one.
    _write_cast(p, [(i * 2.0, f"\rrow {i}") for i in range(1, 6)])
    tight, _ = _render(p, tmp_path / "tight.gif", "--fps", "8", "--idle-cells", "0")
    loose, _ = _render(p, tmp_path / "loose.gif", "--fps", "8", "--idle-cells", "40")
    assert loose < tight, f"raising the threshold kept {loose} frames against {tight}"
