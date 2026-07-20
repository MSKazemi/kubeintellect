"""Investigation write-back (v5 P2) — signal derivation + reconcile application."""
from __future__ import annotations

from app.memory.writeback import EdgeSignal, apply_writeback, signals_from_investigation


class TestSignals:
    def test_one_confirm_edge_per_playbook(self):
        sigs = signals_from_investigation("cl-1", ["OOMKilled", "CrashLoopBackOff"])
        assert len(sigs) == 2
        assert all(s.src == "cl-1" and s.rel == "exhibits" and s.verdict == "confirm" for s in sigs)
        assert {s.dst for s in sigs} == {"OOMKilled", "CrashLoopBackOff"}

    def test_dedupes_and_skips_blank(self):
        sigs = signals_from_investigation("cl-1", ["OOMKilled", "OOMKilled", "", "  "])
        assert [s.dst for s in sigs] == ["OOMKilled"]

    def test_no_playbooks_no_signals(self):
        assert signals_from_investigation("cl-1", []) == []


class TestApplyWriteback:
    async def test_reconciles_each_signal_with_confirm_attr(self):
        calls = []
        async def fake_reconcile(cluster_id, src, rel, dst, attrs, **kw):
            calls.append((cluster_id, src, rel, dst, attrs, kw))
            return "ADD"
        sigs = signals_from_investigation("cl-1", ["OOMKilled", "Evicted"])
        tally = await apply_writeback("cl-1", sigs, reconcile=fake_reconcile)
        assert tally == {"ADD": 2}
        assert all(c[4].get("investigation_confirmed") is True for c in calls)
        assert all(c[5].get("source_kind") == "investigation" for c in calls)

    async def test_contradict_sets_counter_attr(self):
        captured = {}
        async def fake_reconcile(cluster_id, src, rel, dst, attrs, **kw):
            captured.update(attrs)
            return "UPDATE"
        sig = EdgeSignal(src="cl-1", rel="exhibits", dst="X", verdict="contradict")
        await apply_writeback("cl-1", [sig], reconcile=fake_reconcile)
        assert captured.get("investigation_contradicted") is True
        assert "investigation_confirmed" not in captured

    async def test_tally_counts_decisions(self):
        seq = iter(["ADD", "UPDATE", "NOOP"])
        async def fake_reconcile(*a, **k):
            return next(seq)
        sigs = signals_from_investigation("cl-1", ["a", "b", "c"])
        tally = await apply_writeback("cl-1", sigs, reconcile=fake_reconcile)
        assert tally == {"ADD": 1, "UPDATE": 1, "NOOP": 1}

    async def test_reconcile_exception_tallied_not_raised(self):
        async def boom(*a, **k):
            raise RuntimeError("db gone")
        tally = await apply_writeback("cl-1", signals_from_investigation("cl-1", ["a"]), reconcile=boom)
        assert tally == {"ERROR": 1}

    async def test_empty_signals_noop(self):
        assert await apply_writeback("cl-1", []) == {}
