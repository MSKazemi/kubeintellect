"""A rollback point that cannot be applied must not report itself armed.

`docs/flight-recorder.md` says recovery is "manual but mechanical: pipe the captured state into
`kubectl apply -f -`", the digest listed every capture under **"Rollback points armed"**, and the
server logged `rollback_point_armed`. None of that survived contact with what is actually stored:
the YAML is redacted (it lands in Postgres) and capped at 4000 chars, and both transformations
can leave something that is not the object.

Measured 2026-08-20 with real kubectl (`bitnami/kubectl:latest`) at both ends — the fixtures
below are its verbatim output, and its verdict on the stored result is quoted in each test:

    Secret     `kind: Secret` contains "secret", a redaction keyword, so the line is dropped
               -> kubectl: `error: unable to decode "STDIN": Object 'Kind' is missing`
    ConfigMap  40-char values are token-shaped -> every one becomes `<redacted-token>`, and the
               result is still VALID: `kubectl label --local -f -` answers `configmap/app-config`.
               Applying it succeeds and overwrites all 120 values. The dangerous case.
    >4000ch    truncated mid-line (the project's own chart values.yaml is 7.4 KB)
               -> kubectl: `error parsing STDIN: ... did not find expected key`

Redaction is not the defect and is not negotiable — the alternative is credentials in the
database. Claiming restorability that is not there is the defect.
"""
from __future__ import annotations

import pytest

from app.tools import kubectl_tool

# ── verbatim output of real kubectl (bitnami/kubectl:latest) ──────────────────────────────
REAL_SECRET = """apiVersion: v1
data:
  password: aHVudGVyMg==
  token: YWJjZGVm
kind: Secret
metadata:
  name: db-creds
"""

REAL_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app: api
  name: api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: api
  strategy: {}
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
      - image: nginx:1.27
        name: nginx
        resources: {}
status: {}
"""

# Real `kubectl create configmap --from-file` output; the body is one real 40-char value per
# line, which is what makes `_TOKEN_RE` fire on every one of them.
REAL_CONFIGMAP = (
    "apiVersion: v1\ndata:\n  app.properties: |\n"
    + "".join(f"    key{i}: u8jzPde0IgxLd6GncfBAepfJBd0Kh8oOOL8dKLzd\n" for i in range(20))
    + "kind: ConfigMap\nmetadata:\n  name: app-config\n"
)

# Same shape, but the values are prose rather than token-shaped, so nothing is redacted and the
# only transformation left is the 4000-char cap.
BIG_PLAIN_CONFIGMAP = (
    "apiVersion: v1\ndata:\n  notes: |\n"
    + "".join(f"    line {i}: the quick brown fox jumps over the lazy dog again and again\n"
             for i in range(80))
    + "kind: ConfigMap\nmetadata:\n  name: notes\n"
)


class _Recorded:
    """Captures what _capture_rollback_point handed to the flight recorder."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, episode_id, kind, payload):
        self.calls.append((episode_id, kind, payload))

    @property
    def payload(self) -> dict:
        assert self.calls, "nothing was recorded"
        return self.calls[-1][2]


@pytest.fixture
def capture(mocker):
    """Run the real capture against one canned `kubectl get -o yaml` result."""
    rec = _Recorded()
    from app.db import flight_recorder
    mocker.patch.object(flight_recorder, "record", rec)

    def _run(stdout: str, *, args=None):
        class _Proc:
            returncode = 0
        proc = _Proc()
        proc.stdout = stdout
        mocker.patch.object(kubectl_tool.subprocess, "run", return_value=proc)
        kubectl_tool._capture_rollback_point(
            "delete", args or ["kubectl", "delete", "secret", "db-creds"], None, None, {}
        )
        return rec
    return _run


