"""Auto-approve is a demonstration and evaluation mode, off unless `AUTO_APPROVE_ENABLED` is set.

Auto-approve (`hitl_bypass`) lets a turn mutate the cluster without the approval prompt. It exists for demonstrations and
for the evaluation harness, which runs its scenarios with writes auto-approved. In the JGC round-1 pilots it was also the
path by which an autonomous A3 investigation acted with no human in the loop, and with one model an instruction injected
into a Job's logs used it to scale an unrelated deployment to zero. `AGENTS.md` safety invariant 2 says no flag may skip
the approval gate; this setting makes that the default again.

With `AUTO_APPROVE_ENABLED=false` (the default):
* no request body (`auto_approve`, i.e. `kq --auto-approve`), no "approve all" message and no watchtower turn runs with
  `hitl_bypass=True`;
* an A3-allowlisted finding is investigated like A2: the fix is proposed, not applied, so the turn does not stall on a
  prompt nobody can answer.
With it set to true, behaviour is exactly as before (and the namespace binding still applies).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent import workflow
from app.autonomy import watchtower
from app.core.config import Settings, settings
from app.detectors.models import Finding


class _NoInterrupt:
    tasks = ()


async def _bypass_seen(message: str, **kwargs) -> bool:
    seen: list[bool] = []

    async def _astream_events(input_data, config=None, version=None):
        seen.append(config["configurable"].get("hitl_bypass"))
        return
        yield  # pragma: no cover - makes this an async generator

    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_NoInterrupt())
    graph.astream_events = _astream_events
    with patch.object(workflow, "get_graph", AsyncMock(return_value=graph)), \
            patch.object(workflow, "get_langfuse_callbacks", lambda: None):
        async for _ in workflow.stream_events(message, "sess-demo", "u", "admin", **kwargs):
            pass
    assert len(seen) == 1
    return bool(seen[0])


def test_the_default_is_off():
    assert Settings.model_fields["AUTO_APPROVE_ENABLED"].default is False


@pytest.mark.asyncio
class TestTheChatPath:
    async def test_a_request_flag_does_not_bypass_when_the_mode_is_off(self, mocker):
        mocker.patch.object(settings, "AUTO_APPROVE_ENABLED", False)
        assert await _bypass_seen("scale the api deployment to 0", auto_approve=True) is False

    async def test_the_approve_all_phrase_does_not_bypass_when_the_mode_is_off(self, mocker):
        mocker.patch.object(settings, "AUTO_APPROVE_ENABLED", False)
        assert await _bypass_seen("approve all") is False

    async def test_both_still_bypass_when_the_mode_is_on(self, mocker):
        mocker.patch.object(settings, "AUTO_APPROVE_ENABLED", True)
        assert await _bypass_seen("scale the api deployment to 0", auto_approve=True) is True
        assert await _bypass_seen("approve all") is True


def _finding() -> Finding:
    return Finding(playbook="CrashLoopBackOff", cluster_id="c1", namespace="dev",
                   object_name="api-1", evidence="pod status=CrashLoopBackOff")


@pytest.mark.asyncio
class TestTheWatchtower:
    async def _run(self, mocker) -> dict:
        captured: dict = {}

        async def fake_run_session(ask, session_id, user_id, user_role, auto_approve, trigger_source,
                                   bound_namespace=None):
            captured.update(ask=ask, auto=auto_approve)

        async def empty_stream(sid, heartbeat_interval=5.0):
            return
            yield  # pragma: no cover

        mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
        mocker.patch("app.streaming.emitter.prepare_session")
        mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        mocker.patch.object(watchtower, "_autofix_revoked", AsyncMock(return_value=None))
        mocker.patch.object(watchtower, "_schedule_post_fix_recheck", AsyncMock())
        await watchtower._investigate(_finding(), "A3")
        return captured

    async def test_an_allowlisted_a3_finding_is_only_proposed_when_the_mode_is_off(self, mocker):
        mocker.patch.object(settings, "AUTO_APPROVE_ENABLED", False)
        captured = await self._run(mocker)
        assert captured["auto"] is False
        assert "do not execute destructive" in captured["ask"]
        assert "apply the appropriate fix" not in captured["ask"]

    async def test_an_allowlisted_a3_finding_is_fixed_when_the_mode_is_on(self, mocker):
        mocker.patch.object(settings, "AUTO_APPROVE_ENABLED", True)
        captured = await self._run(mocker)
        assert captured["auto"] is True
        assert "apply the appropriate fix" in captured["ask"]
