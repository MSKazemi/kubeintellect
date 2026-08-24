"""A flag can be ON, wired, and still doing nothing — because its subsystem is dead.

`set_but_unwired_flags` already answers "the operator set a switch no code reads". This file
covers the case one level out: the code *does* read the flag, but it lives inside the memory
hierarchy, and the hierarchy is not running. Measured 2026-08-24 — `/healthz` returned
`memory: {enabled: false, state: "unavailable"}` and `experimental_flags:
["MEMORY_HIERARCHY_ENABLED"]` in the *same response*, and `/v5/status`, whose docstring
promises "which v5 slices are active", reported the flag active and carried no way to see the
outage at all.

The deliberate non-change: these flags stay in `active_experimental_flags()`. That list is
rollout identity — which arm the pod was configured as — and it must not flap when Postgres
blips. Liveness is the new list's job.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.v5_status import v5_status
from app.core import readiness
from app.core.config import settings
from app.core.version import (
    active_experimental_flags,
    degraded_experimental_flags,
    set_but_unwired_flags,
    version_info,
)
from app.memory import service as mem


@pytest.fixture
def memory_state(monkeypatch):
    """Drive the memory hierarchy's state directly — these are the four values
    `init_memory` can leave behind, and each is reachable in production."""
    def _set(state: str, reason: str = ""):
        monkeypatch.setattr(mem, "_state", state)
        monkeypatch.setattr(mem, "_reason", reason)
    return _set


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_router)
    readiness.set_ready(True)
    return TestClient(app)


@pytest.fixture
def flags(monkeypatch):
    def _set(**kw):
        for name, value in kw.items():
            assert name in type(settings).model_fields, f"no such setting: {name}"
            monkeypatch.setattr(settings, name, value)
    return _set


# ── the defect ─────────────────────────────────────────────────────────────────

def test_a_hierarchy_that_cannot_reach_postgres_degrades_its_flags(memory_state, flags):
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("unavailable", "connection refused to postgres:5432")
    assert "MEMORY_HIERARCHY_ENABLED" in degraded_experimental_flags()


def test_a_slice_turned_on_with_the_hierarchy_off_is_degraded(memory_state, flags):
    """The case that needs no outage at all: the operator turns on a memory slice and leaves
    the hierarchy off, so there is nothing for the slice to run inside."""
    flags(MEMORY_HIERARCHY_ENABLED=False, MEMORY_HYBRID_RETRIEVAL=True,
          MEMORY_SUMMARY_TREE=True)
    memory_state("flag", "MEMORY_HIERARCHY_ENABLED=false")
    degraded = degraded_experimental_flags()
    assert "MEMORY_HYBRID_RETRIEVAL" in degraded
    assert "MEMORY_SUMMARY_TREE" in degraded
    # …and they are still reported as configured — identity is not liveness.
    assert "MEMORY_HYBRID_RETRIEVAL" in active_experimental_flags()


def test_sqlite_mode_degrades_them_too(memory_state, flags):
    """`USE_SQLITE=true` is a supported deployment and the hierarchy needs Postgres, so every
    memory slice is inert — a configuration, but still not a running one."""
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("sqlite", "USE_SQLITE=true — hierarchy needs Postgres")
    assert "MEMORY_HIERARCHY_ENABLED" in degraded_experimental_flags()


def test_startup_counts_as_degraded(memory_state, flags):
    """`starting` is the value before `init_memory` runs. Transient, self-clearing, and
    reporting a slice as working before it is up is the statement being removed."""
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("starting", "")
    assert degraded_experimental_flags() == ["MEMORY_HIERARCHY_ENABLED"]


# ── vacuity guards, both directions ────────────────────────────────────────────

def test_a_running_hierarchy_degrades_nothing(memory_state, flags):
    """Without this, a function that returns every ON memory flag unconditionally passes
    every assertion above."""
    flags(MEMORY_HIERARCHY_ENABLED=True, MEMORY_HYBRID_RETRIEVAL=True)
    memory_state("ready", "")
    assert degraded_experimental_flags() == []


def test_a_dead_hierarchy_does_not_degrade_unrelated_flags(memory_state, flags):
    """The other direction: the memory outage must not be blamed on flags that have nothing
    to do with memory, or the list becomes noise and gets ignored."""
    flags(MEMORY_HIERARCHY_ENABLED=True, KI_V5_KILL_SWITCH=True)
    memory_state("unavailable", "connection refused")
    assert "KI_V5_KILL_SWITCH" not in degraded_experimental_flags()
    assert "KI_V5_KILL_SWITCH" in active_experimental_flags()


def test_an_unwired_memory_flag_is_not_reported_twice(memory_state, flags, monkeypatch):
    """A flag no code reads is already answered by `set_but_unwired_flags`; naming it here too
    would say the subsystem is why it does nothing, which is not the reason.

    No `MEMORY_*` flag is in `UNWIRED_EXPERIMENTAL_FLAGS` today, so this is injected — and the
    injection is the point: without it the exclusion term in `degraded_experimental_flags` is
    untestable, and a mutant that removes it survives. Asserted below.
    """
    import app.core.version as version

    flags(MEMORY_HIERARCHY_ENABLED=True, MEMORY_KG_PPR=True)
    memory_state("unavailable", "connection refused")
    assert not any(f.startswith("MEMORY_") for f in version.UNWIRED_EXPERIMENTAL_FLAGS), (
        "a MEMORY_* flag is now genuinely unwired — use it here instead of injecting")

    assert "MEMORY_KG_PPR" in degraded_experimental_flags()      # before
    monkeypatch.setattr(version, "UNWIRED_EXPERIMENTAL_FLAGS",
                        version.UNWIRED_EXPERIMENTAL_FLAGS | {"MEMORY_KG_PPR"})
    degraded = degraded_experimental_flags()                     # after
    assert "MEMORY_KG_PPR" not in degraded
    assert "MEMORY_HIERARCHY_ENABLED" in degraded               # the rest is unaffected
    assert not (set(degraded) & version.UNWIRED_EXPERIMENTAL_FLAGS)


def test_an_off_flag_is_never_degraded(memory_state, flags):
    """Degraded is about flags the operator turned ON. A default-off slice is not a
    disappointment."""
    flags(MEMORY_HIERARCHY_ENABLED=False, MEMORY_KG_PPR=False)
    memory_state("unavailable", "connection refused")
    assert "MEMORY_KG_PPR" not in degraded_experimental_flags()


# ── the surfaces ───────────────────────────────────────────────────────────────

def test_version_info_carries_it(memory_state, flags):
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("unavailable", "connection refused")
    assert version_info()["degraded_experimental_flags"] == ["MEMORY_HIERARCHY_ENABLED"]


def test_healthz_reports_it(client, memory_state, flags):
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("unavailable", "connection refused to postgres:5432")
    body = client.get("/healthz").json()
    assert body["degraded_experimental_flags"] == ["MEMORY_HIERARCHY_ENABLED"]
    # identity is unchanged in the same response — that is the deliberate part
    assert "MEMORY_HIERARCHY_ENABLED" in body["experimental_flags"]
    assert body["memory"]["state"] == "unavailable"
    assert body["status"] == "ok"          # degraded is not wedged


def test_healthz_is_clean_when_memory_is_up(client, memory_state, flags):
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("ready", "")
    assert client.get("/healthz").json()["degraded_experimental_flags"] == []


def test_v5_status_reports_it_and_can_show_the_reason(memory_state, flags):
    """`/v5/status` is the surface that promises "which v5 slices are active" and could not
    see a memory outage at all before this."""
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("unavailable", "connection refused to postgres:5432")
    body = asyncio.run(v5_status())
    assert body.degraded_experimental_flags == ["MEMORY_HIERARCHY_ENABLED"]
    assert body.memory["state"] == "unavailable"
    assert "postgres:5432" in body.memory["reason"]
    assert "MEMORY_HIERARCHY_ENABLED" in body.active_flags


def test_v5_status_is_clean_when_memory_is_up(memory_state, flags):
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("ready", "")
    body = asyncio.run(v5_status())
    assert body.degraded_experimental_flags == []
    assert body.memory["enabled"] is True


# ── the premise this file rests on ─────────────────────────────────────────────

def test_every_memory_slice_really_does_live_inside_the_hierarchy():
    """The claim that makes blaming the hierarchy for all of them truthful. If a MEMORY_* flag
    is ever read outside the memory package and its two known readers, this file's rule is too
    broad and the guard must be narrowed rather than the test relaxed."""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "packages/kubeintellect-server/app"
    allowed_prefixes = ("memory/", "autonomy/watchtower.py", "agent/nodes/memory_loader.py",
                        "core/config.py", "core/version.py")
    pattern = re.compile(r"settings\.(MEMORY_[A-Z0-9_]+)")
    stray: list[str] = []
    for path in app_dir.rglob("*.py"):
        rel = str(path.relative_to(app_dir))
        if rel.startswith(allowed_prefixes):
            continue
        for name in pattern.findall(path.read_text()):
            stray.append(f"{rel}:{name}")
    assert not stray, f"MEMORY_* read outside the hierarchy: {stray}"


def test_the_two_lists_answer_different_questions(memory_state, flags):
    """Vacuity guard on the whole design: if `degraded` were just `unwired` under a new name,
    every test above would pass and nothing new would be reported."""
    flags(MEMORY_HIERARCHY_ENABLED=True)
    memory_state("unavailable", "connection refused")
    assert degraded_experimental_flags() != set_but_unwired_flags()
    assert degraded_experimental_flags()
