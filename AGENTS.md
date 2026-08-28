# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor, Copilot, Aider, …) working in
this repository. Humans should read [`CONTRIBUTING.md`](CONTRIBUTING.md) — it is the
authority; this file is the machine-oriented summary of it.

Format: <https://agents.md/>

---

## What this project is

KubeIntellect is an LLM-orchestrated multi-agent framework for Kubernetes operations. It
answers questions about a live cluster by querying `kubectl`, Prometheus (PromQL), and Loki
(LogQL), and it can *act* on the cluster — but only behind a human-in-the-loop approval gate
with role-based access control.

**This is an incident-response tool for production clusters.** Output that looks like a real
diagnosis but isn't is the single worst failure mode. See "Safety invariants" below — those
are not style preferences.

## Repository layout

The repo holds four generations of the product. **Only `v4/` accepts behavioral changes.**

| Path | Role | Changes allowed |
|---|---|---|
| `v4/` | Current platform — a `uv` monorepo | ✅ Yes, this is where work happens |
| `v3/`, `v2/` | Baseline / experimental lineage | 🔸 Bug fixes and docs only |
| `v1/` | Published, cited by the paper | ❄️ Frozen — docs/typos only |
| root (`Makefile`, `deploy/`, `scripts/`) | Shared infra: one Kind cluster, one observability stack | 🔸 Only if the issue asks |

`v4/` contains three distributions:

- `v4/packages/kubeintellect-server/` — the FastAPI server and the agent graph (`app/`)
- `v4/packages/kube-q/` — the `kq` terminal client
- `v4/packages/ki-protocol/` — the shared SSE wire protocol

Do not "fix" duplication *between* version directories. They are deliberately independent
snapshots of a design lineage, and the older ones are cited by a peer-reviewed paper.

## Working map of `v4/`

Start with [`v4/docs/architecture.md`](v4/docs/architecture.md) for the system topology and
[`v4/docs/configuration.md`](v4/docs/configuration.md) for feature flags. The main code paths
are:

| Concern | Source of truth |
|---|---|
| Process startup and background services | `packages/kubeintellect-server/app/main.py` |
| Settings, environment validation, feature flags | `packages/kubeintellect-server/app/core/config.py` |
| Authenticated API surface | `packages/kubeintellect-server/app/api/v1/router.py` and `packages/kubeintellect-server/app/api/v1/endpoints/` |
| Default V2 LangGraph and session runner | `packages/kubeintellect-server/app/agent/workflow.py` |
| Optional explicit-node Cortex graph | `packages/kubeintellect-server/app/cortex/graph.py` |
| Cluster, Prometheus, Loki, and Helm tools | `packages/kubeintellect-server/app/tools/` |
| Sensorium, detectors, and autonomous investigations | `packages/kubeintellect-server/app/sensorium/`, `packages/kubeintellect-server/app/detectors/`, `packages/kubeintellect-server/app/autonomy/` |
| Memory and persistence | `packages/kubeintellect-server/app/memory/`, `packages/kubeintellect-server/app/db/`, and `packages/kubeintellect-server/app/db/schema.sql` |
| Server-to-client event schema | `packages/ki-protocol/ki_protocol/` |
| Terminal client | `packages/kube-q/kube_q/` |
| Browser demo | `packages/kube-q/web/` |

All paths in this table are relative to `v4/`. `CORTEX_V4_ENABLED` selects the Cortex graph at
graph initialization; the V2 graph remains the default. Do not implement the same behavior
in both graphs unless the issue explicitly requires parity.

The SSE protocol has two deliberately separate views: `ki_protocol/wire.py` defines what the
server emits, and `ki_protocol/events.py` defines what clients parse. Any wire-format change
must update both, plus the server and kube-q parity tests. The server and kube-q test trees are
also separate Python packages named `tests`; run them as separate pytest invocations.

## Setup

Requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
cd v4
uv sync          # creates .venv and installs the whole workspace
```

## Required validation

The root [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is the executable source of
truth. From the repo root, `make setup` installs the workspace and runs all nine
locally-runnable gates in order, but use the standalone commands below when validating a
change so the lint scope cannot lag behind CI. Individually, from `v4/`:

```bash
# 1. Lint — `ruff check` only. NOT `ruff format`.
uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/ packages/kube-q/ tests/ scripts/

# 2. Types — the workspace is at ZERO errors; keep it there.
uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q

# 3. Server suite (5447 tests)
uv run python -m pytest tests/ -q

# 4. kq CLI suite (749 tests)
cd packages/kube-q && uv run python -m pytest tests/ -q

# 5. Doc claims — every documented count is recollected from the code and compared.
uv run python scripts/check_doc_claims.py        # --fix rewrites the numbers in place
```

Plus four repo-root gates, which need no virtualenv. Gates 5, 8 and 9 run inside existing CI
jobs (**Lint (ruff)** for the first, **Syntax warnings** for the other two) rather than jobs
of their own — branch protection matches required checks by name, so a new job name is a check
`main` does not require and every open PR would sit unmergeable until the settings caught up:

```bash
# 6. File modes — a tracked file is executable if and only if it has a shebang.
make check-modes          # or: ./scripts/check-file-modes.sh
make fix-modes            # corrects any violation in place

