"""The demo video is a claim surface, and until 2026-08-28 nothing checked it.

`scripts/demo/video/scenes.py` calls itself the single source of truth and says every factual
claim in it is checked against a file in this repository. That was true of the *terminal*
scenes — each names the transcript and line numbers it replays — and untrue of the cards, where
the product claims live. Nothing read those. The owner asked for exactly this check, and it
found two:

* **"read-only by default"**, said twice, was false. `core/config.py` § *Production auth
  hardening* records the opposite: with no keys configured the server treats every
  unauthenticated caller as **admin**, and `REQUIRE_AUTH` is off by default. The safe thing the
  product actually does is stop mutating commands at a gate — which is a different sentence.
* **"a role per API key — read only, operator, admin"** named three of four roles and did not
  say that roles exist only once keys are configured.

The colours were a second kind of unchecked claim: the accent was `#7c8cf8`, commented
"KubeIntellect indigo", which appears in no stylesheet and no shipped mark — it came from the
upstream build this renderer was adapted from, along with a `logo()` that drew a hexagon with a
`K` in it. The project's mark is a chevron, `ki`, and a cursor block.

So these tests pin three things: no retired claim comes back, the claims that remain are tied
to the code fact that forced their wording, and the mark is drawn from the shipped brand asset
rather than from memory. What they cannot check is the *website*, which is a separate
repository — the values taken from it are recorded in `render.py` beside the file they came
from, and the in-repo brand SVG is what the mark itself is compared against.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "scripts" / "demo" / "video"
BRAND = ROOT / "v4" / "docs" / "assets" / "brand" / "ki-c-green.svg"
CONFIG = (ROOT / "v4" / "packages" / "kubeintellect-server" / "app" / "core" / "config.py")


def _load(name: str):
    """Import a module out of `scripts/demo/video/`, which is not a package."""
    if not (VIDEO / f"{name}.py").exists():
        pytest.skip(f"{name}.py is not present")
    sys.path.insert(0, str(VIDEO))
    try:
        spec = importlib.util.spec_from_file_location(f"_video_{name}", VIDEO / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(VIDEO))


@pytest.fixture(scope="module")
def scenes():
    return _load("scenes")


@pytest.fixture(scope="module")
def spoken(scenes) -> str:
    """Everything the video says or shows on a card, lowercased."""
    parts = []
    for sc in scenes.SCENES:
        if not sc.get("enabled"):
            continue
        parts += [sc.get("narration", ""), sc.get("title", ""), sc.get("subtitle", "")]
        for head, sub in sc.get("bullets") or []:
            parts += [head, sub]
    return " ".join(parts).lower()


class TestTheRetiredClaimsStayRetired:
    def test_it_does_not_claim_to_be_read_only_by_default(self, spoken):
        assert "read-only by default" not in spoken
        assert "read only by default" not in spoken

    def test_it_does_not_promise_read_only_until_you_say_otherwise(self, spoken):
        assert "until you say otherwise" not in spoken

    def test_the_code_fact_that_forced_that_wording_still_holds(self):
        """If the default ever becomes read-only, this fails and the video may say so again."""
        text = CONFIG.read_text(encoding="utf-8")
        assert "treats EVERY unauthenticated caller as `admin`" in text
        assert re.search(r"REQUIRE_AUTH: bool = False", text), "auth is now required by default"

    def test_the_role_claim_names_all_four_roles(self, spoken):
        for role in ("read only", "operator", "admin", "superadmin"):
            assert role in spoken, role

    def test_the_four_roles_are_the_ones_the_server_has(self):
        text = CONFIG.read_text(encoding="utf-8")
        for key in ("SUPERADMIN", "ADMIN", "OPERATOR", "READONLY"):
            assert f"KUBEINTELLECT_{key}_KEYS" in text, key

    def test_the_closing_does_not_spell_a_url_out_loud(self, scenes):
        """A synthetic voice reading `slash M S Kazemi slash` is the sound of a machine."""
        close = next(s for s in scenes.SCENES if s["id"] == "16-close")
        assert "slash" not in close["narration"]
        assert "github.com/MSKazemi/kubeintellect" in close["links"], "still on screen, though"


class TestTheClaimsItDoesMakeAreTrue:
    def test_the_gate_really_is_at_the_tool_boundary(self, spoken):
        """The strongest claim in the video: the gate cannot be talked around."""
        assert "tool boundary" in spoken
        tool = ROOT / "v4" / "packages" / "kubeintellect-server" / "app" / "tools" / "kubectl_tool.py"
        assert "from langgraph.types import interrupt" in tool.read_text(encoding="utf-8")

    def test_the_decision_log_is_on_by_default(self, spoken):
        assert "hash chained" in spoken or "hash-chained" in spoken
        assert "FLIGHT_RECORDER_ENABLED: bool = True" in CONFIG.read_text(encoding="utf-8")

    def test_the_licence_claim_matches_the_licence(self, spoken):
        assert "a g p l three" in spoken
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (ROOT / "LICENSE").read_text(
            encoding="utf-8")[:200]

    def test_the_paper_claim_is_about_an_earlier_version(self, spoken):
        """The published paper describes an earlier system; the v4 paper is not accepted."""
        assert "an earlier version is described in a peer reviewed paper" in spoken
        assert "doi.org/10.1007/s10723-026-09837-6" in (
            ROOT / "README.md").read_text(encoding="utf-8")

    def test_every_source_a_card_cites_exists(self, scenes):
        missing = []
        for sc in scenes.SCENES:
            for src in sc.get("sources") or []:
                path = src.split(" ")[0].split("§")[0].strip()
                if not path or not path.startswith(".."):
                    continue
                if not (VIDEO / path).exists():
                    missing.append((sc["id"], path))
        assert missing == [], missing


@pytest.fixture(scope="module")
def render():
    return _load("render")


class TestTheBrandIsTheProjectsOwn:
    def test_the_retired_accent_is_gone(self, render, scenes):
        for value in (render.ACCENT, render.GREEN, render.AMBER, render.TEAL):
            assert value != (124, 140, 248), "the upstream indigo is back"
        assert not re.search(r'=\s*"#7c8cf8"', (VIDEO / "scenes.py").read_text(encoding="utf-8"))

    def test_the_mark_colours_come_from_the_shipped_brand_asset(self, render):
        stops = re.findall(r'stop-color="(#[0-9a-fA-F]{6})"', BRAND.read_text(encoding="utf-8"))
        assert len(stops) == 2, stops
        assert render.MARK_A == render._rgb(stops[0])
        assert render.MARK_B == render._rgb(stops[1])

    def test_the_mark_is_drawn_from_the_svgs_geometry(self, render):
        """A hexagon with a K in it is not this project's logo, and used to be."""
        src = (VIDEO / "render.py").read_text(encoding="utf-8")
        body = src[src.index("def logo("):src.index("def wordmark(")]
        assert "polygon" not in body, "the hexagon is back"
        assert "53, 96" in body.replace("*", "").replace(" s", "") or "53 * s, 96 * s" in body

    def test_the_tile_is_the_marks_own_background(self, render):
        svg = BRAND.read_text(encoding="utf-8")
        tile = re.search(r'<rect width="256" height="256" rx="58" fill="(#[0-9a-fA-F]{6})"', svg)
        assert tile, "the mark's tile fill moved"
        assert render.BG_SUNKEN == render._rgb(tile.group(1))

    def test_the_gate_colour_is_the_one_the_site_calls_the_gate(self, render, scenes):
        assert scenes.AMBER == "#f59e0b"
        assert render.AMBER == render._rgb(scenes.AMBER)

    def test_the_renderer_and_the_scene_spec_share_one_palette(self, render, scenes):
        """Two palettes is how a `single source of truth` file ends up read by nobody."""
        assert render.ACCENT == render._rgb(scenes.TEAL)
        assert render.GREEN == render._rgb(scenes.ACCENT)
        assert render.CORAL == render._rgb(scenes.CORAL)

    def test_every_palette_entry_says_where_it_came_from(self):
        """A colour with no source is how the last one survived three years upstream."""
        src = (VIDEO / "render.py").read_text(encoding="utf-8")
        block = src[src.index("BG          = "):src.index("JB = ")]
        for line in block.splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            assert "#" in line.split("=", 1)[1], f"no source comment: {line.strip()}"


