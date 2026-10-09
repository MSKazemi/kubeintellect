"""ADR-008 — exactly-once irreversible calls and single-use approvals, at the guarded-tool boundary.

What would have to break for each test to fail:

* replay — the guard executes an irreversible call a second time at the same rollback point
  instead of returning the recorded result (the `kubectl delete` subprocess is counted);
* different target — a retry that drifted onto another PVC runs without an explicit fork;
* consumed approval — a resumed node is handed a stale approval (LangGraph replays resume
  values by position) and the guard honours it ("Authority Resurrection");
* effect log unavailable — the guard trusts the auto-approve bypass without a ledger, i.e.
  silently re-executes instead of falling back to a human;
* flag off — any of this machinery changes today's behaviour when SELF_GOVERN_ENABLED is false.

Everything runs against an in-memory ledger that keeps the real hash chain
(`flight_recorder.compute_hash`) and the database's one hard constraint (a token is consumed
once), so no Postgres and no cluster are needed.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.db import effect_log
from app.db.flight_recorder import compute_hash
from app.tools import effect_guard


# ── In-memory ledger with the same contract as PostgresEffectStore ───────────


class _Txn:
    def __init__(self, store: _Store, session_id: str):
        self._store = store
        self._session_id = session_id
        self.rows = [dict(r) for r in store.rows.get(session_id, [])]

    def append(self, kind, payload):
        assert kind in effect_log.KINDS
        payload = json.loads(json.dumps(payload, default=str))
        if kind == effect_log.APPROVAL_CONSUMED and payload["token"] in self._store.consumed():
            # uq_effect_log_token_consumed — the database refuses a second consumption.
            raise effect_log.EffectLogUnavailable("duplicate key value violates unique constraint")
        last = self.rows[-1] if self.rows else None
        seq = last["seq"] + 1 if last else 0
        prev = last["hash"] if last else ""
        row = {"session_id": self._session_id, "seq": seq, "kind": kind, "payload": payload,
               "prev_hash": prev, "hash": compute_hash(prev, self._session_id, seq, kind, payload),
               "created_at": f"t{seq}"}
        self.rows.append(row)
        return row


class _Store:
    def __init__(self, *, down: bool = False):
        self.rows: dict[str, list[dict]] = {}
        self.down = down

    def consumed(self):
        return {r["payload"]["token"] for rows in self.rows.values() for r in rows
                if r["kind"] == effect_log.APPROVAL_CONSUMED}

    def kinds(self, session_id="s1"):
        return [r["kind"] for r in self.rows.get(session_id, [])]

    @contextmanager
    def transaction(self, session_id):
        if self.down:
            raise effect_log.EffectLogUnavailable("could not connect: connection refused")
        txn = _Txn(self, session_id)
        yield txn
        self.rows[session_id] = txn.rows        # commit only on a clean exit


# ── Harness ───────────────────────────────────────────────────────────────────


def _cfg(*, bypass: bool, session="s1", rp="turn:1", role="admin"):
    return {"configurable": {"thread_id": session, "user_role": role, "hitl_bypass": bypass,
                             "effect_rollback_point": rp}}


class _Cluster:
    """Stands in for subprocess.run; counts the mutations that actually reached kubectl."""

    def __init__(self):
        self.mutations: list[list[str]] = []

    def __call__(self, args, **_kw):
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        if "get" in args:                          # rollback-point pre-state capture
            proc.stdout = "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: x\n"
        else:
            self.mutations.append(list(args))
            name = next((a.split("/")[-1] for a in args[3:] if not a.startswith("-")), "?")
            proc.stdout = f'persistentvolumeclaim "{name}" deleted'
        return proc


def _approve_with_token(value):
    return {"approved": True, "approval_token": value["approval_token"]}


def _run(command, cfg, cluster, *, guard_interrupt=None, legacy_interrupt=None):
    from app.tools.kubectl_tool import run_kubectl
    gi = guard_interrupt or MagicMock(side_effect=AssertionError("guard prompted unexpectedly"))
    li = legacy_interrupt or MagicMock(side_effect=AssertionError("legacy prompt reached"))
    with patch("app.tools.effect_guard.interrupt", gi), \
         patch("app.tools.kubectl_tool.interrupt", li), \
         patch("subprocess.run", side_effect=cluster):
        return run_kubectl.invoke({"command": command}, config=cfg)


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", True)
    monkeypatch.setattr(effect_log, "get_store", lambda: s)
    return s


# ── Flag off: exactly today's behaviour ───────────────────────────────────────


class TestFlagOffIsUnchanged:
    def test_irreversible_call_takes_the_legacy_path_and_never_touches_the_ledger(self, monkeypatch):
        monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", False)
        monkeypatch.setattr(effect_log, "get_store",
                            MagicMock(side_effect=AssertionError("ledger consulted with flag off")))
        cluster = _Cluster()
        seen = {}
        legacy = MagicMock(side_effect=lambda v: seen.update(v) or True)
        out = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=False), cluster,
                   legacy_interrupt=legacy)
        legacy.assert_called_once()
        assert "approval_token" not in seen and "effect_guard" not in seen
        assert out == 'persistentvolumeclaim "data-a" deleted'
        # and a second identical call executes again, exactly as before ADR-008
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=False), cluster,
             legacy_interrupt=MagicMock(return_value=True))
        assert len(cluster.mutations) == 2

    def test_admit_is_pass_and_settle_is_identity(self, monkeypatch):
        monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", False)
        adm = effect_guard.admit("kubectl delete pvc x", ["kubectl", "delete", "pvc", "x"], None,
                                 _cfg(bypass=True), has_dry_run=False, hitl_bypass=True,
                                 always_confirm=False)
        assert adm is effect_guard.PASS
        assert effect_guard.settle(adm, "out") == "out"

    def test_workflow_helpers_leave_config_and_resume_alone(self, monkeypatch):
        monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", False)
        cfg = {"configurable": {"thread_id": "s"}}
        effect_guard.stamp_rollback_point(cfg, {"messages": [SimpleNamespace(id=None)]})
        assert cfg == {"configurable": {"thread_id": "s"}}
        state = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(
            value={"type": "hitl", "approval_token": "apv-x"})])])
        assert effect_guard.resume_value(True, state) is True


# ── Equivalent retry → recorded result, no second execution ──────────────────


class TestEquivalentRetryReplays:
    def test_semantically_equivalent_call_returns_the_recorded_result(self, store):
        cluster = _Cluster()
        first = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster)
        assert first == 'persistentvolumeclaim "data-a" deleted'
        assert len(cluster.mutations) == 1

        # Same canonical target, different spelling and volatile flags.
        again = _run("kubectl -n prod delete persistentvolumeclaims/data-a --wait=false",
                     _cfg(bypass=True), cluster)
        assert len(cluster.mutations) == 1, "an irreversible call executed twice"
        assert again.startswith("[Replayed — not re-executed]")
        assert 'persistentvolumeclaim "data-a" deleted' in again

    def test_a_new_turn_is_a_new_rollback_point(self, store):
        cluster = _Cluster()
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True, rp="turn:1"), cluster)
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True, rp="turn:2"), cluster)
        assert len(cluster.mutations) == 2

    def test_the_ledger_is_a_verifiable_hash_chain(self, store):
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), _Cluster())
        rows = store.rows["s1"]
        assert store.kinds() == ["approval_issued", "approval_consumed", "intent", "effect"]
        assert effect_log.verify(rows)
        rows[3]["payload"]["result"] = "nothing happened"
        assert not effect_log.verify(rows)


# ── Different target → block, surface prior record, require explicit fork ────


class TestDifferentTargetRequiresFork:
    def test_drifted_retry_is_blocked_and_the_prior_record_surfaced(self, store):
        cluster = _Cluster()
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster)
        seen = {}
        deny = MagicMock(side_effect=lambda v: seen.update(v) or False)
        out = _run("kubectl delete pvc data-b -n prod", _cfg(bypass=True), cluster,
                   guard_interrupt=deny)
        deny.assert_called_once()                  # even on an auto-approve session
        assert seen["effect_guard"]["purpose"] == "fork"
        assert "data-a" in seen["effect_guard"]["prior"]
        assert out.startswith("[Blocked]") and "data-a" in out
        assert len(cluster.mutations) == 1

    def test_an_approved_fork_records_a_new_branch_and_runs(self, store):
        cluster = _Cluster()
        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster)
        out = _run("kubectl delete pvc data-b -n prod", _cfg(bypass=True), cluster,
                   guard_interrupt=MagicMock(side_effect=_approve_with_token))
        assert out == 'persistentvolumeclaim "data-b" deleted'
        assert len(cluster.mutations) == 2
        fork = next(r for r in store.rows["s1"] if r["kind"] == "fork")
        assert fork["payload"]["branch_id"] == "main"
        assert fork["payload"]["new_branch_id"].startswith("br-")


# ── Single-use approvals ──────────────────────────────────────────────────────


class TestApprovalsAreSingleUse:
    def test_a_resurrected_approval_is_rejected(self, store):
        cluster = _Cluster()
        captured = {}

        def approve(value):
            captured["first"] = value["approval_token"]
            return _approve_with_token(value)

        _run("kubectl delete pvc data-a -n prod", _cfg(bypass=False), cluster,
             guard_interrupt=MagicMock(side_effect=approve))
        assert len(cluster.mutations) == 1

        # The node re-runs and LangGraph hands the stored resume value — the token the human
        # gave for data-a — to whatever interrupt now sits at that position.
        stale = MagicMock(return_value={"approved": True, "approval_token": captured["first"]})
        out = _run("kubectl delete pvc data-b -n prod", _cfg(bypass=False), cluster,
                   guard_interrupt=stale)
        assert out.startswith("[Rejected]") and "already consumed" in out
        assert len(cluster.mutations) == 1
        assert store.kinds()[-1] == "refused"

    def test_a_bare_true_is_not_an_approval_of_an_irreversible_call(self, store):
        cluster = _Cluster()
        out = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=False), cluster,
                   guard_interrupt=MagicMock(return_value=True))
        assert out.startswith("[Rejected]") and "no approval token" in out
        assert cluster.mutations == []

    def test_the_ledger_refuses_a_second_consumption(self, store):
        with store.transaction("s1") as txn:
            txn.append(effect_log.APPROVAL_CONSUMED, {"token": "apv-1"})
        with pytest.raises(effect_log.EffectLogUnavailable):
            with store.transaction("s1") as txn:
                txn.append(effect_log.APPROVAL_CONSUMED, {"token": "apv-1"})

    def test_an_attempt_with_no_recorded_outcome_goes_back_to_a_human(self, store):
        # Admitted (intent claimed) but the effect was never recorded — a crash mid-call.
        adm = effect_guard.admit("kubectl delete pvc data-a -n prod",
                                 ["kubectl", "delete", "pvc", "data-a", "-n", "prod"], None,
                                 _cfg(bypass=True), has_dry_run=False, hitl_bypass=True,
                                 always_confirm=False)
        assert adm.approved and adm.ticket is not None
        cluster = _Cluster()
        seen = {}
        out = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster,
                   guard_interrupt=MagicMock(side_effect=lambda v: seen.update(v) or False))
        assert seen["effect_guard"]["purpose"] == "reapprove"
        assert out == "Action cancelled by user."
        assert cluster.mutations == []


# ── Fail-closed when the ledger is unavailable ───────────────────────────────


class TestUnavailableLedgerFallsBackToApproval:
    @pytest.mark.parametrize("store_factory", [lambda: None, lambda: _Store(down=True)])
    def test_auto_approve_does_not_execute_without_a_ledger(self, monkeypatch, store_factory):
        monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", True)
        s = store_factory()
        monkeypatch.setattr(effect_log, "get_store", lambda: s)
        cluster = _Cluster()
        seen = {}
        out = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster,
                   guard_interrupt=MagicMock(side_effect=lambda v: seen.update(v) or False))
        assert seen["effect_guard"]["effect_log"] == "unavailable"
        assert out == "Action cancelled by user."
        assert cluster.mutations == []

    def test_an_explicit_re_approval_runs_it_once(self, monkeypatch):
        monkeypatch.setattr(settings, "SELF_GOVERN_ENABLED", True)
        monkeypatch.setattr(effect_log, "get_store", lambda: _Store(down=True))
        cluster = _Cluster()
        out = _run("kubectl delete pvc data-a -n prod", _cfg(bypass=True), cluster,
                   guard_interrupt=MagicMock(side_effect=_approve_with_token))
        assert out == 'persistentvolumeclaim "data-a" deleted'
        assert len(cluster.mutations) == 1

    def test_no_session_means_no_rollback_point_and_no_execution(self, store):
        cluster = _Cluster()
        out = _run("kubectl delete pvc data-a -n prod",
                   {"configurable": {"user_role": "admin", "hitl_bypass": True}}, cluster)
        assert out.startswith("[Blocked]")
        assert cluster.mutations == []


# ── Scope: only the IRREVERSIBLE class is interposed ─────────────────────────


def test_a_reversible_mutation_is_not_interposed(store):
    cluster = _Cluster()
    _run("kubectl delete pod web-1 -n prod", _cfg(bypass=True), cluster)
    _run("kubectl delete pod web-1 -n prod", _cfg(bypass=True), cluster)
    assert len(cluster.mutations) == 2
    assert store.rows == {}


# ── Canonicalization ──────────────────────────────────────────────────────────


class TestCanonicalization:
    def _key(self, *args, stdin=None):
        return effect_guard.canonical_key(effect_guard.canonical_intent(list(args), stdin))

    def test_spelling_and_execution_modifiers_do_not_matter(self):
        assert self._key("kubectl", "delete", "pvc", "a", "-n", "prod") == self._key(
            "kubectl", "--namespace=prod", "delete", "persistentvolumeclaims/a",
            "--grace-period", "0", "--wait=false", "-o", "name")

    def test_target_namespace_and_replicas_are_intent(self):
        base = self._key("kubectl", "delete", "pvc", "a", "-n", "prod")
        assert base != self._key("kubectl", "delete", "pvc", "b", "-n", "prod")
        assert base != self._key("kubectl", "delete", "pvc", "a", "-n", "dev")
        assert self._key("kubectl", "scale", "sts/db", "--replicas=3") != \
               self._key("kubectl", "scale", "sts/db", "--replicas=0")

    def test_request_ids_and_timestamps_are_not_intent(self):
        assert self._key("kubectl", "annotate", "pvc", "a", "owner=me", "request-id=1") == \
               self._key("kubectl", "annotate", "pvc", "a", "owner=me", "request-id=2")
        m1 = ("kind: PersistentVolumeClaim\nmetadata:\n  name: a\n  namespace: prod\n"
              "  uid: 111\n  resourceVersion: '5'\n  annotations:\n    trace-id: abc\n")
        m2 = ("kind: PersistentVolumeClaim\nmetadata:\n  name: a\n  namespace: prod\n"
              "  uid: 222\n  resourceVersion: '9'\n  annotations:\n    trace-id: xyz\n")
        assert self._key("kubectl", "delete", "-f", "-", stdin=m1) == \
               self._key("kubectl", "delete", "-f", "-", stdin=m2)

    def test_a_manifest_delete_is_classified_by_the_kinds_it_names(self):
        m = "kind: Namespace\nmetadata:\n  name: shop\n"
        assert effect_guard.is_irreversible(effect_guard.canonical_intent(
            ["kubectl", "delete", "-f", "-"], m))
