"""Two-axis capability sandbox (v5 P3) — SA impersonation flag building."""
from __future__ import annotations

from app.core.config import settings
from app.tools.aci.sandbox import (
    NAMESPACE_WRITE,
    NEVER_ADMIN,
    READ_ONLY,
    as_impersonated,
    impersonation_args,
    run_as,
    sa_for_role,
)


class TestSaForRole:
    def test_readonly_and_never_admin_share_sa(self):
        assert sa_for_role(READ_ONLY) == settings.KI_V5_SANDBOX_READONLY_SA
        assert sa_for_role(NEVER_ADMIN) == settings.KI_V5_SANDBOX_READONLY_SA

    def test_writer_sa(self):
        assert sa_for_role(NAMESPACE_WRITE) == settings.KI_V5_SANDBOX_WRITER_SA


class TestImpersonationArgs:
    def test_read_only(self):
        args = impersonation_args(READ_ONLY)
        assert args == f"--as=system:serviceaccount:{settings.KI_V5_SANDBOX_SA_NAMESPACE}:{settings.KI_V5_SANDBOX_READONLY_SA}"

    def test_namespace_write(self):
        assert settings.KI_V5_SANDBOX_WRITER_SA in impersonation_args(NAMESPACE_WRITE)

    def test_unknown_role_empty_failclosed(self):
        assert impersonation_args("root") == ""


class TestAsImpersonated:
    def test_appends_flags(self):
        out = as_impersonated("kubectl get pods -n demo", READ_ONLY)
        assert out.startswith("kubectl get pods -n demo --as=system:serviceaccount:")

    def test_unknown_role_is_noop(self):
        assert as_impersonated("kubectl get pods", "root") == "kubectl get pods"


class TestRunAs:
    def test_runs_with_impersonation(self):
        seen = {}
        run_as("kubectl get pods -n demo", READ_ONLY, _runner=lambda c: seen.setdefault("cmd", c) or "ok")
        assert "--as=system:serviceaccount:" in seen["cmd"]
