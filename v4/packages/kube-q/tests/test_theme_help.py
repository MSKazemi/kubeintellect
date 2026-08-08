"""
Tests for the interface-polish surfaces added alongside the repl split:
theme (NO_COLOR handling), sectioned help, and the new renderer helpers
(status footer, HITL panel, live plan panel).
"""

import pytest

from kube_q.cli import help_text, theme
from kube_q.cli.renderer import (
    plan_panel,
    render_hitl_panel,
    render_status_footer,
)

# ── theme ─────────────────────────────────────────────────────────────────────


def test_color_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert theme.color_enabled() is True


def test_no_color_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert theme.color_enabled() is False
    # Neutral theme still defines every role (so markup is stripped, not printed).
    neutral = theme.get_theme()
    assert "accent" in neutral.styles


def test_plan_icons_cover_all_statuses() -> None:
    for status in ("done", "skipped", "in_progress", "pending"):
        assert status in theme.PLAN_ICONS


# ── sectioned help ──────────────────────────────────────────────────────────


def test_help_overview_lists_topics(capsys: pytest.CaptureFixture) -> None:
    help_text.render_help("")
    out = capsys.readouterr().out
    assert "kube-q" in out
    assert "/help sessions" in out


def test_help_topic_renders(capsys: pytest.CaptureFixture) -> None:
    help_text.render_help("sessions")
    out = capsys.readouterr().out
    assert "Session history" in out
    assert "/sessions" in out


def test_help_alias_resolves(capsys: pytest.CaptureFixture) -> None:
    help_text.render_help("ns")  # alias → namespace
    out = capsys.readouterr().out
    assert "Namespace" in out


def test_help_unknown_topic_hints(capsys: pytest.CaptureFixture) -> None:
    help_text.render_help("definitely-not-a-topic")
    out = capsys.readouterr().out
    assert "Unknown help topic" in out


# ── renderer helpers ──────────────────────────────────────────────────────────


def test_status_footer_omits_when_empty(capsys: pytest.CaptureFixture) -> None:
    render_status_footer(kube_context=None, namespace=None, total_tokens=0, cost=None)
    assert capsys.readouterr().out.strip() == ""


def test_status_footer_renders_parts(capsys: pytest.CaptureFixture) -> None:
    render_status_footer(
        kube_context="prod", namespace="payments", total_tokens=12400, cost="$0.03"
    )
    out = capsys.readouterr().out
    assert "prod" in out and "payments" in out and "12,400" in out and "$0.03" in out


def test_hitl_panel_shows_command(capsys: pytest.CaptureFixture) -> None:
    render_hitl_panel("kubectl delete pod x")
    out = capsys.readouterr().out
    assert "Approval required" in out
    assert "kubectl delete pod x" in out


def test_plan_panel_empty_is_none() -> None:
    assert plan_panel({"steps": []}) is None


def test_plan_panel_builds_for_steps() -> None:
    panel = plan_panel({"steps": [{"description": "check pods", "status": "done"}]})
    assert panel is not None
