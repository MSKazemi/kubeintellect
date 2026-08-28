"""The tamper detector was fully built, fully tested — and nothing ever ran it.

`security.verify_memory_chain` is this project's memory tamper-evidence: it recomputes the
hash chain over `memory_audit` and compares its tail against a persisted head anchor, so an
edit to a stored memory row is detectable. Three files of tests cover its verdicts, ADR-018
specifies it, `docs/security.md` documents it, and the P7 flag description in `config.py`
names it. Measured 2026-08-28 with `grep -rn verify_memory_chain v4/`, the callers were:

    tests/…                       four test modules
    scripts/memory_pg_probe.py    an offline operator probe, run by hand
    docs/, design/                prose

and **nothing in a running server**. A hash chain does not accuse anyone on its own; it is a
question that has to be asked. Asked by nobody, it detects nothing, and the difference between
that and having no tamper-evidence at all is documentation. This is the same shape as every
other defect this subsystem has produced — a component reporting nothing while being, in fact,
dead — which is exactly what `liveness.py` was written to end.

So a running server now asks: once at startup, then on `MEMORY_CHAIN_VERIFY_INTERVAL_S`, with
the verdict recorded and reported on `/healthz`. The claims proved here are:

* the verifier is actually started, by the code path that starts the other memory workers;
* a tampered chain reaches `/healthz` as a symptom, so `healthy` goes false;
* an unreachable database does **not** — `unverified` is not `tampered`, and a detector that
  confuses the two teaches operators to ignore it;
* the surface distinguishes *off*, *never checked*, *unverified*, *intact* and *TAMPERED*,
  because none of those four is "fine" and a boolean would call three of them one thing;
* a verdict that has stopped being refreshed is reported as stale rather than as current —
  a stopped verifier otherwise looks exactly like one that keeps agreeing with itself.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.db.flight_recorder import ChainVerdict
from app.memory import liveness, security, service

V4 = Path(__file__).resolve().parents[1]


def chain(n: int, cluster_id: str = "c1") -> list[dict]:
    rows, prev = [], ""
    for seq in range(n):
        payload = {"reason": f"e{seq}"}
        h = security._compute_audit_hash(prev, cluster_id, seq, "episode_write", payload)
        rows.append({"seq": seq, "kind": "episode_write", "payload": payload,
                     "prev_hash": prev, "hash": h})
        prev = h
    return rows


class Pool:
    """Rows and anchor answered independently — the same fake the verdict tests use."""

    def __init__(self, rows, head=None, fetch_exc=None):
        self.rows, self.head, self.fetch_exc = rows, head, fetch_exc

    async def fetch(self, *_a):
        if self.fetch_exc:
            raise self.fetch_exc
        return self.rows

    async def fetchrow(self, *_a):
        return self.head


ROWS = chain(3)
HEAD = {"seq": ROWS[-1]["seq"], "hash": ROWS[-1]["hash"]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every counter in `liveness` is process-global, as the running server's are — so a
    recall counter left behind by an earlier test would otherwise supply a symptom of its own
    and make the `symptoms == []` claims below depend on suite order."""
    liveness.reset()
    liveness.reset_chain_state()
    monkeypatch.setattr("app.cluster_id.get_cluster_id", lambda: "c1")
    yield
    liveness.reset()
    liveness.reset_chain_state()


@pytest.fixture
def pooled(monkeypatch):
    def _set(pool):
        monkeypatch.setattr(service, "_pool", pool, raising=False)
    return _set


# ── 1. it is started at all — the whole point of the change ───────────────────────────────


