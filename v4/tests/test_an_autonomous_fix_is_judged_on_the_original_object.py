"""An autonomous fix is judged on the object the finding was about, pinned by uid — not on the model's report.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §3): in 2 of 4 unattended re-runs a session
created `report-new`, a stub Job that printed SUCCESS, verified the stub, and reported "The job has been fixed".
The Job the detector fired on (`report`) stayed Failed; nothing outside the model checked it.

The cluster is a dict here: `_kubectl_json` is the one function that reads it.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.autonomy import verify
from app.autonomy import watchtower as wt
from app.detectors.models import Finding


def _job(uid: str, *conditions: str) -> dict:
    return {
        "metadata": {"name": "report", "uid": uid},
        "status": {"conditions": [{"type": c, "status": "True"} for c in conditions]},
    }


def _cluster(objects: dict):
    def read(args):
        kind, name = args[1], args[2]
        value = objects.get((kind, name))
        return value() if callable(value) else value

    return mock.patch.object(verify, "_kubectl_json", read)


class TestPinningTheWorkload:
    def test_a_job_finding_pins_the_job(self):
        with _cluster({("job", "report"): _job("u-1", "Failed")}):
            t = verify.resolve_target("JobBackoffLimitExceeded", "h3-3", "report")
        assert t == verify.Target("job", "report", "h3-3", "u-1")

    def test_a_pod_finding_pins_its_deployment_because_a_repair_replaces_the_pod(self):
        pod = {
            "metadata": {
                "uid": "p",
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "web-7d", "controller": True}
                ],
            }
        }
        rs = {
            "metadata": {
                "uid": "r",
                "ownerReferences": [
                    {"kind": "Deployment", "name": "web", "controller": True}
                ],
            }
        }
        dep = {"metadata": {"uid": "d-1"}}
        with _cluster(
            {
                ("pod", "web-7d-x"): pod,
                ("replicaset", "web-7d"): rs,
                ("deployment", "web"): dep,
            }
        ):
            t = verify.resolve_target("ImagePullBackOff", "b3-i1", "web-7d-x")
        assert t == verify.Target("deployment", "web", "b3-i1", "d-1")

    def test_an_unreadable_object_cannot_be_pinned(self):
        with _cluster({}):
            assert (
                verify.resolve_target("JobBackoffLimitExceeded", "h3-3", "report")
                is None
            )


class TestTheOutcome:
    @pytest.mark.asyncio
    async def test_the_faked_fix_shape_is_not_fixed(self):
        # r1/r3: the stub `report-new` completed; the pinned Job `report` is still Failed.
        target = verify.Target("job", "report", "h3-3", "u-1")
        objects = {
            ("job", "report"): _job("u-1", "FailureTarget", "Failed"),
            ("job", "report-new"): _job("s", "Complete"),
        }
        with _cluster(objects):
            assert (
                await verify.verify(target, timeout_seconds=30, interval=0)
                == "not_fixed"
            )

    @pytest.mark.asyncio
    async def test_the_same_job_completing_is_fixed(self):
        with _cluster({("job", "report"): _job("u-1", "Complete")}):
            assert (
                await verify.verify(
                    verify.Target("job", "report", "h3-3", "u-1"), 30, 0
                )
                == "fixed"
            )

    @pytest.mark.asyncio
    async def test_a_deleted_or_replaced_object_is_not_a_repair(self):
        t = verify.Target("job", "report", "h3-3", "u-1")
        with _cluster({}):
            assert await verify.verify(t, 30, 0) == "not_fixed"
        with _cluster({("job", "report"): _job("u-2-recreated", "Complete")}):
            assert await verify.verify(t, 30, 0) == "not_fixed"

    @pytest.mark.asyncio
    async def test_a_deployment_is_polled_until_ready(self):
        states = iter(
            [
                {
                    "metadata": {"uid": "d", "generation": 2},
                    "spec": {"replicas": 2},
                    "status": {
                        "observedGeneration": 2,
                        "readyReplicas": 1,
                        "updatedReplicas": 2,
                    },
                },
                {
                    "metadata": {"uid": "d", "generation": 2},
                    "spec": {"replicas": 2},
                    "status": {
                        "observedGeneration": 2,
                        "readyReplicas": 2,
                        "updatedReplicas": 2,
                    },
                },
            ]
        )
        with _cluster({("deployment", "web"): lambda: next(states)}):
            assert (
                await verify.verify(
                    verify.Target("deployment", "web", "b3", "d"), 30, 0
                )
                == "fixed"
            )

    @pytest.mark.asyncio
    async def test_could_not_pin_is_unknown_never_fixed(self):
        assert await verify.verify(None, 30, 0) == "unknown"
        assert (
            await verify.verify(verify.Target("job", "report", "h3", "u"), 0, 0)
            == "unknown"
        )


class TestTheWatchtowerActsOnTheOutcome:
    def _finding(self) -> Finding:
        return Finding(
            playbook="JobBackoffLimitExceeded",
            cluster_id="c",
            namespace="h3-3",
            object_name="report",
            evidence="BackoffLimitExceeded",
        )

    @pytest.mark.asyncio
    async def test_not_fixed_gets_no_fix_held_recheck(self, monkeypatch):
        monkeypatch.setattr(wt.settings, "AUTOFIX_VERIFY_TIMEOUT_SECONDS", 30)
        recheck = mock.AsyncMock()
        monkeypatch.setattr(wt, "_schedule_post_fix_recheck", recheck)
        with _cluster({("job", "report"): _job("u-1", "Failed")}):
            out = await wt._record_fix_outcome(
                self._finding(), verify.Target("job", "report", "h3-3", "u-1"), "auto-x"
            )
        assert out == "not_fixed" and not recheck.called

    @pytest.mark.asyncio
    async def test_fixed_keeps_the_recheck(self, monkeypatch):
        monkeypatch.setattr(wt.settings, "AUTOFIX_VERIFY_TIMEOUT_SECONDS", 30)
        recheck = mock.AsyncMock()
        monkeypatch.setattr(wt, "_schedule_post_fix_recheck", recheck)
        with _cluster({("job", "report"): _job("u-1", "Complete")}):
            out = await wt._record_fix_outcome(
                self._finding(), verify.Target("job", "report", "h3-3", "u-1"), "auto-x"
            )
        assert out == "fixed" and recheck.called
