"""The architecture animation may only draw what this repository actually ships.

A diagram is the single easiest artifact to lie with, because nothing executes it. The one on
the website (`website/public/images/architecture.svg`, exported 2026-03-29) is the worked
example: its nodes are `Task Router`, `Orchestrator`, `Final Aggregator` and `Code Generator` —
the V1 design, frozen under ADR-001 — and it has been shipping as "the architecture" ever since,
with no sensorium, no recorder, no memory, and no approval gate in it.

The failure this file exists to prevent is narrower and worse. **Most of V4 is flag-gated and a
large share of those flags are off by default.** Drawing them like everything else would claim a
system nobody runs, which is precisely the defect the video audit caught on 2026-08-28: the
narration said "read-only by default" about a server whose `REQUIRE_AUTH` is `False`.

So each claim in `spec.py` is checked against the thing it claims about:

* every ``module`` is a path that exists — the box is labelled with it, so it must be readable;
* every ``flag`` is a real ``Settings`` field;
* every ``on`` equals ``Settings.model_fields[flag].default`` — the **declared** default, not
  ``settings.FLAG``, which is whatever the developer's `.env` says and is how a drawing drifts
  without anyone editing it;
* a component with no flag claims to be always-on, and must actually claim that;
* the default-off set is non-empty and drawn, so the honest half cannot be quietly dropped.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ARCH = ROOT / "scripts" / "demo" / "architecture"


def _load(name: str):
    """Import a module out of `scripts/demo/architecture/`, which is not a package."""
    if not (ARCH / f"{name}.py").exists():
        pytest.skip(f"{name}.py is not present")
    sys.path.insert(0, str(ARCH))
    try:
        s = importlib.util.spec_from_file_location(f"_arch_{name}", ARCH / f"{name}.py")
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ARCH))


@pytest.fixture(scope="module")
def spec():
    return _load("spec")


class TestEveryBoxNamesSomethingReal:
    def test_every_module_path_exists(self, spec):
        missing = [(n.key, n.module) for n in spec.nodes().values()
                   if not (ROOT / n.module).exists()]
        assert missing == [], f"drawn, but not in the tree: {missing}"

    def test_the_module_is_specific_enough_to_read(self, spec):
        """`app/` would be true of everything and useful to nobody."""
        for n in spec.nodes().values():
            assert n.module.count("/") >= 2, f"{n.key}: {n.module} is not a real destination"

    def test_keys_are_unique(self, spec):
        keys = [n.key for lay in spec.LAYERS for n in lay.nodes]
        assert len(keys) == len(set(keys))

    def test_every_layer_has_nodes(self, spec):
        assert all(lay.nodes for lay in spec.LAYERS)


class TestTheFlagsAreTheCodesFlags:
    def test_every_flag_is_a_real_setting(self, spec):
        unknown = [n.flag for n in spec.nodes().values()
                   if n.flag and n.flag not in Settings.model_fields]
        assert unknown == [], f"no such Settings field: {unknown}"

    def test_the_claimed_default_is_the_declared_default(self, spec):
        """The whole honesty property. `model_fields[...].default` ignores any local .env."""
        wrong = []
        for n in spec.nodes().values():
            if not n.flag:
                continue
            actual = Settings.model_fields[n.flag].default
            if n.on != actual:
                wrong.append(f"{n.key}: spec says {n.on!r}, {n.flag} defaults to {actual!r}")
        assert wrong == [], wrong

    def test_an_unflagged_component_claims_to_be_always_on(self, spec):
        for n in spec.nodes().values():
            if n.flag is None:
                assert n.on is True, f"{n.key} has no flag but does not claim always-on"


class TestTheOffHalfIsDrawnAsOff:
    def test_there_is_an_off_half_at_all(self, spec):
        """If this ever empties, the drawing stopped telling the harder half of the truth."""
        assert len(spec.default_off()) >= 4

    def test_the_four_that_a_stock_install_does_not_run(self, spec):
        off = {n.key for n in spec.default_off()}
        assert {"predict", "cortex", "rbac", "v5"} <= off

    def test_cortex_is_off_because_it_has_not_reached_parity(self, spec):
        assert Settings.model_fields["CORTEX_V4_ENABLED"].default is False

    def test_the_auth_claim_that_the_video_got_wrong(self, spec):
        """The code fact that forced the wording, pinned in a second place.

        With no keys configured the server treats every unauthenticated caller as `admin`. If
        that default ever changes, this fails and says the drawing may now be understating.
        """
        assert Settings.model_fields["REQUIRE_AUTH"].default is False
        rbac = spec.nodes()["rbac"]
        assert rbac.on is False
        assert "ONCE KEYS ARE SET" in rbac.note

    def test_the_gate_itself_is_not_flag_gated(self, spec):
        """The approval gate is the product's central claim; a flag on it would be a footnote."""
        assert spec.nodes()["gate"].flag is None
        assert spec.nodes()["gate"].on is True


class TestTheDataFlowConnectsRealComponents:
    def test_no_dangling_endpoints(self, spec):
        keys = set(spec.nodes())
        bad = [(a, b) for a, b, _l, _p in spec.FLOWS if a not in keys or b not in keys]
        assert bad == [], f"flow endpoints that are not components: {bad}"

    def test_every_phase_has_flows(self, spec):
        for num, _title, _blurb in spec.PHASES:
            assert [f for f in spec.FLOWS if f[3] == num], f"phase {num} draws nothing"

    def test_every_flow_belongs_to_a_declared_phase(self, spec):
        declared = {p[0] for p in spec.PHASES}
        assert {f[3] for f in spec.FLOWS} <= declared

    def test_a_mutating_command_reaches_kubectl_only_through_the_gate(self, spec):
        """If the drawing ever shows a write path around the gate, it is drawing a lie."""
        to_kubectl = [(a, lab) for a, b, lab, _p in spec.FLOWS if b == "kubectl"]
        writers = [a for a, lab in to_kubectl if "approval" in lab]
        assert writers == ["gate"], to_kubectl

    def test_perception_does_not_start_at_a_human(self, spec):
        """Phase 3's whole point: it runs when nobody asked."""
        p3 = [f for f in spec.FLOWS if f[3] == 3]
        assert not any(a in {"kq", "api"} for a, _b, _l, _p in p3)


class TestItIsNotTheV1DiagramAgain:
    def test_the_v1_component_names_appear_nowhere(self, spec):
        """`architecture.svg` on the website is V1. This is a different system; keep it that way."""
        text = (ARCH / "spec.py").read_text(encoding="utf-8")
        drawn = " ".join(f"{n.label} {n.note}" for n in spec.nodes().values())
        for v1 in ("Task Router", "Final Aggregator", "Code Generator"):
            assert v1 not in drawn, f"{v1} is a V1 component"
        assert "V1" in text, "the reason the V1 diagram was not reconciled must stay written down"

    def test_the_v4_layers_that_v1_never_had_are_all_present(self, spec):
        keys = set(spec.nodes())
        assert {"watch", "detect", "fr", "l1", "l2", "gate", "ladder"} <= keys
