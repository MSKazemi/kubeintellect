"""Fleet memory exchange (v5 P5, ADR-105) — cross-cluster sharing with tenant isolation."""
from __future__ import annotations

import pytest
from app.memory.fleet_exchange import FleetEntry, _clear, publish, read_fleet, tenants


@pytest.fixture(autouse=True)
def clean():
    _clear()
    yield
    _clear()


def _e(tenant, cluster, sig="OOMKilled|payments", summary="raise memory limit"):
    return FleetEntry(tenant=tenant, cluster_id=cluster, signature=sig, summary=summary)


class TestPublishRead:
    def test_publish_and_read(self):
        publish(_e("acme", "cl-1"))
        got = read_fleet("acme")
        assert len(got) == 1 and got[0].cluster_id == "cl-1"

    def test_exclude_own_cluster(self):
        publish(_e("acme", "cl-1"))
        publish(_e("acme", "cl-2"))
        got = read_fleet("acme", exclude_cluster="cl-1")
        assert [e.cluster_id for e in got] == ["cl-2"]

    def test_signature_filter(self):
        publish(_e("acme", "cl-1", sig="OOMKilled|payments"))
        publish(_e("acme", "cl-2", sig="CrashLoop|web"))
        got = read_fleet("acme", signature="CrashLoop|web")
        assert len(got) == 1 and got[0].cluster_id == "cl-2"


class TestTenantIsolation:
    def test_read_never_crosses_tenants(self):
        publish(_e("acme", "cl-1"))
        publish(_e("globex", "cl-9"))
        acme = read_fleet("acme")
        globex = read_fleet("globex")
        assert [e.cluster_id for e in acme] == ["cl-1"]
        assert [e.cluster_id for e in globex] == ["cl-9"]
        # the security-critical property: acme never sees globex data
        assert all(e.tenant == "acme" for e in acme)
        assert all(e.tenant == "globex" for e in globex)

    def test_unknown_tenant_empty(self):
        publish(_e("acme", "cl-1"))
        assert read_fleet("nobody") == []

    def test_tenants_listed(self):
        publish(_e("acme", "cl-1"))
        publish(_e("globex", "cl-9"))
        assert tenants() == ["acme", "globex"]


class TestBounded:
    def test_ring_buffer_per_tenant(self):
        for i in range(600):
            publish(_e("acme", f"cl-{i}"))
        assert len(read_fleet("acme")) == 500     # _MAX_PER_TENANT