class TestARedactedCaptureIsNotARestorePoint:
    def test_a_secret_capture_is_marked_not_restorable(self, capture):
        rec = capture(REAL_SECRET)
        assert rec.payload["restorable"] is False
        assert rec.payload["capture_notes"], "no reason was recorded"

    def test_the_stored_secret_really_has_lost_its_credentials(self, capture):
        """The claim above is that the capture cannot be re-applied; assert the cause.

        CHANGED-2026-08-20. This asserted `"kind: Secret" not in stored` — the old
        redactor dropped any line containing "secret", so the manifest lost its *type*
        and kubectl would have refused it with `Object 'Kind' is missing`. That was an
        accident of substring matching, not a safety property: deleting `kind:` while
        keeping the payload is the wrong half to delete. The redactor now keeps
        structural fields and removes the values, so the capture is unrestorable for the
        honest reason — the credentials are gone — which is what this asserts.
        """
        stored = capture(REAL_SECRET).payload["pre_state"][0]
        assert "kind: Secret" in stored, "structural type is not a credential"
        assert "aHVudGVyMg==" not in stored, "the Secret's data survived redaction"
        assert "YWJjZGVm" not in stored
        # The key names survive, so a reviewer grepping the store can still find this.
        assert "password:" in stored and "token:" in stored

    def test_a_configmap_whose_values_were_replaced_is_marked_not_restorable(self, capture):
        rec = capture(REAL_CONFIGMAP)
        assert rec.payload["restorable"] is False
        stored = rec.payload["pre_state"][0]
        # Still valid YAML with the right kind — this one APPLIES CLEANLY and destroys the data.
        assert "kind: ConfigMap" in stored
        assert "<redacted-token>" in stored
        assert "value(s) replaced" in " ".join(rec.payload["capture_notes"])

    def test_a_truncated_capture_is_marked_not_restorable(self, capture):
        rec = capture(BIG_PLAIN_CONFIGMAP)
        assert rec.payload["restorable"] is False
        assert rec.payload["pre_state"][0].endswith("[...]")
        assert "truncated at 4000 chars" in " ".join(rec.payload["capture_notes"])

    def test_an_unchanged_capture_is_still_restorable(self, capture):
        rec = capture(REAL_DEPLOYMENT)
        assert rec.payload["restorable"] is True
        assert rec.payload["capture_notes"] == []
        # byte-identical to what kubectl produced, trailing newline aside
        assert rec.payload["pre_state"][0].rstrip("\n") == REAL_DEPLOYMENT.rstrip("\n")

    def test_the_dropped_trailing_newline_alone_never_costs_the_flag(self, capture):
        # `redact_secrets` joins lines and loses the final "\n". If that counted as a change,
        # every capture would be marked unrestorable and the flag would mean nothing.
        rec = capture(REAL_DEPLOYMENT)
        assert rec.payload["pre_state"][0] != REAL_DEPLOYMENT
        assert rec.payload["restorable"] is True

    def test_the_capture_is_still_recorded_when_it_cannot_be_restored(self, capture):
        # It remains evidence of what the object looked like — it just is not a restore point.
        rec = capture(REAL_SECRET)
        assert rec.calls[-1][1] == "rollback_point"
        assert rec.payload["pre_state"]