# 7. Syntax — every tracked .py outside v1-v3 compiles with no SyntaxWarning.
make check-syntax         # or: ./scripts/check-syntax-warnings.py

# 8. Encoding — every text-mode read/write names an encoding (#136/#156).
make check-encoding       # or: ./scripts/check-text-encoding.py

# 9. Roster — .all-contributorsrc and the README table name the same people (#167).
make check-roster         # or: ./scripts/check-contributor-roster.py
```

`ci.yml` produces **15** named checks and `main` requires **9**. Which is which — and why each
unrequired one is unrequired — is recorded in
[`.github/required-checks.yml`](.github/required-checks.yml); `make check-required` compares
that record against the live branch protection (needs an authenticated `gh`, so it is not a CI
job). Adding a job to `ci.yml` without deciding its status fails that check rather than
inheriting silence. Note what the unrequired list contains: **`Container image (build + serve)`
is not required**, so a PR that breaks the published image can merge green.

CI additionally checks the lockfile and clean-wheel installation, builds and probes the
container, and runs the browser demo's lint/build. When a change reaches those surfaces, run
the corresponding focused checks locally:

```bash
# From v4/ — lockfile and all three distributions
uv lock --check
uv build --package ki-protocol -o dist
uv build --package kube-q -o dist
uv build --package kubeintellect -o dist

# From v4/packages/kube-q/web/ — Node 24 in CI
npm ci --no-audit --no-fund
npm run lint
npm run build

# From v4/ — documentation consistency and site build
uv run python scripts/check_doc_claims.py
uv run mkdocs build
```

Python 3.14 test jobs are visible but non-blocking candidate coverage. Python 3.12 and 3.13,
the install smoke test, web build, and container build-and-serve job are blocking coverage.

Both report **what they examined**, not what exists: the mode gate reads one git index
(`--git-dir DIR` points it at another) and the syntax gate compiles the tracked `*.py` it
can read. Either one finding nothing to check now **fails** — a sparse or partial checkout
used to produce a confident pass over zero files.

If you create a file, do not mark it executable unless it is a script with a shebang. This
gate exists because `ruff`'s EXE002 cannot run here (see the pin below), so nothing else
catches a stray `+x`.

The syntax gate exists for the same structural reason: the pinned `ruff` does not report an
invalid escape sequence in the linted scope, `mypy` never compiles source, and pytest only
emits the warning on a cold `.pyc` cache — so a green suite was not evidence. That is how #63
reached an outside contributor, and that escape was also silently corrupting the jsonpath
examples in the coordinator prompt. Never silence one of these; fix the string.

### The suites run on two interpreters

CI runs both suites on **Python 3.12 and 3.13** (`Tests (…)` and `Tests (… · py3.13)`). 3.13 is
not optional coverage — `v4/Dockerfile`'s runtime stage is `python:3.13-slim`, so it is the
interpreter the shipped container actually executes. If a change passes on one and fails on the
other, that difference is the bug.

Do not report work as complete without running these and reading the output.

**The test counts above are gated.** `v4/scripts/check_doc_claims.py` collects both suites and
fails if either number here disagrees, so a PR that adds tests must update this file. That
number drifted to 990-vs-1031 before the gate existed, and a wrong count is worse than none —
an agent uses it to decide whether its own run was complete.

### `mypy` is clean — if it reports something, it is from your change

The workspace type-checks with **zero errors across 172 source files**, and `Types (mypy)` is
a CI job. Do not add `# type: ignore` to silence a real error.

Two annotations are load-bearing and mypy *cannot* verify them — see "Safety invariants" #6.

### Known pre-existing debt — do not try to fix it in an unrelated PR

- **`ruff format --check` is not a CI gate** and would reformat **127** files. `make lint` in
  `v4/` *does* run it, so `make lint` fails on a clean checkout. Use the `ruff check` command
  above to predict CI, not `make lint`.
