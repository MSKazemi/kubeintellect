"""Every trace this server emitted said `service.name = unknown_service`.

Langfuse v4 traces through OpenTelemetry. An OTel resource with nothing set reports
`unknown_service` and a `service.instance.id` that is a fresh UUID per process — so a shared
Langfuse project collecting from several clusters holds traces that cannot be attributed to any
of them.

That failed concretely: a multi-cluster campaign attributed traces to a run by time window and
got a different lane's traces, because two lanes overlapped in wall-clock time on separate
clusters and both wrote to one project. The counts looked entirely plausible.
"""
from __future__ import annotations

import pytest

from app.core import llm as llm_mod
from app.core.config import settings


@pytest.fixture(autouse=True)
def _clean_otel_env(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)


def _attrs() -> dict[str, str]:
    import os
    raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    return dict(p.split("=", 1) for p in raw.split(",") if "=" in p)


def test_the_service_name_is_no_longer_unknown(monkeypatch):
    llm_mod._stamp_otel_identity()
    import os
    assert os.environ["OTEL_SERVICE_NAME"] == "kubeintellect"
    assert _attrs()["service.name"] == "kubeintellect"
    assert _attrs()["service.name"] != "unknown_service"


def test_the_cluster_id_is_stamped_so_a_shared_project_can_attribute_a_trace(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ID", "ki-soak-c1", raising=False)
    llm_mod._stamp_otel_identity()
    assert _attrs()["kubeintellect.cluster_id"] == "ki-soak-c1"


def test_two_clusters_writing_to_one_project_are_distinguishable(monkeypatch):
    """The exact failure: overlapping windows, one project, nothing to tell the lanes apart."""
    seen = []
    for cluster in ("ki-chaos-c1", "ki-soak-c1"):
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        monkeypatch.setattr(settings, "CLUSTER_ID", cluster, raising=False)
        llm_mod._stamp_otel_identity()
        seen.append(_attrs()["kubeintellect.cluster_id"])
    assert seen == ["ki-chaos-c1", "ki-soak-c1"]
    assert len(set(seen)) == 2


def test_the_version_is_stamped(monkeypatch):
    monkeypatch.setattr(settings, "KI_VERSION", "v4", raising=False)
    llm_mod._stamp_otel_identity()
    assert _attrs()["service.version"] == "v4"


def test_an_unset_cluster_id_is_omitted_rather_than_stamped_empty(monkeypatch):
    """An empty attribute is worse than an absent one — it reads as a cluster literally named ''."""
    monkeypatch.setattr(settings, "CLUSTER_ID", "", raising=False)
    llm_mod._stamp_otel_identity()
    assert "kubeintellect.cluster_id" not in _attrs()


def test_an_operator_who_set_their_own_values_keeps_them(monkeypatch):
    """Someone pointing this at their own collector means it; this must not overwrite them."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "their-name")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=their-name,deployment=prod")
    monkeypatch.setattr(settings, "CLUSTER_ID", "ki-soak-c1", raising=False)
    llm_mod._stamp_otel_identity()
    import os
    assert os.environ["OTEL_SERVICE_NAME"] == "their-name"
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "service.name=their-name,deployment=prod"


def test_the_identity_is_stamped_before_the_handler_is_built():
    """OTel reads these when it builds its resource, so after construction is too late."""
    import inspect
    src = inspect.getsource(llm_mod.get_langfuse_callbacks)
    lines = [ln.strip() for ln in src.splitlines()]
    stamp = next(i for i, ln in enumerate(lines) if "_stamp_otel_identity()" in ln)
    build = next(i for i, ln in enumerate(lines) if ln == "return [CallbackHandler()]")
    assert stamp < build, "identity must be stamped before the tracer provider is created"


def test_tracing_stays_off_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False, raising=False)
    assert llm_mod.get_langfuse_callbacks() == []
    import os
    assert "OTEL_RESOURCE_ATTRIBUTES" not in os.environ, \
        "a disabled integration must not touch the process environment"
