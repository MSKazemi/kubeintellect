"""L0 file plane (v5 P2) — CLUSTER.md / MEMORY.md projections + regeneration orchestrator."""
from __future__ import annotations

from pathlib import Path

from app.memory.file_plane import (
    _bound_bytes,
    regenerate_file_plane,
    render_cluster_md,
    render_memory_md,
)

_EPISODES = [
    {"namespace": "demo", "outcome": "resolved", "verified": True,
     "summary": "web OOMKilled; raised memory limit", "root_cause": "memory-limit-too-low"},
    {"namespace": "shop", "outcome": "report_only", "verified": False,
     "summary": "ingress 502 investigated", "root_cause": None},
]


class TestRenderClusterMd:
    def test_lists_namespaces_and_episodes(self):
        out = render_cluster_md("cl-1", _EPISODES)
        assert "# CLUSTER.md — cl-1" in out
        assert "- demo" in out and "- shop" in out
        assert "web OOMKilled" in out
        assert "Postgres is the source of truth" in out

    def test_empty_is_graceful(self):
        out = render_cluster_md("cl-1", [])
        assert "_none recorded yet_" in out


class TestRenderMemoryMd:
    def test_themes_and_verified_resolutions_only(self):
        out = render_memory_md("cl-1", "## Themes\n- oom stuff", _EPISODES)
        assert "# MEMORY.md — cl-1" in out
        assert "## Themes" in out and "- oom stuff" in out
        # only the verified episode with a root cause becomes a recurring resolution
        assert "memory-limit-too-low" in out
        assert "ingress 502" not in out          # unverified / no root_cause ⇒ excluded

    def test_no_resolutions_is_graceful(self):
        out = render_memory_md("cl-1", "", [{"verified": False, "summary": "x"}])
        assert "_no verified resolutions yet_" in out


class TestBoundBytes:
    def test_under_budget_unchanged(self):
        assert _bound_bytes("short", 1000) == "short"

    def test_over_budget_cut_on_line_with_marker(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        out = _bound_bytes(text, 200)
        assert len(out.encode("utf-8")) <= 200
        assert "truncated to fit" in out

    def test_render_respects_max_bytes(self):
        big = [{"namespace": "n", "outcome": "resolved", "summary": "x" * 500}] * 200
        out = render_cluster_md("c", big, max_bytes=1000)
        assert len(out.encode("utf-8")) <= 1000


class TestRegenerate:
    async def test_writes_both_files(self, tmp_path):
        written = {}
        def writer(path: Path, content: str):
            written[path.name] = content

        async def fetch_eps(cid):
            return _EPISODES

        async def fetch_themes(cid):
            return "## Themes\n- theme A"

        stats = await regenerate_file_plane(
            "cl-1", tmp_path, fetch_episodes=fetch_eps, fetch_themes=fetch_themes, writer=writer,
        )
        assert set(written) == {"CLUSTER.md", "MEMORY.md"}
        assert "web OOMKilled" in written["CLUSTER.md"]
        assert "theme A" in written["MEMORY.md"]
        assert stats["cluster_md_bytes"] > 0 and stats["memory_md_bytes"] > 0

    async def test_writes_to_real_fs(self, tmp_path):
        async def fetch_eps(cid):
            return _EPISODES

        async def fetch_themes(cid):
            return ""

        await regenerate_file_plane("cl-1", tmp_path, fetch_episodes=fetch_eps, fetch_themes=fetch_themes)
        assert (tmp_path / "CLUSTER.md").exists() and (tmp_path / "MEMORY.md").exists()

    async def test_fetch_failure_is_swallowed(self, tmp_path):
        async def boom(cid):
            raise RuntimeError("db down")

        stats = await regenerate_file_plane("cl-1", tmp_path, fetch_episodes=boom, fetch_themes=boom)
        assert stats == {"cluster_md_bytes": 0, "memory_md_bytes": 0}