- **`ruff` is pinned `<0.16`** on purpose — 0.16's default rules reported 438 findings, then
  342 after the `EXE002` family was cleared (#70), and **317 as measured with ruff 0.16.3 on
  2026-08-18** (largest families: `BLE001` 140, `UP045` 91, `PLW1510` 25, `UP035` 17; 120 of the
  317 are auto-fixable). Re-measure rather than quoting this number — it drifts with the code.
  Do not bump the pin. The remaining families
  must land as separate per-rule PRs, **tracked in [#75](https://github.com/MSKazemi/kubeintellect/issues/75)**
  ("replaces the prematurely-closed #64"). Comment there before starting — do not open a new
  issue. (This paragraph previously said the work was untracked and told you to file a fresh
  one; that was wrong and would have produced a duplicate. `scripts/dev-setup.sh` had #75 right
  all along.) Note the consequence of the pin: `ruff` here is blind to `EXE002`/`EXE001`,
  which is why the separate `make check-modes` gate above exists.

  ⚠️ **`UP045` (91 of the 317, and every one of them auto-fixable) is a safety trap, not a
  cleanup.** That combination is the hazard: a single `ruff check --fix` would apply all 91,
  including on the tools where it disables the safety gates. It rewrites
  `Optional[X]` → `X | None`, which on an injected `RunnableConfig` parameter is exactly the
  change invariant #6 below forbids: the run config stops being injected, and RBAC and the
  HITL gate silently stop being enforced *while every test still passes*. Never run
  `ruff --fix` over that family. It needs a hand-audited PR that leaves every
  `RunnableConfig` annotation alone (`app/tools/aci/read_verbs.py`,
  `app/agent/nodes/coordinator.py`, `app/tools/kubectl_tool.py`).

## Safety invariants — never weaken these

A change that violates any of these will be rejected regardless of test results.

1. **Never fabricate an answer.** Any command that emits a diagnosis, report, postmortem, or
   summary must fail loudly when its data source is absent. It must never return a plausible
   hardcoded or synthesized result. This has happened before (PR #58): a command returned a
   literal `status: ok` with an invented high-severity finding on a machine with no cluster,
   and its tests passed because they asserted the hardcoded dict against itself.
2. **Never bypass the HITL approval gate.** Every mutating cluster operation (scale, delete,
   restart, apply) stops for explicit human approval. No flag, config, or "convenience" path
   may skip it.
3. **Never weaken RBAC.** The `admin` / `operator` / `readonly` role checks are load-bearing.
4. **Never widen the kubectl safety guard.** `shell=False` and the command allowlist stay.
5. **Never log or echo secrets.** DSNs and tokens pass through the existing redaction helpers.
6. **Never "fix" an injected `RunnableConfig` annotation.** On tools that receive the run
   config, the annotation must stay exactly:

   ```python
   config: Annotated[RunnableConfig, InjectedToolArg] = None,  # type: ignore[assignment]
   ```

   `langchain_core` matches this parameter by identity (`type_ is RunnableConfig`). Widening
   it to `RunnableConfig | None` — which is what a type checker, a linter, or an agent
   "cleaning up implicit Optional" will suggest — stops the run config being injected at all.
   The tool then silently receives `config=None` and loses `user_role` (RBAC) and
   `hitl_bypass` (the HITL gate). Behaviourally verified by
   `v4/tests/test_kubectl_tool.py::TestAlwaysConfirm`; see issue #54 for the full analysis.

   **The annotation itself is now gated** by `v4/tests/test_injected_config_invariant.py`,
   which scans every `config: Annotated[…, InjectedToolArg]` parameter in `app/` and fails if
   any is not bare `RunnableConfig`, plus two canary tests proving against the installed
   `langchain_core` that the bare form *is* injected and the widened form is *not*.

   That gate was added after finding the widened form live in `app/tools/aci/read_verbs.py`
   (all four ACI read verbs). It was harmless there — those verbs never read `user_role`, so
   no RBAC decision was affected — but it is the exact shape of the failure this invariant
   exists to prevent, and it survived because a tool that never *uses* `config` passes every
   behavioural test while silently receiving `None`. A behavioural test cannot catch that;
   only asserting the annotation can.

## Testing expectations

- Every behavioral change needs a test that **fails before your change and passes after.**
  Verify that directly — write the test, run it against the unpatched code, see it fail.
- Ask of each new test: *what would have to break for this to fail?* If the answer is
  "nothing", the test is asserting a constant against itself and is worthless.
- For a new user-facing command, **run it by hand against a deliberately absent resource**
  (a made-up session id, no cluster configured) and confirm the output is a refusal with a
  non-zero exit code, not a plausible-looking artifact. CI cannot catch this class.

## Conventions

- Match the style of the file you are editing; don't reformat surrounding code.
- Comment density and naming should look like the neighbours'.
- Keep changes scoped to the issue. Drive-by refactors make review harder and get sent back.
- If a capability can be a new *tool* on an existing agent, don't make it a new *agent*.

## Pull requests

- Branch from `main`. One logical change per PR.
- Fill in the PR template, including which gate commands you ran and their output.
- **Disclose substantial AI assistance in the PR description.** This is not held against
  you — it tells reviewers where to look, exactly like "I copied this pattern from the
  Kubernetes docs" would. What matters is that you understand every line well enough to
  explain it in review and that you ran the tests yourself.
- First-time contributors: CI does not run on your PR until a maintainer approves the
  workflow run. If your PR looks stalled with no checks, that is why — it is not your fault
  and you do not need to do anything.

## Do not touch

- `.claude/`, `design/`, `evaluation/`, `papers/`, `v5/` — not part of the public
  contribution surface.
- Anything under `_archives/`.
- The `v1/`–`v3/` trees, beyond documentation typos.
