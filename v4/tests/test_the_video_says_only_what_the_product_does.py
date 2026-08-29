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
SHOTS = VIDEO / "shots-dark"

# The shot sources are build *inputs*, not source: `.gitignore` excludes
# `scripts/demo/video/shots-*/`, so a fresh checkout has no footage at all. Three checks
# added on 2026-08-29 measured decoded pixels and so passed on the machine that rendered
# the video and failed on every runner — `FileNotFoundError: .../shots-dark/chatui`, and a
# `TypeError` from `shot_frames()` returning `None` three frames from the cause. They are
# real where the footage exists and cannot run where it does not, so they skip loudly
# rather than redden `main` for everyone. `test_the_footage_guard_is_conditional` below
# keeps this from quietly becoming a permanent skip.
needs_footage = pytest.mark.skipif(
    not SHOTS.is_dir(),
    reason=f"no rendered footage at {SHOTS} — shots-*/ is gitignored, so CI never has it",
)


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
        if not sc.get("enabled", True):
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


class TestThePayoffIsNotCut:
    """Scene `11-approve` narrates: *"That is the most valuable thirty seconds in this video,
    and it is the one a demo normally cuts."* Its transcript window was `(108, 140)`, which
    ended on `with the same error:` — a colon. The evidence for the claim the narration
    makes (a restart cannot supply a missing environment variable) is on :143-:145 and never
    reached the screen. The video asserted the conclusion and cut the proof, in the one scene
    that boasts about not doing that. Found 2026-08-29 by dumping each window's last line."""

    @pytest.fixture(scope="class")
    def video(self):
        return _load("render")

    def _tail(self, render, sc):
        lines = render.load_transcript(sc["source"], sc.get("lines"))
        return [ln for ln in lines if ln.strip()][-1]

    def test_no_terminal_scene_ends_mid_thought(self, scenes, video):
        """A window that stops on a colon or a comma has cut a sentence in half."""
        bad = []
        for sc in scenes.SCENES:
            if not sc.get("enabled", True) or sc.get("kind") != "terminal":
                continue
            tail = self._tail(video, sc)
            if tail.rstrip().endswith((":", ",")):
                bad.append((sc["id"], tail))
        assert bad == [], bad

    def test_the_approved_fix_scene_shows_why_the_fix_failed(self, scenes, video):
        """Not just that it failed — the root cause it names, on screen."""
        sc = next(s for s in scenes.SCENES if s["id"] == "11-approve")
        lines = video.load_transcript(sc["source"], sc.get("lines"))
        assert any("DATABASE_URL" in ln for ln in lines), \
            "the evidence for the narration's claim is outside the window"
        assert any("exit with code 1" in ln for ln in lines)

    def test_the_narration_walks_the_viewer_to_that_evidence(self, scenes):
        said = next(s for s in scenes.SCENES if s["id"] == "11-approve")["narration"]
        assert "DATABASE underscore U R L" in said, "spoken phonetically for Piper"
        assert "rather than taking my word for it" in said

    def test_the_reveal_stays_readable(self, scenes, video):
        """A longer window under the same narration just scrolls faster. Each terminal
        scene reveals its lines over 82% of its duration; past ~2 lines/s it is unreadable,
        which would trade one defect for another."""
        import json
        durs = VIDEO / "durations.json"
        if not durs.exists():
            pytest.skip("durations.json is a build artifact")
        d = json.loads(durs.read_text(encoding="utf-8"))
        too_fast = []
        for sc in scenes.SCENES:
            if not sc.get("enabled", True) or sc.get("kind") != "terminal":
                continue
            if sc["id"] not in d:
                continue
            n = len(video.load_transcript(sc["source"], sc.get("lines")))
            rate = n / ((d[sc["id"]] + 1.3) * 0.82)
            if rate > 2.0:
                too_fast.append((sc["id"], round(rate, 2)))
        assert too_fast == [], too_fast


SERVER = ROOT / "v4" / "packages" / "kubeintellect-server"
TRANSCRIPTS = ROOT / "scripts" / "demo" / "transcripts-kq"
CHATUI = ROOT / "scripts" / "demo" / "chat-ui"