class TestSomethingInARunningServerAsks:
    async def _activate(self, monkeypatch, *, hardening: bool, interval: int = 900):
        """Run the real activation path against a fake pool, then read what it started.

        `_activate` is the function `init_memory` and the reconnect loop both finish startup
        with — testing it rather than a source string is what makes this a wiring claim.
        """
        monkeypatch.setattr(settings, "MEMORY_SECURITY_HARDENING", hardening)
        monkeypatch.setattr(settings, "MEMORY_CHAIN_VERIFY_INTERVAL_S", interval)
        for mod in ("episodes", "kg", "preferences"):
            monkeypatch.setattr(getattr(service, mod), f"init_{mod}", lambda _p: None)
        service._activate(Pool(ROWS, HEAD))
        started = {t.get_coro().__qualname__ for t in service._tasks}
        for task in service._tasks:      # nothing is awaited, so nothing has run yet
            task.cancel()
        service._tasks.clear()
        return started

    async def test_the_verifier_is_started_alongside_the_other_memory_workers(
        self, monkeypatch
    ):
        started = await self._activate(monkeypatch, hardening=True)
        assert "_verify_chain_loop" in started, started

    async def test_it_is_not_started_when_nothing_writes_the_chain(self, monkeypatch):
        """Hardening off means no rows are appended. Verifying then reports `intact` about a
        chain nothing writes to — a green light for a feature that is not running."""
        started = await self._activate(monkeypatch, hardening=False)
        assert "_verify_chain_loop" not in started
        assert "_drain_observations" in started, "the rest of activation must be unaffected"

    async def test_a_negative_interval_turns_it_off_entirely(self, monkeypatch):
        started = await self._activate(monkeypatch, hardening=True, interval=-1)
        assert "_verify_chain_loop" not in started

    async def test_zero_keeps_the_startup_pass_and_drops_the_schedule(
        self, monkeypatch, pooled
    ):
        """A process coming up against a database edited while it was down should say so."""
        monkeypatch.setattr(settings, "MEMORY_CHAIN_VERIFY_INTERVAL_S", 0)
        pooled(Pool(ROWS, HEAD))
        await asyncio.wait_for(service._verify_chain_loop(), timeout=5)
        assert liveness.chain_status(enabled=True)["checks"] == 1

    def test_exactly_one_production_module_calls_the_verifier(self):
        """Pins the fix. Before 2026-08-28 this set was EMPTY, which is the whole defect —
        so a future refactor that drops the call site fails here rather than silently
        restoring a tamper detector nobody ever asks."""
        callers = {
            p.relative_to(V4).as_posix()
            for p in (V4 / "packages").rglob("*.py")
            if re.search(r"(?<!def )verify_memory_chain\s*\(",
                         p.read_text(encoding="utf-8"))
        }
        assert callers == {"packages/kubeintellect-server/app/memory/service.py"}, callers


# ── 2. a finding reaches the operator ─────────────────────────────────────────────────────


