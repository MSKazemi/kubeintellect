"""Autonomy ladder, watchtower, Safety Sandwich rollback capture (P5, ADR-003)."""
from __future__ import annotations

import asyncio

from app.autonomy import ladder, watchtower
from app.detectors.models import Finding


def _finding(playbook="CrashLoopBackOff", ns="dev", obj="web-1"):
    return Finding(
        playbook=playbook, cluster_id="c1", namespace=ns,
        object_name=obj, evidence="pod status=CrashLoopBackOff",
    )


class TestLadder:
    def test_default_level(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A1")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        assert ladder.level_for_namespace("anything") == "A1"

    def test_namespace_override(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A1")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "prod=A0, dev=A2")
        assert ladder.level_for_namespace("prod") == "A0"
        assert ladder.level_for_namespace("dev") == "A2"
        assert ladder.level_for_namespace("other") == "A1"

    def test_protected_namespaces_pinned_to_a0(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A3")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        assert ladder.level_for_namespace("kube-system") == "A0"
        assert ladder.level_for_namespace("monitoring") == "A0"

    def test_invalid_level_falls_back(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A9")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        assert ladder.level_for_namespace("x") == "A1"

    def test_ordering(self):
        assert ladder.at_least("A3", "A1")
        assert not ladder.at_least("A0", "A1")

    def test_a3_allowlist(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A3")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        mocker.patch.object(
            ladder.settings, "AUTONOMY_A3_ALLOWLIST",
            "CrashLoopBackOff/dev-*, ImagePullBackOff/staging",
        )
        assert ladder.a3_allowed("CrashLoopBackOff", "dev-payments")
        assert not ladder.a3_allowed("CrashLoopBackOff", "prod")
        assert ladder.a3_allowed("ImagePullBackOff", "staging")
        assert not ladder.a3_allowed("OOMKilled", "staging")

    def test_a3_requires_level_a3(self, mocker):
        mocker.patch.object(ladder.settings, "AUTONOMY_LEVEL", "A1")
        mocker.patch.object(ladder.settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        mocker.patch.object(ladder.settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoopBackOff/dev")
        assert not ladder.a3_allowed("CrashLoopBackOff", "dev")


class TestWatchtower:
    async def test_a0_namespace_spawns_nothing(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", True)
        mocker.patch("app.autonomy.watchtower.level_for_namespace", return_value="A0")
        investigate = mocker.patch.object(watchtower, "_investigate")
        watchtower.on_finding(_finding())
        await asyncio.sleep(0)
        investigate.assert_not_called()

    async def test_a1_finding_spawns_investigation(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", True)
        mocker.patch("app.autonomy.watchtower.level_for_namespace", return_value="A1")
        investigate = mocker.patch.object(
            watchtower, "_investigate", new=mocker.AsyncMock()
        )
        watchtower.on_finding(_finding())
        await asyncio.sleep(0.01)
        investigate.assert_awaited_once()
        finding, level = investigate.await_args.args
        assert level == "A1"
        assert finding.playbook == "CrashLoopBackOff"

    async def test_cooldown_blocks_repeat(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", True)
        mocker.patch("app.autonomy.watchtower.level_for_namespace", return_value="A1")
        investigate = mocker.patch.object(
            watchtower, "_investigate", new=mocker.AsyncMock()
        )
        watchtower.on_finding(_finding())
        watchtower.on_finding(_finding())
        await asyncio.sleep(0.01)
        assert investigate.await_count == 1

    async def test_disabled_flag(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", False)
        investigate = mocker.patch.object(watchtower, "_investigate")
        watchtower.on_finding(_finding())
        await asyncio.sleep(0)
        investigate.assert_not_called()

    async def test_predicted_finding_arms_pre_capture(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", True)
        mocker.patch.object(watchtower.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(watchtower.settings, "KI_V5_PREDICTIVE_PRECAPTURE", True)
        mocker.patch("app.autonomy.watchtower.level_for_namespace", return_value="A1")
        mocker.patch.object(watchtower, "_investigate", new=mocker.AsyncMock())
        from app.sensorium.pre_capture import PreCapturePlan
        spy = mocker.patch("app.sensorium.pre_capture.plan_pre_capture",
                           return_value=PreCapturePlan(target="web-1", namespace="dev",
                                                       actions=["raise_log_verbosity"]))
        pred = Finding(playbook="OOMKilled", cluster_id="c1", namespace="dev", object_name="web-1",
                       evidence="mem up", severity="predicted", eta_minutes=8.0)
        watchtower.on_finding(pred)
        await asyncio.sleep(0.01)
        spy.assert_called_once()

    async def test_pre_capture_off_by_default(self, mocker):
        watchtower.reset_cooldowns()
        mocker.patch.object(watchtower.settings, "WATCHTOWER_ENABLED", True)
        mocker.patch.object(watchtower.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(watchtower.settings, "KI_V5_PREDICTIVE_PRECAPTURE", False)
        mocker.patch("app.autonomy.watchtower.level_for_namespace", return_value="A1")
        mocker.patch.object(watchtower, "_investigate", new=mocker.AsyncMock())
        spy = mocker.patch("app.sensorium.pre_capture.plan_pre_capture")
        watchtower.on_finding(_finding())
        await asyncio.sleep(0.01)
        spy.assert_not_called()

    async def test_investigation_prompt_levels(self, mocker):
        """A2 asks for proposals; A3-allowlisted asks for the fix + bypasses HITL."""
        captured = {}

        async def fake_run_session(ask, session_id, user_id, user_role, auto_approve):
            captured.update(ask=ask, auto=auto_approve)

        mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
        mocker.patch("app.streaming.emitter.prepare_session")

        async def empty_stream(sid, heartbeat_interval=5.0):
            return
            yield

        mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        await watchtower._investigate(_finding(), "A3")
        assert captured["auto"] is True
        assert "apply the appropriate fix" in captured["ask"]

        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=False)
        await watchtower._investigate(_finding(), "A2")
        assert captured["auto"] is False
        assert "do not execute destructive" in captured["ask"]

    async def test_autofix_schedules_post_fix_recheck(self, mocker):
        """Memory V5 P6 (ADR-017): an autonomous fix records a prospective re-check."""
        async def fake_run_session(*a, **k):
            return

        mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
        mocker.patch("app.streaming.emitter.prepare_session")

        async def empty_stream(sid, heartbeat_interval=5.0):
            return
            yield

        mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        mocker.patch.object(watchtower.settings, "MEMORY_PROSPECTIVE", True)
        sched = mocker.patch(
            "app.memory.prospective.schedule_recheck", new=mocker.AsyncMock()
        )
        await watchtower._investigate(_finding(), "A3")             # auto_fix path
        sched.assert_awaited_once()
        kw = sched.await_args.kwargs
        assert kw["namespace"] == "dev" and kw["created_by"] == "watchtower"
        assert kw["dedup_key"] == "recheck:CrashLoopBackOff:dev:web-1"

    async def test_no_recheck_when_prospective_off(self, mocker):
        async def fake_run_session(*a, **k):
            return

        mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
        mocker.patch("app.streaming.emitter.prepare_session")

        async def empty_stream(sid, heartbeat_interval=5.0):
            return
            yield

        mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        mocker.patch.object(watchtower.settings, "MEMORY_PROSPECTIVE", False)
        sched = mocker.patch(
            "app.memory.prospective.schedule_recheck", new=mocker.AsyncMock()
        )
        await watchtower._investigate(_finding(), "A3")
        sched.assert_not_awaited()

    def test_predicted_finding_never_autofix(self, mocker):
        """Safety contract (ADR-010): a predicted finding cannot auto-fix even at
        A3 on an allowlisted pair."""
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        realized = _finding()
        predicted = Finding(
            playbook="OOMKilled", cluster_id="c1", namespace="dev",
            object_name="web-1", evidence="predicted OOM in ~7m",
            severity="predicted", source="trend", eta_minutes=7.0,
        )
        assert watchtower._should_auto_fix(realized, "A3") is True
        assert watchtower._should_auto_fix(predicted, "A3") is False

    async def test_predicted_investigation_prompt_is_preemptive(self, mocker):
        captured = {}

        async def fake_run_session(ask, session_id, user_id, user_role, auto_approve):
            captured.update(ask=ask, auto=auto_approve)

        mocker.patch("app.agent.workflow.run_session", side_effect=fake_run_session)
        mocker.patch("app.streaming.emitter.prepare_session")

        async def empty_stream(sid, heartbeat_interval=5.0):
            return
            yield

        mocker.patch("app.streaming.emitter.stream", side_effect=empty_stream)
        mocker.patch("app.autonomy.watchtower.a3_allowed", return_value=True)
        predicted = Finding(
            playbook="OOMKilled", cluster_id="c1", namespace="dev",
            object_name="web-1", evidence="predicted OOM in ~7m",
            severity="predicted", source="trend", eta_minutes=7.0,
        )
        await watchtower._investigate(predicted, "A3")
        assert captured["auto"] is False
        assert "PREDICTED" in captured["ask"]
        assert "Do NOT execute destructive" in captured["ask"]


class TestRollbackCapture:
    def test_delete_command_captures_pre_state(self, mocker):
        from app.tools import kubectl_tool as kt

        record = mocker.patch("app.db.flight_recorder.record")
        proc = mocker.MagicMock(returncode=0, stdout="kind: Pod\nmetadata:\n  name: web-1\n")
        mocker.patch.object(kt.subprocess, "run", return_value=proc)

        kt._capture_rollback_point(
            "delete",
            ["kubectl", "delete", "pod", "web-1", "-n", "shop"],
            None,
            {"configurable": {"thread_id": "s1"}},
            {},
        )
        record.assert_called_once()
        episode_id, kind, payload = record.call_args[0]
        assert episode_id == "s1"
        assert kind == "rollback_point"
        assert payload["rollback_id"].startswith("rb-")
        assert "kind: Pod" in payload["pre_state"][0]

    def test_apply_stdin_targets_parsed_from_yaml(self, mocker):
        from app.tools import kubectl_tool as kt

        record = mocker.patch("app.db.flight_recorder.record")
        proc = mocker.MagicMock(returncode=0, stdout="kind: Deployment\n")
        run = mocker.patch.object(kt.subprocess, "run", return_value=proc)

        stdin = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: shop\n"
        kt._capture_rollback_point(
            "apply", ["kubectl", "apply", "-f", "-"], stdin,
            {"configurable": {"thread_id": "s2"}}, {},
        )
        get_args = run.call_args[0][0]
        assert get_args[:4] == ["kubectl", "get", "deployment", "web"]
        assert record.called

    def test_capture_never_raises(self, mocker):
        from app.tools import kubectl_tool as kt

        mocker.patch.object(kt.subprocess, "run", side_effect=RuntimeError("boom"))
        # must not raise
        kt._capture_rollback_point(
            "delete", ["kubectl", "delete", "pod", "x"], None, None, {}
        )

    def test_secrets_redacted_in_pre_state(self, mocker):
        from app.tools import kubectl_tool as kt

        record = mocker.patch("app.db.flight_recorder.record")
        proc = mocker.MagicMock(
            returncode=0, stdout="kind: ConfigMap\ndata:\n  password: hunter2\n"
        )
        mocker.patch.object(kt.subprocess, "run", return_value=proc)
        kt._capture_rollback_point(
            "delete", ["kubectl", "delete", "configmap", "x", "-n", "s"], None, None, {}
        )
        payload = record.call_args[0][2]
        assert "hunter2" not in str(payload)