class TestTheVideoAndThePapersDoNotContradictEachOther:
    """The half of the 2026-08-28 review that pass 286 left open: check the claims against the
    papers, not only against the code.

    There are two papers and they are not the same artifact. **The published one** (Journal of
    Grid Computing 24(3), DOI 10.1007/s10723-026-09837-6) describes the *earlier* system. **The
    V4 paper** (`papers/v4-operator/`) is built and verified but **has not been submitted** to
    anything, so nothing may present it as reviewed.

    The audit found one genuine tension, and it is worth pinning rather than editing away. The
    published paper says write confirmation is *"a conversational confirmation step embedded in
    the agent's reasoning prompt"*, with no workflow interrupt for the Deletion agent. The video
    says the gate is *"at the tool boundary — not in the prompt, and not advisory"*. Both are
    true of their own version; the video is describing V4. What makes that safe is the closing
    card's hedge — *an earlier* version is the published one — and the code fact below, which is
    stronger than either paper's design: the gate is driven by a read-only **allowlist**, so a
    verb nobody has classified is gated rather than let through.
    """

    def test_the_paper_claim_is_hedged_to_an_earlier_version(self, scenes):
        close = next(s for s in scenes.SCENES if s["id"] == "16-close")
        published = next(b for b in close["bullets"] if b[0] == "Published")
        assert "an earlier version" in published[1]
        assert "peer reviewed" in published[1]

    def test_the_video_never_claims_the_v4_paper_is_published(self, scenes):
        """`papers/v4-operator/` is not submitted. Presenting it as reviewed would be false."""
        text = " ".join(str(s) for s in scenes.SCENES)
        for phrase in ("two peer reviewed", "our papers", "published in the Journal"):
            assert phrase not in text

    def test_the_doi_the_video_points_at_is_the_repositorys_own(self):
        cff = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        doi = "10.1007/s10723-026-09837-6"
        assert doi in cff and doi in readme

    def test_the_gate_claim_is_true_by_allowlist_not_by_enumeration(self):
        """Why the video may say "every mutating command" while the paper describes less.

        `_is_write_verb` returns True for anything **not** on the read-only list, so a verb that
        nobody classified is gated. If this ever inverts to a deny-list, the word "every" in the
        video stops being true and this test says so.
        """
        src = (ROOT / "v4" / "packages" / "kubeintellect-server" / "app" / "tools"
               / "kubectl_tool.py").read_text(encoding="utf-8")
        assert "return verb not in _READ_ONLY_VERBS" in src
        assert "from langgraph.types import interrupt" in src

    def test_the_published_paper_describes_the_prompt_side_design_it_still_describes(self):
        """A guard on the discrepancy itself, so nobody 'corrects' the video toward the paper.

        If this text ever leaves the published paper's source, the tension recorded in this
        class's docstring is stale and the note entry should be re-read.
        """
        arch = ROOT / "papers" / "old-paper" / "paper" / "my_paper" / "text" / "relatedwork.tex"
        if not arch.exists():
            pytest.skip("the published paper's source is not in this checkout")
        assert "conversational confirmation step" in arch.read_text(encoding="utf-8")