class TestATamperedChainIsReported:
    async def test_the_verdict_is_recorded(self, pooled):
        pooled(Pool(ROWS, HEAD))
        await service.verify_chain_once()
        st = liveness.chain_status(enabled=True)
        assert (st["state"], st["valid"], st["verified"]) == ("intact", True, True)

    async def test_a_mutated_row_is_TAMPERED(self, pooled):
        bad = [dict(r) for r in ROWS]
        bad[1]["payload"] = {"reason": "edited after the fact"}
        pooled(Pool(bad, HEAD))
        await service.verify_chain_once()
        assert liveness.chain_status(enabled=True)["state"] == "TAMPERED"

    async def test_a_truncated_chain_is_TAMPERED(self, pooled):
        """Every link in a truncated chain still verifies. Only the anchor contradicts it."""
        pooled(Pool(ROWS[:2], HEAD))
        await service.verify_chain_once()
        assert liveness.chain_status(enabled=True)["state"] == "TAMPERED"

    async def test_it_becomes_a_symptom_so_healthy_goes_false(self, pooled, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SECURITY_HARDENING", True)
        monkeypatch.setattr(service, "_state", "ready", raising=False)
        pooled(Pool(ROWS[:2], HEAD))
        await service.verify_chain_once()
        status = service.memory_status()
        assert status["chain"]["state"] == "TAMPERED"
        assert any("audit chain does not verify" in s for s in status["symptoms"])
        assert status["healthy"] is False
        assert status["enabled"] is True, "the pool is fine — that is why this had to be said"


# ── 3. a failure to look is not a finding ─────────────────────────────────────────────────


class TestNotLookingIsNotAnAccusation:
    async def test_an_unreachable_database_is_unverified_not_tampered(self, pooled):
        pooled(Pool(ROWS, HEAD, fetch_exc=OSError("connection refused")))
        await service.verify_chain_once()
        st = liveness.chain_status(enabled=True)
        assert st["state"] == "unverified"
        assert st["valid"] is True and st["verified"] is False

    async def test_unverified_is_not_a_symptom(self, pooled, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SECURITY_HARDENING", True)
        monkeypatch.setattr(service, "_state", "ready", raising=False)
        pooled(Pool(ROWS, HEAD, fetch_exc=OSError("connection refused")))
        await service.verify_chain_once()
        status = service.memory_status()
        assert status["symptoms"] == []
        assert status["healthy"] is True, "nobody looked is not evidence of anything"

    async def test_no_pool_is_unverified(self, pooled):
        pooled(None)
        await service.verify_chain_once()
        assert liveness.chain_status(enabled=True)["state"] == "unverified"

    async def test_a_raising_verifier_records_unverified_and_does_not_escape(
        self, pooled, monkeypatch
    ):
        """This runs in the task group that owns the observation drain and consolidation."""
        async def boom(*_a, **_k):
            raise RuntimeError("schema drift")
        monkeypatch.setattr(security, "verify_memory_chain", boom)
        pooled(Pool(ROWS, HEAD))
        await service.verify_chain_once()          # must not raise
        st = liveness.chain_status(enabled=True)
        assert (st["state"], st["checks"]) == ("unverified", 1)

    async def test_the_recorded_verdict_is_whatever_the_verifier_returned(
        self, pooled, monkeypatch
    ):
        async def verdict(*_a, **_k):
            return ChainVerdict(False, True)
        monkeypatch.setattr(security, "verify_memory_chain", verdict)
        pooled(Pool(ROWS, HEAD))
        await service.verify_chain_once()
        assert liveness.chain_status(enabled=True)["state"] == "TAMPERED"


# ── 4. five states, because none of the other four means "fine" ───────────────────────────


class TestTheSurfaceSaysWhichKindOfSilenceItIs:
    def test_off_is_not_a_clean_bill_of_health(self):
        assert liveness.chain_status(enabled=False)["state"] == "off"

    def test_off_wins_even_after_a_check_ran(self):
        """Turning hardening off must not leave a stale `intact` standing as current."""
        liveness.record_chain_check(valid=True, verified=True, at=100.0)
        assert liveness.chain_status(enabled=False)["state"] == "off"

    def test_never_checked_is_distinct_from_intact(self):
        st = liveness.chain_status(enabled=True)
        assert st["state"] == "never-checked"
        assert st["checks"] == 0 and st["checked_at"] is None

    def test_the_five_states_are_all_reachable_and_distinct(self):
        seen = {liveness.chain_status(enabled=False)["state"],
                liveness.chain_status(enabled=True)["state"]}
        for valid, verified in ((True, True), (True, False), (False, True)):
            liveness.reset_chain_state()
            liveness.record_chain_check(valid=valid, verified=verified, at=1.0)
            seen.add(liveness.chain_status(enabled=True)["state"])
        assert seen == {"off", "never-checked", "intact", "unverified", "TAMPERED"}

    def test_the_check_count_is_reported(self):
        for i in range(3):
            liveness.record_chain_check(valid=True, verified=True, at=float(i))
        st = liveness.chain_status(enabled=True)
        assert st["checks"] == 3 and st["checked_at"] == 2.0


class TestAStoppedVerifierDoesNotLookLikeAnAgreeingOne:
    def test_a_fresh_verdict_is_not_stale(self):
        liveness.record_chain_check(valid=True, verified=True, at=1_000.0)
        st = liveness.chain_status(enabled=True, now=1_060.0, stale_after_s=900.0)
        assert st["stale"] is False and st["age_s"] == 60.0

    def test_an_old_verdict_is_stale_and_says_how_old(self):
        liveness.record_chain_check(valid=True, verified=True, at=1_000.0)
        st = liveness.chain_status(enabled=True, now=9_000.0, stale_after_s=900.0)
        assert st["stale"] is True and st["age_s"] == 8_000.0

    def test_staleness_is_a_symptom(self):
        stale = {"state": "intact", "stale": True, "age_s": 8_000.0}
        assert any("has not been verified" in s
                   for s in liveness.symptoms(state="ready", chain=stale))

    def test_the_window_is_wider_than_one_interval(self, monkeypatch):
        """One slow or skipped pass is not an alarm; a verifier that stopped is."""
        monkeypatch.setattr(settings, "MEMORY_CHAIN_VERIFY_INTERVAL_S", 900)
        assert service._chain_stale_after_s() > 900

    def test_nothing_is_stale_when_the_schedule_is_off(self, monkeypatch):
        """A single startup verdict is the only one there will be — by the operator's choice."""
        monkeypatch.setattr(settings, "MEMORY_CHAIN_VERIFY_INTERVAL_S", 0)
        assert service._chain_stale_after_s() is None
        liveness.record_chain_check(valid=True, verified=True, at=1.0)
        st = liveness.chain_status(enabled=True, now=1e9, stale_after_s=None)
        assert st["stale"] is False

    def test_the_default_interval_is_not_the_probe_interval(self):
        """Verifying reads every audit row for the cluster; /healthz is probed every few
        seconds. Re-deriving on the probe path would make its latency a function of history."""
        assert settings.MEMORY_CHAIN_VERIFY_INTERVAL_S >= 60


# ── 5. the surfaces that carry it ─────────────────────────────────────────────────────────


class TestItIsWrittenDownWhereAnOperatorLooks:
    def test_healthz_carries_the_chain_block(self, monkeypatch):
        monkeypatch.setattr(service, "_state", "ready", raising=False)
        assert "chain" in service.memory_status()

    def test_the_setting_is_documented(self):
        text = (V4 / "docs" / "configuration.md").read_text(encoding="utf-8")
        assert "MEMORY_CHAIN_VERIFY_INTERVAL_S" in text

    def test_the_security_doc_no_longer_describes_an_unrun_verifier(self):
        text = (V4 / "docs" / "security.md").read_text(encoding="utf-8")
        assert "MEMORY_CHAIN_VERIFY_INTERVAL_S" in text
        assert re.search(r"chain[^\n]*/healthz|/healthz[^\n]*chain", text)
