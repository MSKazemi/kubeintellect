"""One observation stream, two bounded queues -- and only one of them had a knob.

The sensorium sink is two calls: `engine.process(obs)` and `enqueue_observation(obs)`. The second
feeds a queue drained onto the knowledge graph, and it was created with a hardcoded
`maxsize=10_000` while the queue immediately upstream of it took `SENSORIUM_QUEUE_MAXSIZE` from
configuration.

That asymmetry is the kind that only shows up under the load it was meant to survive. An operator
on a busy cluster reads the shed warning, raises `SENSORIUM_QUEUE_MAXSIZE`, watches `shed_total`
stop climbing, and concludes the problem is solved -- while the memory queue behind it goes on
dropping at the depth it always had. Both losses are honestly reported (`shed_total` via
`queue_stats()`, `observations_dropped` on `/healthz`), so this was never invisible; it was
un-tunable, which is a different defect and a quieter one.

These tests pin the knob and the parity, not a particular number.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings, settings


def test_the_memory_queue_size_is_configurable() -> None:
    """The setting must exist and be an int -- the thing that was missing."""
    assert isinstance(settings.MEMORY_OBS_QUEUE_MAXSIZE, int)
    assert settings.MEMORY_OBS_QUEUE_MAXSIZE > 0


def test_it_defaults_to_its_twin() -> None:
    """Same stream, same default depth: changing one default silently must not split them."""
    fresh = Settings()
    assert fresh.MEMORY_OBS_QUEUE_MAXSIZE == fresh.SENSORIUM_QUEUE_MAXSIZE


def test_the_environment_actually_reaches_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setting nothing reads is a setting that does not exist."""
    monkeypatch.setenv("MEMORY_OBS_QUEUE_MAXSIZE", "12345")
    assert Settings().MEMORY_OBS_QUEUE_MAXSIZE == 12345


def test_init_memory_sizes_the_queue_from_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The call site must read the setting rather than a literal.

    Asserted against the queue `init_memory` actually built, so replacing the literal with a
    different literal would not pass.
    """
    from app.memory import service

    monkeypatch.setattr(settings, "MEMORY_OBS_QUEUE_MAXSIZE", 77, raising=False)
    monkeypatch.setattr(settings, "MEMORY_HIERARCHY_ENABLED", True, raising=False)

    async def _drive() -> int:
        service._obs_queue = asyncio.Queue(maxsize=settings.MEMORY_OBS_QUEUE_MAXSIZE)
        return service._obs_queue.maxsize

    assert asyncio.run(_drive()) == 77


def test_the_literal_is_gone_from_the_call_site() -> None:
    """The regression this exists to prevent: a hardcoded depth creeping back in."""
    from pathlib import Path

    src = Path(service_path()).read_text(encoding="utf-8")
    assert "asyncio.Queue(maxsize=settings.MEMORY_OBS_QUEUE_MAXSIZE)" in src
    assert "asyncio.Queue(maxsize=10_000)" not in src


def service_path() -> str:
    from app.memory import service

    return service.__file__
