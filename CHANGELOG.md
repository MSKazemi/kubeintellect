# Changelog

All notable changes to KubeIntellect (v3 / DeepAgents line, branch `exp/v3`) are
documented here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `OPENAI_BASE_URL` config knob — point the OpenAI provider at any
  OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) to run fully self-hosted.
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
