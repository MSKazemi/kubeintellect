"""The namespace filter handled `-o jsonpath` by splitting on spaces. That is one jsonpath.

`_filter_namespace_output` promises every output format is filtered — a bare
`kubectl get namespaces` is deliberately allowed so the agent can see the shape of the cluster,
and the protected ones are removed from the answer instead. For `-o jsonpath` it did this::

    tokens = output.split()
    kept   = [t for t in tokens if t.lower() not in blocked]

which works for exactly one expression: the one whose output is bare names separated by spaces.
jsonpath renders whatever the caller asked for. Measured 2026-08-20 with the default blocklist,
three ordinary expressions returned every protected namespace **and no withheld note**, so the
answer looked complete::

    {range .items[*]}{.metadata.name}{","}{end}                  -> default,kube-system,monitoring,
    {range .items[*]}{.metadata.name}{"="}{.status.phase}{"\\n"}{end} -> kube-system=Active
    {range .items[*]}{.metadata.name}{":"}{.status.phase}{"\\n"}{end} -> kube-system:Active

The name was still there; it was just no longer a whole token. A separator is not a filter's
business, and there is no separator jsonpath cannot produce.

`custom-columns` and `go-template` were refused for this exact reason — the caller chose the
shape — and the `--all-namespaces` sibling `_filter_all_namespaces_output` already refuses
jsonpath too. Two functions doing one job had two different answers for one format. The branch
is gone rather than patched: `jsonpath` now falls to the same allowlist check as every other
caller-shaped format.

Pass 99's lens, one step on: when one parser is fixed, the bug is that a second parser was
reading the same thing with a different idea of what it said.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools.kubectl_tool import run_kubectl

_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}

# What kubectl really prints for each expression, for namespaces default + kube-system + monitoring.
_JSONPATHS = [
    ("{.items[*].metadata.name}", "default kube-system monitoring"),
    ('{range .items[*]}{.metadata.name}{","}{end}', "default,kube-system,monitoring,"),
    ('{range .items[*]}{.metadata.name}{"="}{.status.phase}{"\\n"}{end}',
     "default=Active\nkube-system=Active\nmonitoring=Active\n"),
    ('{range .items[*]}{.metadata.name}{":"}{.status.phase}{"\\n"}{end}',
     "default:Active\nkube-system:Active\nmonitoring:Active\n"),
    ('{.items[*].metadata["name"]}', "default kube-system monitoring"),
    ("{.items[0].metadata.name}", "kube-system"),
]


def _run(command: str, stdout: str, role: str = "admin") -> str:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = stdout, "", 0
        run.return_value = proc
        cfg = _ADMIN if role == "admin" else {"configurable": {"user_role": role, "thread_id": "t"}}
        return str(run_kubectl.invoke({"command": command, "stdin": None}, config=cfg))


class TestNoJsonpathReturnsAProtectedNamespace:

    @pytest.mark.parametrize(("path", "output"), _JSONPATHS)
    def test_a_namespace_listing_is_refused_not_half_filtered(self, path, output):
        out = _run(f"kubectl get ns -o jsonpath={path}", output)
        assert "kube-system" not in out and "monitoring" not in out, f"leaked: {out[:200]!r}"
        assert "[Protected]" in out

    @pytest.mark.parametrize(("path", "output"), _JSONPATHS)
    def test_the_same_holds_for_the_long_flag(self, path, output):
        out = _run(f"kubectl get namespaces --output=jsonpath={path}", output)
        assert "kube-system" not in out and "[Protected]" in out

    def test_the_refusal_says_what_to_use_instead(self):
        out = _run("kubectl get ns -o jsonpath={.items[*].metadata.name}",
                   "default kube-system monitoring")
        assert "-o json" in out and "-n <namespace>" in out

    def test_it_does_not_silently_return_a_short_answer(self):
        """The old branch dropped tokens and appended a note. Refusing is not the same shape."""
        out = _run("kubectl get ns -o jsonpath={.items[*].metadata.name}",
                   "default kube-system monitoring")
        assert "withheld" not in out
        assert not out.startswith("default")


class TestTheTwoFiltersNowAgree:
    """`-A` and the namespace listing are two functions doing one job."""

    @pytest.mark.parametrize("command", [
        "kubectl get ns -o jsonpath={.items[*].metadata.name}",
        "kubectl get pods -A -o jsonpath={.items[*].metadata.name}",
    ])
    def test_neither_path_filters_a_caller_shaped_format(self, command):
        out = _run(command, "default kube-system monitoring")
        assert "[Protected]" in out and "kube-system" not in out

    @pytest.mark.parametrize("fmt", ["jsonpath={.x}", "jsonpath-file=/f"])
    def test_jsonpath_is_outside_the_allowlist_like_its_siblings(self, fmt):
        assert fmt not in kt._FIXED_SHAPE_FORMATS

    def test_the_allowlist_itself_did_not_move(self):
        assert kt._FIXED_SHAPE_FORMATS == frozenset({"", "wide", "json", "yaml"})


class TestTheFormatsKubectlShapesStillFilter:
    """Refusing jsonpath must not have turned the filter into a blanket refusal."""

    _TABLE = "NAME          STATUS   AGE\ndefault       Active   1d\nkube-system   Active   1d\n"
    _NAMES = "namespace/default\nnamespace/kube-system\n"

    def test_the_default_table_is_still_filtered(self):
        out = _run("kubectl get ns", self._TABLE)
        assert "default" in out and "kube-system" not in out and "withheld" in out

    def test_wide_is_still_filtered(self):
        out = _run("kubectl get ns -o wide", self._TABLE)
        assert "default" in out and "kube-system" not in out

    def test_o_name_is_still_filtered_not_refused(self):
        out = _run("kubectl get ns -o name", self._NAMES)
        assert "namespace/default" in out and "kube-system" not in out
        assert "[Protected]" not in out.splitlines()[0]

    def test_json_is_still_filtered(self):
        import json
        payload = json.dumps({"items": [{"metadata": {"name": "default"}},
                                        {"metadata": {"name": "kube-system"}}]})
        out = _run("kubectl get ns -o json", payload)
        doc = json.loads(out)
        assert [i["metadata"]["name"] for i in doc["items"]] == ["default"]

    def test_a_non_namespace_listing_is_untouched_by_this_path(self):
        """`kubectl get pods -o jsonpath=…` in one namespace has nothing to filter here."""
        out = _run("kubectl get pods -n shop -o jsonpath={.items[*].metadata.name}", "api-0 api-1")
        assert out.strip() == "api-0 api-1"


class TestTheBlocklistStillDrivesIt:

    def test_every_configured_namespace_would_have_leaked(self):
        """The comma form leaked whatever was configured, not just the defaults."""
        joined = ",".join(sorted(settings.kubectl_blocked_namespaces))
        out = _run("kubectl get ns -o jsonpath={range .items[*]}{.metadata.name}{,}{end}", joined)
        for ns in settings.kubectl_blocked_namespaces:
            assert ns not in out, ns
