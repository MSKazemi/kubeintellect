"""Autonomy level resolution (ADR-003).

Levels:
  A0 — observe: detectors fire, findings logged; no LLM investigation.
  A1 — investigate: a firing detector opens an autonomous investigation;
       the report is published (findings feed + episode); no mutations.
  A2 — propose: investigation may propose a fix; execution needs HITL.
  A3 — auto-fix: execution without approval — ONLY for (playbook, namespace)
       pairs on the explicit allowlist; always post-verified.

Config (env / values.yaml):
  AUTONOMY_LEVEL             default level, e.g. "A1"
  AUTONOMY_NAMESPACE_LEVELS  per-namespace overrides: "prod=A0,dev=A2"
  AUTONOMY_A3_ALLOWLIST      "CrashLoopBackOff/dev,ImagePullBackOff/staging"
                             (playbook/namespace; namespace supports a
                             trailing * glob)

Protected namespaces (KUBECTL_BLOCKED_NAMESPACES) are pinned to A0 for
autonomous action regardless of configuration — the watchtower never
investigates or mutates infra namespaces on its own.

A **cluster-scoped** object has no namespace at all, so this model cannot evaluate it: a
Warning event about a Node, PersistentVolume or ClusterRole arrives with `namespace=""`.
Until 2026-08-20 that fell through to the configured default, which meant an operator who
wrote `AUTONOMY_A3_ALLOWLIST="SomePlaybook/*"` — a glob this module documents and supports —
silently made **Nodes auto-fixable**, the object where an unattended remediation (cordon,
drain, delete) is least recoverable. An unattributable namespace is now capped at A1:
investigate and report, never mutate. Observation is unaffected; only autonomous action is.
"""
from __future__ import annotations

from fnmatch import fnmatch

from app.core.config import settings

_ORDER = ("A0", "A1", "A2", "A3")


def _parse_overrides(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            ns, level = part.split("=", 1)
            if level.strip() in _ORDER:
                out[_normalise(ns)] = level.strip()
    return out


def _normalise(namespace: str | None) -> str:
    """Match how the kubectl tool compares namespaces, so the two cannot disagree."""
    return (namespace or "").strip().lower()


def _cap(level: str, ceiling: str) -> str:
    """The more restrictive of two levels."""
    return level if _ORDER.index(level) <= _ORDER.index(ceiling) else ceiling


def level_for_namespace(namespace: str) -> str:
    """Effective autonomy level for a namespace."""
    ns = _normalise(namespace)
    if ns in settings.kubectl_blocked_namespaces:
        return "A0"
    overrides = _parse_overrides(settings.AUTONOMY_NAMESPACE_LEVELS)
    level = overrides.get(ns, settings.AUTONOMY_LEVEL)
    level = level if level in _ORDER else "A1"
    if not ns:
        # Cluster-scoped: the namespace model cannot judge it, so cap at investigate-only.
        # `_cap`, not a flat "A1", so a deployment that pinned everything to A0 stays at A0.
        return _cap(level, "A1")
    return level


def at_least(level: str, floor: str) -> bool:
    return _ORDER.index(level) >= _ORDER.index(floor)


def a3_allowed(playbook: str, namespace: str) -> bool:
    """True iff (playbook, namespace) is explicitly allowlisted for auto-fix."""
    if not _normalise(namespace):
        return False          # never auto-fix an object the namespace model cannot evaluate
    if level_for_namespace(namespace) != "A3":
        return False
    for entry in settings.AUTONOMY_A3_ALLOWLIST.split(","):
        entry = entry.strip()
        if not entry or "/" not in entry:
            continue
        pb, ns_pattern = entry.split("/", 1)
        if pb.strip() == playbook and fnmatch(_normalise(namespace), ns_pattern.strip()):
            return True
    return False