class TestTheNewScenesClaimOnlyWhatWasCaptured:
    """Three scenes were added on 2026-08-28 — an architecture diagram, the chat UI, and a
    live capture from the production cluster in Azure. Two of them make claims that no
    transcript backs, because they are not terminal scenes: the diagram names modules, and
    the chat-UI scene quotes a latency measured somewhere else. These tie each one to the
    artifact it came from."""

    def test_every_stage_in_the_diagram_names_a_module_that_exists(self, render):
        missing = [n[3] for n in render.FLOW_NODES if n[3] and not (SERVER / n[3]).exists()]
        assert missing == [], missing

    def test_the_chokepoint_names_the_function_the_code_actually_calls_it(self, render):
        gate = [n for n in render.FLOW_NODES if n[0] == "gate"][0]
        src = (SERVER / "app" / "tools" / "aci" / "mutating.py").read_text(encoding="utf-8")
        assert "decide_write" in gate[2]
        assert "def decide_write(" in src

    def test_the_three_outcomes_are_the_three_the_gate_returns(self, render):
        """`decide_write` returns exactly `auto`, `approve` or `deny`. The diagram draws three
        outcomes; if the code ever grows a fourth, the diagram is a lie by omission."""
        src = (SERVER / "app" / "tools" / "aci" / "mutating.py").read_text(encoding="utf-8")
        verdicts = set(re.findall(r'MutationProposal\(command, rc, "(\w+)"', src))
        drawn = {label.split()[0] for label, _ in render.FLOW_OUTS}
        assert drawn == verdicts, (drawn, verdicts)

    def test_the_zero_token_claim_holds_for_the_path_that_evaluates_detectors(self):
        """The diagram says detectors are "compiled predicates · no model". That is a claim
        about *evaluation*, and `engine.py` is where evaluation happens. `authoring.py` in the
        same package does call a model — it is the natural-language detector *authoring*
        feature — so the claim is scoped to the engine rather than to the package."""
        engine = (SERVER / "app" / "detectors" / "engine.py").read_text(encoding="utf-8")
        assert not re.search(r"\bopenai\b|langchain|ChatOpenAI", engine)

    def test_the_azure_scene_replays_a_capture_that_is_in_the_repo(self, scenes):
        sc = self._scene(scenes, "13b-azure")
        assert (TRANSCRIPTS / sc["source"]).exists()

    def test_the_uptime_it_states_is_the_uptime_in_the_capture(self, scenes):
        sc = self._scene(scenes, "13b-azure")
        cap = (TRANSCRIPTS / sc["source"]).read_text(encoding="utf-8")
        assert "one hundred and twenty five days" in sc["narration"]
        assert "125d" in cap

    def test_it_admits_the_version_the_endpoint_actually_returned(self, scenes):
        """The deployed server answers 2.0.0 while the code in this video is newer. The scene
        says so out loud; this fails if someone quietly drops the admission."""
        sc = self._scene(scenes, "13b-azure")
        cap = (TRANSCRIPTS / sc["source"]).read_text(encoding="utf-8")
        assert '"version":"2.0.0"' in cap.replace(" ", "")
        assert "version two point zero" in sc["narration"]
        assert "newer than the box" in sc["narration"]

    def test_the_azure_window_covers_the_whole_capture(self, scenes):
        sc = self._scene(scenes, "13b-azure")
        n = len((TRANSCRIPTS / sc["source"]).read_text(encoding="utf-8").splitlines())
        assert sc["lines"] == (1, n), (sc["lines"], n)

    @needs_footage
    def test_the_chat_ui_clip_was_decoded_into_frames(self, scenes, render):
        sc = self._scene(scenes, "13-chat-ui")
        assert len(render.shot_frames(sc["source"])) > 0

    def test_the_replay_speed_it_declares_matches_the_footage(self, scenes, render):
        """The clip is retimed onto the narration, so it runs faster than it happened. The
        scene states the factor; the caption says so on screen. Both have to be true."""
        import json
        durs = VIDEO / "durations.json"
        if not durs.exists():
            pytest.skip("durations.json is a build artifact")
        sc = self._scene(scenes, "13-chat-ui")
        dur = json.loads(durs.read_text(encoding="utf-8"))[sc["id"]] + 1.3
        actual = len(render.shot_frames(sc["source"])) / 15 / dur   # decoded at fps=15
        assert abs(sc["speed"] - actual) < 0.1, (sc["speed"], actual)
        assert "faster than" in sc["caption"]

    def test_the_settle_time_it_quotes_comes_from_the_recording(self, scenes):
        sc = self._scene(scenes, "13-chat-ui")
        meta = (CHATUI / "chat-ui-crashloop.json").read_text(encoding="utf-8")
        assert "fifteen point three seconds" in sc["narration"]
        assert "15.3" in meta

    def test_the_chat_ui_scene_does_not_call_the_rbac_refusal_an_approval_gate(self, scenes):
        """That session holds a read-only key, so the write is refused at the role boundary —
        a different mechanism from the approval gate scenes 09 to 11 demonstrate."""
        sc = self._scene(scenes, "13-chat-ui")
        assert "read only key" in sc["narration"]
        assert "approval gate" not in sc["narration"].lower()

    @staticmethod
    def _scene(scenes, sid):
        return [sc for sc in scenes.SCENES if sc["id"] == sid][0]


class TestTheTerminalChromeDoesNotMislabelFootage:
    """The terminal title bar is itself a claim about where the footage came from. It was
    hard-coded to `kq · shop @ ki-demo · AUTONOMY_LEVEL=A2`, which put the local demo
    cluster's prompt above a capture taken against the production cluster in Azure."""

    def test_footage_from_another_host_carries_its_own_title(self, scenes):
        for sc in scenes.SCENES:
            if not sc.get("enabled", True) or sc.get("kind") != "terminal":
                continue
            body = (TRANSCRIPTS / sc["source"]).read_text(encoding="utf-8")
            if "20.119.62.10" in body or "api.kubeintellect.com" in body:
                assert sc.get("term_title"), f"{sc['id']} uses the default local-cluster title"
                assert "ki-demo" not in sc["term_title"], sc["term_title"]

    def test_the_title_is_a_scene_property_rather_than_a_constant(self, render):
        src = (VIDEO / "render.py").read_text(encoding="utf-8")
        assert 'sc.get("term_title"' in src