class TestTheOperatorIsTold:
    def test_the_log_says_armed_only_when_it_is(self, capture, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            capture(REAL_DEPLOYMENT)
        assert "rollback_point_armed" in caplog.text

    def test_an_unrestorable_capture_logs_a_warning_not_armed(self, capture, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            capture(REAL_SECRET)
        assert "rollback_point_armed" not in caplog.text
        assert "NOT restorable, do not apply it" in caplog.text


class TestTheDigestDoesNotSayArmed:
    @staticmethod
    def _digest(points: list[dict]) -> str:
        from app.digest import builder
        digest = {
            "window_hours": 24.0, "findings": [], "auto_investigations": [],
            "rollback_points": points, "user_sessions": 0, "summary": "",
            "counts": {}, "degraded": False, "degraded_reasons": [],
        }
        return builder.render_markdown(digest)

    def test_a_non_restorable_capture_is_not_listed_as_armed(self):
        md = self._digest([{"at": 0.0, "rollback_id": "rb-1", "command": "kubectl delete secret x",
                            "restorable": False, "capture_notes": ["secret db-creds: redacted"]}])
        assert "NOT restorable" in md
        assert "do not apply" in md
        assert "0 of 1 restorable" in md

    def test_a_restorable_capture_still_reads_as_usable(self):
        md = self._digest([{"at": 0.0, "rollback_id": "rb-2", "command": "kubectl scale deploy/api",
                            "restorable": True, "capture_notes": []}])
        assert "[restorable]" in md
        assert "NOT restorable" not in md
        assert "1 of 1 restorable" in md

    def test_a_record_written_before_the_flag_is_unknown_not_armed(self):
        md = self._digest([{"at": 0.0, "rollback_id": "rb-3", "command": "kubectl delete pod x",
                            "restorable": None, "capture_notes": []}])
        assert "restorability unknown" in md
        assert "0 of 1 restorable" in md


class TestTheFlagSurvivesTheRoundTrip:
    """The digest render is only honest if `build_digest` actually carries the field out of the
    recorded payload — asserted through the real query path, not by hand-building the dict."""

    @staticmethod
    def _build(payload: dict) -> dict:
        import asyncio
        import json
        from datetime import datetime, timezone
        from unittest.mock import patch

        from app.core.config import settings
        from app.digest.builder import build_digest

        class _Pool:
            async def fetch(self, sql, *a, **k):
                if "decision_log" not in sql:
                    return []            # the episodes query — not what this asserts
                return [{"episode_id": "s1", "kind": "rollback_point",
                         "payload": json.dumps(payload),
                         "created_at": datetime.now(tz=timezone.utc)}]

        with patch("app.memory.service._pool", _Pool()):
            with patch.multiple(settings, FLIGHT_RECORDER_ENABLED=True, USE_SQLITE=False,
                                WATCHTOWER_ENABLED=True):
                return asyncio.run(build_digest(24.0))

    def test_a_recorded_false_reaches_the_digest(self):
        d = self._build({"type": "rollback_point", "rollback_id": "rb-1",
                         "command": "kubectl delete secret x", "restorable": False,
                         "capture_notes": ["secret x: redacted: 1 line(s) dropped"]})
        assert d["rollback_points"][0]["restorable"] is False
        assert d["rollback_points"][0]["capture_notes"] == ["secret x: redacted: 1 line(s) dropped"]

    def test_a_recorded_true_reaches_the_digest(self):
        d = self._build({"type": "rollback_point", "rollback_id": "rb-2",
                         "command": "kubectl scale deploy/api", "restorable": True,
                         "capture_notes": []})
        assert d["rollback_points"][0]["restorable"] is True

    def test_an_older_payload_arrives_as_unknown_not_true(self):
        d = self._build({"type": "rollback_point", "rollback_id": "rb-3",
                         "command": "kubectl delete pod x"})
        assert d["rollback_points"][0]["restorable"] is None


class TestThePostmortemDoesNotSayArmed:
    @staticmethod
    def _summary(payload: dict) -> str:
        from app.digest import postmortem
        return postmortem._summarize("rollback_point", payload)

    def test_a_non_restorable_capture_is_flagged_in_the_timeline(self):
        text = self._summary({"rollback_id": "rb-1", "command": "kubectl delete secret x",
                              "restorable": False})
        assert "NOT restorable" in text

    def test_a_restorable_capture_reads_as_before(self):
        text = self._summary({"rollback_id": "rb-2", "command": "kubectl scale deploy/api",
                              "restorable": True})
        assert "NOT restorable" not in text
        assert "rb-2" in text

    def test_an_older_record_is_not_claimed_either_way(self):
        text = self._summary({"rollback_id": "rb-3", "command": "kubectl delete pod x"})
        assert "restorability not recorded" in text
