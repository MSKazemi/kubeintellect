"""Playbook YAML loader and snapshot trigger matcher.

Schema (one .yaml file per playbook):

    name: CrashLoopBackOff
    triggers:
      - pod_status_regex: "CrashLoopBackOff"
      - event_reason_regex: "BackOff"
    investigation_steps:
      - "Describe the pod to inspect Last State and exit code."
      - "kubectl logs --previous --tail=50."
    expected_evidence:
      - "Exit code in Last State"
    recommended_fix_template: >
      The pod's container is crashing. Likely cause is <CAUSE>; suggested
      fix: <ACTION>.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from app.utils.logger import get_logger

logger = get_logger(__name__)

_PLAYBOOK_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Trigger:
    pod_status_regex: re.Pattern | None = None
    event_reason_regex: re.Pattern | None = None
    event_message_regex: re.Pattern | None = None


@dataclass(frozen=True)
class Playbook:
    name: str
    triggers: tuple[Trigger, ...] = field(default_factory=tuple)
    investigation_steps: tuple[str, ...] = field(default_factory=tuple)
    expected_evidence: tuple[str, ...] = field(default_factory=tuple)
    recommended_fix_template: str = ""


def _compile_trigger(raw: dict) -> Trigger:
    def _maybe(pattern: str | None) -> re.Pattern | None:
        return re.compile(pattern, re.IGNORECASE) if pattern else None

    return Trigger(
        pod_status_regex=_maybe(raw.get("pod_status_regex")),
        event_reason_regex=_maybe(raw.get("event_reason_regex")),
        event_message_regex=_maybe(raw.get("event_message_regex")),
    )


def _load_one(path: Path) -> Playbook:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Playbook {path.name} must be a YAML mapping")
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"Playbook {path.name} is missing a 'name'")
    triggers_raw = data.get("triggers") or []
    if not isinstance(triggers_raw, list):
        raise ValueError(f"Playbook {path.name}: 'triggers' must be a list")
    triggers = tuple(_compile_trigger(t) for t in triggers_raw if isinstance(t, dict))
    steps = tuple(data.get("investigation_steps") or [])
    evidence = tuple(data.get("expected_evidence") or [])
    fix = (data.get("recommended_fix_template") or "").strip()
    return Playbook(
        name=name,
        triggers=triggers,
        investigation_steps=steps,
        expected_evidence=evidence,
        recommended_fix_template=fix,
    )


def _load_all() -> dict[str, Playbook]:
    registry: dict[str, Playbook] = {}
    for path in sorted(_PLAYBOOK_DIR.glob("*.yaml")):
        try:
            pb = _load_one(path)
        except Exception as exc:
            logger.error(f"playbook_load_failed file={path.name} error={exc!r}")
            continue
        registry[pb.name] = pb
    logger.info(f"playbooks loaded: {sorted(registry.keys())}")
    return registry


# Loaded once at import time; immutable thereafter.
_REGISTRY: dict[str, Playbook] = _load_all()


def list_playbooks() -> Iterable[Playbook]:
    return _REGISTRY.values()


def get_playbook(name: str) -> Playbook | None:
    return _REGISTRY.get(name)


def match_playbooks(pods_out: str, events_out: str) -> list[str]:
    """Return names of playbooks whose triggers match the snapshot.

    A playbook is considered matched if ANY of its triggers matches.
    """
    matched: list[str] = []
    for pb in _REGISTRY.values():
        if not pb.triggers:
            continue
        for trig in pb.triggers:
            if trig.pod_status_regex and trig.pod_status_regex.search(pods_out):
                matched.append(pb.name)
                break
            if trig.event_reason_regex and trig.event_reason_regex.search(events_out):
                matched.append(pb.name)
                break
            if trig.event_message_regex and trig.event_message_regex.search(events_out):
                matched.append(pb.name)
                break
    return matched