class TestTheShotFitsTheFrame:
    """`render_shot` scaled every source to a fixed 1400 px width and derived the height,
    which is only correct for 16:9. The chat-UI capture is 1280x800: it came out 921 px tall
    with its title bar and landed at y=80..1001, over the act label and through the caption
    bar. Neither the build nor any test noticed — the frames simply looked wrong."""

    @needs_footage
    def test_the_shot_stays_between_the_act_label_and_the_caption_bar(self, scenes, render):
        from PIL import Image
        top, bottom, bar = 124, render.H - 132, 46
        for sc in scenes.SCENES:
            if not sc.get("enabled", True) or sc.get("kind") != "shot":
                continue
            seq = render.shot_frames(sc["source"])
            src = Image.open(seq[0] if seq else render.SHOTS / sc["source"])
            vw = int(min(1400, (bottom - top - bar) * src.width / src.height))
            fh = int(round(vw * src.height / src.width)) + bar
            y0 = top + max(0, ((bottom - top) - fh) // 2)
            assert y0 >= top and y0 + fh <= bottom, (sc["id"], y0, y0 + fh)

    @needs_footage
    def test_a_directory_source_plays_the_whole_recording(self, scenes, render):
        """A clip is retimed, never truncated: the last scene-frame must land on the last
        source frame, or the video quietly stops the recording early."""
        sc = [s for s in scenes.SCENES if s["id"] == "13-chat-ui"][0]
        n = len(render.shot_frames(sc["source"]))
        assert render.shot_frame_index(sc, 0.0, 30.0, n) == 0
        assert render.shot_frame_index(sc, 30.0, 30.0, n) == n - 1


class TestTheFootageGuardStaysHonest:
    """A skip that can never turn back on is a deleted test wearing a disguise.

    `needs_footage` exists so three pixel checks stop reddening `main` on a checkout that
    cannot hold the footage. That is only acceptable while the skip is genuinely
    conditional — it must key off the footage directory and nothing else, and it must let
    the checks run on the machine that has rendered the video.
    """

    def test_the_guard_keys_off_the_footage_directory_and_nothing_else(self):
        source = Path(__file__).read_text(encoding="utf-8")
        assert "not SHOTS.is_dir()" in source, "the guard no longer reads the footage directory"
        assert "@pytest.mark.skip\n" not in source, "an unconditional skip crept in"
        assert SHOTS == VIDEO / "shots-dark", SHOTS

    def test_the_guarded_checks_run_wherever_the_footage_exists(self):
        """The condition is the directory, so the skip lifts the moment a render happens."""
        marked = [m for m in (needs_footage,) if m.args or m.kwargs]
        assert marked, "needs_footage carries no condition"
        assert needs_footage.kwargs["reason"].startswith("no rendered footage at")
        assert needs_footage.args[0] is (not SHOTS.is_dir())


class TestNothingIsSpelledPhoneticallyOnScreen:
    """The narration is spelled for Piper: it has to say "A G P L three" to be pronounced
    correctly. `SUBS` existed to undo that for the subtitle track, and nothing undid it for
    the cards — so the closing card shipped reading "A G P L three, self hosted"."""

    def test_no_card_is_authored_in_the_voices_spelling(self, scenes):
        leaks = []
        for sc in scenes.SCENES:
            if not sc.get("enabled", True):
                continue
            shown = [sc.get("title", ""), sc.get("subtitle", ""), sc.get("caption", ""),
                     sc.get("term_title", "")]
            for head, sub in sc.get("bullets") or []:
                shown += [head, sub]
            shown += list(sc.get("links") or [])
            leaks += [(sc["id"], k) for txt in shown for k in scenes.SUBS if k in txt]
        assert leaks == [], leaks

    def test_the_normaliser_rewrites_the_form_that_shipped(self, render):
        assert render.written("A G P L three") == "AGPL-3.0"
        assert render.written("kube control get pods") == "kubectl get pods"

    def test_every_kind_of_on_screen_text_runs_through_the_normaliser(self):
        """Belt and braces: the source strings are written form now, and the renderer
        normalises anyway, so a card authored phonetically tomorrow still renders correctly."""
        src = (VIDEO / "render.py").read_text(encoding="utf-8")
        assert 'written(sc["title"])' in src
        assert 'written(sc["subtitle"])' in src
        assert "written(h), written(b)" in src
        assert "caption = written(caption)" in src

    def test_the_narration_keeps_the_phonetic_spelling(self, scenes):
        """The fix must not reach the narration — Piper reads that, and `AGPL-3.0` is
        pronounced as a mess. The two forms are supposed to differ."""
        narration = " ".join(sc.get("narration", "") for sc in scenes.SCENES
                             if sc.get("enabled", True))
        assert "A G P L three" in narration
        assert "kube control" in narration
