"""Unit tests for K8s-ACI v0 (v5 specs/01). The cluster boundary (_exec / run_kubectl)
is patched, so these run without a live cluster."""

from __future__ import annotations

import pytest

from app.tools.aci import (
    ACI_READ_VERB_ALLOWLIST,
    diff_change,
    inspect,
    logs,
    search,
)
from app.tools.aci import read_verbs
from app.tools.aci.bounds import empty_message, is_read_only, normalize_krm, window
from app.tools.aci.models import AciContractError, AciResult, Health


# ── Allowlist export (R-aci-reg-02: available regardless of the enable flag) ──
def test_allowlist_is_exactly_the_four_read_verbs():
    assert ACI_READ_VERB_ALLOWLIST == frozenset({"inspect", "search", "logs", "diff_change"})


def test_verbs_are_langchain_tools_named_correctly():
    assert {inspect.name, search.name, logs.name, diff_change.name} == ACI_READ_VERB_ALLOWLIST


# ── is_read_only (R-aci-wrap-02) ──────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "kubectl get pods -n default",
    "kubectl describe deploy web -n prod",
    "kubectl logs mypod -n default --tail=100",
    "kubectl diff -f -",
    "kubectl rollout history deploy/web",
])
def test_is_read_only_accepts_read_commands(cmd):
    assert is_read_only(cmd) is True


@pytest.mark.parametrize("cmd", [
    "kubectl delete pod mypod -n default",
    "kubectl apply -f -",
    "kubectl scale deploy/web --replicas=3",
    "kubectl patch deploy web -p '{}'",
])
def test_is_read_only_rejects_mutating_commands(cmd):
    assert is_read_only(cmd) is False


# ── window() bounds (R-aci-bound-01..03) ──────────────────────────────────────
def test_window_enforces_line_cap_and_reports_cursor():
    body = "\n".join(f"line{i}" for i in range(250))
    trimmed, total, shown, cursor = window(body, max_lines=100, max_chars=100_000)
    assert total == 250
    assert shown == 100
    assert cursor == 100
    assert trimmed.count("\n") == 99  # exactly 100 lines, never a partial line


def test_window_no_truncation_returns_none_cursor():
    body = "a\nb\nc"
    trimmed, total, shown, cursor = window(body, max_lines=100, max_chars=100_000)
    assert (total, shown, cursor) == (3, 3, None)
    assert trimmed == body


def test_window_char_cap_drops_whole_lines():
    body = "\n".join("x" * 50 for _ in range(10))  # 10 lines, ~509 chars
    trimmed, total, shown, cursor = window(body, max_lines=100, max_chars=120)
    assert len(trimmed) <= 120
    assert shown < 10
    assert cursor == shown  # more remains


def test_window_single_overlong_line_hardcut_at_offset_zero():
    body = "A" * 500  # one line longer than the cap
    trimmed, total, shown, cursor = window(body, max_lines=100, max_chars=100)
    assert trimmed == "A" * 100
    assert shown == 1 and cursor is None


def test_window_single_overlong_line_hardcuts_the_offset_line_not_the_head():
    # Regression: paginating into an over-long line must return THAT line's head,
    # not the document head. Line 0 is short; line 1 exceeds the cap.
    body = "short\n" + "B" * 500
    trimmed, total, shown, cursor = window(body, max_lines=1, max_chars=100, offset=1)
    assert trimmed == "B" * 100          # the offset line, not "short…"
    assert "short" not in trimmed
    assert shown == 1 and cursor is None


# ── normalize_krm (R-aci-bound-04) ────────────────────────────────────────────
def test_normalize_strips_server_noise():
    raw = (
        "metadata:\n"
        "  name: web\n"
        "  uid: abc-123\n"
        "  resourceVersion: '9988'\n"
        "  generation: 4\n"
        "  managedFields:\n"
        "  - manager: kubectl\n"
        "    operation: Update\n"
        "spec:\n"
        "  replicas: 3\n"
    )
    out = normalize_krm(raw, view="summary")
    assert "name: web" in out
    assert "replicas: 3" in out
    for noise in ("uid:", "resourceVersion:", "generation:", "managedFields:", "manager: kubectl"):
        assert noise not in out


# ── never-silent empties (R-aci-empty-01) ─────────────────────────────────────
def test_empty_message_is_never_blank():
    msg = empty_message("search", "pods in default")
    assert msg.strip() != ""
    assert "no matching results" in msg


def test_aci_result_render_never_empty_on_empty_flag():
    r = AciResult(verb="search", ok=True, target="pods in x", kubectl_command="kubectl get pods -n x", empty=True)
    assert r.render().strip() != ""


# ── verb behavior with patched cluster seam ───────────────────────────────────
@pytest.mark.asyncio
async def test_search_names_and_bounds(monkeypatch):
    big = "NAME READY STATUS\n" + "\n".join(f"pod-{i} 1/1 Running" for i in range(300))
    monkeypatch.setattr(read_verbs, "_exec", lambda cmd: big)
    out = await search.ainvoke({"kinds": ["pods"], "namespace": "default"})
    assert "[aci:search]" in out
    assert "next offset" in out  # truncation surfaced, never silent


@pytest.mark.asyncio
async def test_search_empty_is_explicit(monkeypatch):
    monkeypatch.setattr(read_verbs, "_exec", lambda cmd: "No resources found in default namespace.")
    out = await search.ainvoke({"kinds": ["pods"], "namespace": "default"})
    assert "ran successfully, no matching results" in out


@pytest.mark.asyncio
async def test_inspect_reports_health(monkeypatch):
    monkeypatch.setattr(read_verbs, "_exec", lambda cmd: "Status: Running\nReady: True")
    out = await inspect.ainvoke({"kind": "pod", "name": "web", "namespace": "default"})
    assert f"health={Health.CURRENT.value}" in out


@pytest.mark.asyncio
async def test_logs_requires_pod_or_selector():
    out = await logs.ainvoke({"namespace": "default"})
    assert "requires either 'pod' or 'selector'" in out


@pytest.mark.asyncio
async def test_diff_change_git_is_declined_not_silent():
    out = await diff_change.ainvoke({"against": "git", "kind": "deploy", "name": "web"})
    assert "unsupported in K8s-ACI v0" in out


@pytest.mark.asyncio
async def test_verb_builds_only_read_only_commands(monkeypatch):
    """The read-only assertion is structural: capture the command the verb runs."""
    seen = {}
    monkeypatch.setattr(read_verbs, "_exec", lambda cmd: seen.setdefault("cmd", cmd) or "ok")
    await inspect.ainvoke({"kind": "pod", "name": "web", "namespace": "default"})
    assert is_read_only(seen["cmd"]) is True


def test_contract_error_type_available():
    assert issubclass(AciContractError, RuntimeError)
