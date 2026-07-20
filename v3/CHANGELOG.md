# Changelog

All notable changes to KubeIntellect (v3 / DeepAgents line, branch `exp/v3`) are
documented here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Security
- **HITL fail-open hole closed.** `is_approval`/`is_denial` (`app/agent/hitl.py`) now
  recognize a leading decisive token ("yes fix it", "no don't do that"), not only exact
  whole-message phrases. The resume gate is `resume = not is_denial(...)`, so a multi-word
  denial that exact-match previously missed was silently treated as an **approval**. Now
  such denials are honored (fail-safe toward *not* mutating). +2 parametrized test groups.

### Added
- `SNAPSHOT_MAX_CHARS` config setting (default 8000, replaces the hard-coded
  `_SNAPSHOT_MAX_CHARS`) — raise it for large clusters whose snapshot probes overflow.
- `tests/test_registry.py` — guards that `ALL_TOOLS` stays the single source of truth
  for the coordinator's tool surface (7 tools; memory/playbook/snapshot included).
- `OPENAI_BASE_URL` config knob — point the OpenAI provider at any
  OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) to run fully self-hosted.

### Changed
- `app/tools/registry.py::ALL_TOOLS` is now the canonical tool list (all 7 tools);
  `main_agent.build_agent` imports it instead of re-declaring the list, so the registry
  and the compiled graph can no longer drift.
- Subagent temperature (`app/core/llm.py`) documented as intentionally `0.0`
  (deterministic, reproducible fan-out).
- `deepagents` dependency bounded `>=0.5.3,<1.0` — a breaking `1.0` can no longer be
  pulled in silently (pre-1.0 package). Lockfile refreshed.

### Observability
- `load_memory_context` logs when the ≤1800-char pinned-context cap truncates memory.
- `query_prometheus` / `query_loki` log a `warning` on non-200 responses (esp. `429`,
  including any `Retry-After`) so backend rate-limiting is visible in ops logs.
- `refresh_snapshot` logs when a kubectl probe is truncated at `SNAPSHOT_MAX_CHARS`.
- `docs/architecture.md` — reference page for the v3 DeepAgents topology
  (coordinator → 5 specialist subagents → synthesis, `task()` dispatch,
  `/findings/` virtual filesystem, snapshot pre-seeding, HITL, SSE bridge).
- `ruff` declared in the dev dependency group so `make lint` runs out of the box.

### Fixed
- Updated 19 stale unit tests that asserted v2 behavior to match the intentional
  v3 changes (shell guard allows `|`/`<`/`>`, advisory YAML validation, read-only
  verbs on protected namespaces, `TRUNCATED` casing, refactored streaming flow).
- `tests/conftest.py` now clears the v3 superadmin tier and HMAC demo-key secret,
  so auth is actually disabled in tests (fixes spurious 401s in SSE/HITL tests).
- `make test` uses `uv run python -m pytest` instead of `uv run pytest`, avoiding
  stale console-script shebangs when the project venv is relocated.
- 17 `ruff check` errors and project-wide `ruff format` drift.

### Changed
- Corrected `docs/security.md` for v3: four auth tiers (+ HMAC demo keys), the
  read-only-on-protected-namespaces policy, and the actual shell-metacharacter
  guard (`<`/`>` allowed under `shell=False`; YAML validation is advisory).
- `docs/agent-behaviors.md` now cross-references the DeepAgents architecture.

[Unreleased]: https://github.com/mskazemi/kubeintellect/tree/exp/v3
