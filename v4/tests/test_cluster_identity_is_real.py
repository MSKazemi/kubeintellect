"""Cluster identity must be real, or visibly not.

`cluster_id.py` opens by saying why it exists: *"Without this, patterns from a Kind dev cluster
would pollute prompts on prod EKS and vice versa."* Memory, findings, learned patterns and
episodes are all scoped by the id it returns.

Two of its three strategies shell out to `kubectl config`, which needs a kubeconfig **file**. An
in-cluster deployment has none — the chart sets `KUBECONFIG_PATH: ""` so kubectl authenticates
with the pod's ServiceAccount — and both commands then exit 1 with empty stdout. Verified against
the real binary (`bitnami/kubectl:latest`, no kubeconfig):

    $ kubectl config current-context
    error: current-context is not set                                    (exit 1)
    $ kubectl config view --minify -o jsonpath={.clusters[0].cluster.server}
    error: current-context must exist in order to minify                 (exit 1)

So every Helm-deployed instance resolved to the `"unknown"` sentinel, and the per-cluster scoping
was inert in exactly the deployment mode it was written for. It looked correct in development,
where a kubeconfig is present — which is why the tests below drive the *in-cluster* shape, not the
laptop shape.

The sentinel itself is not a bug and is not filtered on read: in a single-cluster deployment every
row is sentinel-scoped and is legitimately that cluster's own data. The fix is to make identity
resolvable (`CLUSTER_ID`, and a namespace-UID fallback), not to discard rows.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from app.cluster_id import (
    UNRESOLVED_CLUSTER_ID,
    cluster_id_is_resolved,
    get_cluster_id,
    reset_cluster_id_cache,
)
from app.core.config import settings

# Exactly what real kubectl does with no kubeconfig — see the module docstring.
_NO_CONTEXT = subprocess.CompletedProcess([], 1, "", "error: current-context is not set\n")
_NO_MINIFY = subprocess.CompletedProcess(
    [], 1, "", "error: current-context must exist in order to minify\n")
_NO_RBAC = subprocess.CompletedProcess(
    [], 1, "", 'Error from server (Forbidden): namespaces "kube-system" is forbidden\n')


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fake_kubectl(*, context=_NO_CONTEXT, view=_NO_MINIFY, uid=_NO_RBAC):
    """Dispatch on the kubectl subcommand, the way the real calls differ."""
    def run(args, **_kwargs):
        sub = tuple(args[1:3])
        if sub == ("config", "current-context"):
            return context
        if sub == ("config", "view"):
            return view
        if sub == ("get", "namespace"):
            return uid
        raise AssertionError(f"unexpected kubectl call: {args}")
    return run


@pytest.fixture(autouse=True)
def _no_cache_bleed():
    reset_cluster_id_cache()
    yield
    reset_cluster_id_cache()


@pytest.fixture(autouse=True)
def _no_ambient_override(mocker):
    """A local .env with CLUSTER_ID set must not silently satisfy these tests."""
    mocker.patch.object(settings, "CLUSTER_ID", "")


class TestTheInClusterDeployment:
    """The mode the chart actually ships, and the one that was broken."""

    def test_a_namespace_uid_identifies_the_cluster_when_kubeconfig_is_absent(self):
        with patch("subprocess.run",
                   side_effect=_fake_kubectl(uid=_ok("f3c1a9de-77b2-4c1e-9a01-2b8e5d6f0a11"))):
            assert get_cluster_id() == "uid:f3c1a9de-77b"

    def test_without_that_permission_it_is_the_sentinel_not_a_guess(self):
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            assert get_cluster_id() == UNRESOLVED_CLUSTER_ID

    def test_the_fallback_warns_and_says_what_it_costs(self, caplog):
        with caplog.at_level("WARNING"), patch("subprocess.run", side_effect=_fake_kubectl()):
            get_cluster_id()
        msg = caplog.text
        assert "CLUSTER_ID" in msg, "the warning must name the fix"
        assert "share one scope" in msg, "the warning must name the consequence"


class TestTheExplicitOverride:
    def test_it_wins_over_every_probe(self, mocker):
        mocker.patch.object(settings, "CLUSTER_ID", "prod-eks-eu")
        with patch("subprocess.run", side_effect=_fake_kubectl(context=_ok("kind-local"))):
            assert get_cluster_id() == "prod-eks-eu"

    def test_it_works_where_nothing_else_can(self, mocker):
        """The in-cluster case: no kubeconfig, no cluster-scoped read."""
        mocker.patch.object(settings, "CLUSTER_ID", "prod-eks-eu")
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            assert get_cluster_id() == "prod-eks-eu"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_override_is_not_an_identity(self, mocker, blank):
        mocker.patch.object(settings, "CLUSTER_ID", blank)
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            assert get_cluster_id() == UNRESOLVED_CLUSTER_ID

    def test_it_is_stripped(self, mocker):
        mocker.patch.object(settings, "CLUSTER_ID", "  prod-eks-eu  ")
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            assert get_cluster_id() == "prod-eks-eu"


class TestTheExistingStrategiesStillWork:
    """The laptop path must not regress — it is the one that always worked."""

    def test_context_and_server_compose(self):
        with patch("subprocess.run", side_effect=_fake_kubectl(
                context=_ok("kind-kubeintellect"), view=_ok("https://127.0.0.1:6443"))):
            cid = get_cluster_id()
        assert cid.startswith("kind-kubeintellect:") and len(cid.split(":")[1]) == 8

    def test_context_alone(self):
        with patch("subprocess.run", side_effect=_fake_kubectl(context=_ok("ctx-only"))):
            assert get_cluster_id() == "ctx-only"

    def test_server_alone(self):
        with patch("subprocess.run",
                   side_effect=_fake_kubectl(view=_ok("https://prod.example:6443"))):
            assert get_cluster_id().startswith("server:")

    def test_the_same_server_always_gives_the_same_id(self):
        ids = set()
        for _ in range(3):
            reset_cluster_id_cache()
            with patch("subprocess.run",
                       side_effect=_fake_kubectl(view=_ok("https://prod.example:6443"))):
                ids.add(get_cluster_id())
        assert len(ids) == 1, ids

    def test_different_servers_give_different_ids(self):
        got = []
        for server in ("https://a.example:6443", "https://b.example:6443"):
            reset_cluster_id_cache()
            with patch("subprocess.run", side_effect=_fake_kubectl(view=_ok(server))):
                got.append(get_cluster_id())
        assert got[0] != got[1]

    def test_the_server_url_is_not_leaked_in_the_id(self):
        with patch("subprocess.run",
                   side_effect=_fake_kubectl(view=_ok("https://internal-host.corp:6443"))):
            assert "internal-host" not in get_cluster_id()


class TestTheSentinelIsNotSilentlyCoveredByExistingGuards:
    def test_the_helper_agrees_with_the_sentinel(self):
        assert not cluster_id_is_resolved(UNRESOLVED_CLUSTER_ID)
        assert not cluster_id_is_resolved("")
        assert cluster_id_is_resolved("prod-eks-eu")

    def test_the_function_never_returns_the_empty_string(self):
        """The SQL guards elsewhere exclude `cluster_id <> ''`, which this can never produce.

        Pinning it: those guards do not, and never did, cover an unresolved identity — so nobody
        should read them as protection against it.
        """
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            assert get_cluster_id() != ""


class TestTheChartLetsAnOperatorSetIt:
    """A fix an operator cannot reach is not a fix."""

    def test_the_configmap_exposes_cluster_id(self):
        from pathlib import Path
        cm = (Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"
              / "templates" / "configmap.yaml").read_text()
        assert "CLUSTER_ID:" in cm and ".Values.config.clusterId" in cm

    def test_values_yaml_documents_the_key(self):
        import yaml
        from pathlib import Path
        v = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy" / "helm"
                            / "kubeintellect" / "values.yaml").read_text())
        assert v["config"]["clusterId"] == ""


class TestTheSentinelIsAWildcardNotAnInertPlaceholder:
    """The sentinel is *read back* by every cluster — which is what makes this matter.

    `memory_store` recalls with `cluster_id IN ($1, 'unknown')`, deliberately: rows written
    before the column existed should still match anywhere. But that also means anything written
    under the sentinel is visible to **every** cluster sharing the database. Combine that with an
    in-cluster deployment resolving to the sentinel and the module's stated purpose inverts —
    "patterns from a Kind dev cluster would pollute prompts on prod EKS" becomes the default
    rather than the thing prevented.

    `docs/reflexion.md` also claimed the sentinel rows "age out via retention" so the system
    "naturally converges to per-cluster patterns only". That is true only where identity is
    resolvable; an in-cluster deployment kept minting fresh sentinel rows forever.
    """

    def _memory_store(self) -> str:
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app"
                / "db" / "memory_store.py").read_text()

    def test_recall_reads_sentinel_rows_from_any_cluster(self):
        """Assert the SQL, not a comment about it — this is the mechanism, so pin it."""
        sql = self._memory_store()
        assert sql.count("cluster_id IN ($1, 'unknown')") == 1, "failure_patterns recall"
        assert sql.count("cluster_id IN ($2, 'unknown')") == 1, "rca_outcomes recall"

    def test_the_wildcard_literal_matches_the_sentinel_the_code_can_return(self):
        """If either side is renamed alone, the wildcard stops matching and recall goes silent."""
        assert f"'{UNRESOLVED_CLUSTER_ID}'" in self._memory_store()

    def test_so_an_unresolved_id_makes_recall_global(self):
        """Belt and braces: sentinel in, wildcard hit — stated as the property, not the string."""
        with patch("subprocess.run", side_effect=_fake_kubectl()):
            cid = get_cluster_id()
        assert not cluster_id_is_resolved(cid)
        assert f"IN ($1, '{cid}')" in self._memory_store(), (
            "an unresolved cluster reads and writes the shared wildcard scope")
