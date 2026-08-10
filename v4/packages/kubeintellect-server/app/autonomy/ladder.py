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
                out[ns.strip()] = level.strip()
    return out


def level_for_namespace(namespace: str) -> str:
    """Effective autonomy level for a namespace."""
    if namespace in settings.kubectl_blocked_namespaces:
        return "A0"
    overrides = _parse_overrides(settings.AUTONOMY_NAMESPACE_LEVELS)
    level = overrides.get(namespace, settings.AUTONOMY_LEVEL)
    return level if level in _ORDER else "A1"


def at_least(level: str, floor: str) -> bool:
    return _ORDER.index(level) >= _ORDER.index(floor)


def a3_allowed(playbook: str, namespace: str) -> bool:
    """True iff (playbook, namespace) is explicitly allowlisted for auto-fix."""
    if level_for_namespace(namespace) != "A3":
        return False
    for entry in settings.AUTONOMY_A3_ALLOWLIST.split(","):
        entry = entry.strip()
        if not entry or "/" not in entry:
            continue
        pb, ns_pattern = entry.split("/", 1)
        if pb.strip() == playbook and fnmatch(namespace, ns_pattern.strip()):
            return True
    return False
