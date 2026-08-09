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

## Setup

Requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
cd v4
uv sync          # creates .venv and installs the whole workspace
```

## Gate commands — these are exactly what CI runs

From the repo root, `make setup` runs all six gates in order. Individually, from `v4/`:

```bash
# 1. Lint — `ruff check` only. NOT `ruff format`.
uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/

# 2. Types — the workspace is at ZERO errors; keep it there.
uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q

# 3. Server suite (990 tests)
uv run python -m pytest tests/ -q

# 4. kq CLI suite (312 tests)
cd packages/kube-q && uv run python -m pytest tests/ -q
```

Plus two repo-root gates, which need no virtualenv:

```bash
# 5. File modes — a tracked file is executable if and only if it has a shebang.
make check-modes          # or: ./scripts/check-file-modes.sh
make fix-modes            # corrects any violation in place

# 6. Syntax — every tracked .py outside v1-v3 compiles with no SyntaxWarning.
make check-syntax         # or: ./scripts/check-syntax-warnings.py
```

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

### `mypy` is clean — if it reports something, it is from your change

The workspace type-checks with **zero errors across 171 source files**, and `Types (mypy)` is
a CI job. Do not add `# type: ignore` to silence a real error.

Two annotations are load-bearing and mypy *cannot* verify them — see "Safety invariants" #6.

### Known pre-existing debt — do not try to fix it in an unrelated PR

- **`ruff format --check` is not a CI gate** and would reformat ~108 files. `make lint` in
  `v4/` *does* run it, so `make lint` fails on a clean checkout. Use the `ruff check` command
  above to predict CI, not `make lint`.
- **`ruff` is pinned `<0.16`** on purpose — 0.16's default rules reported 438 findings, now
  **342** after the `EXE002` family was cleared (#70). Do not bump it. The remaining families
  must land as separate per-rule PRs; #64 tracked this and was closed when `EXE002` cleared,
  so the rest is currently untracked — open a fresh issue before starting, don't assume it is
  unclaimed work. Note the consequence of the pin: `ruff` here is blind to `EXE002`/`EXE001`,
  which is why the separate `make check-modes` gate above exists.

  ⚠️ **`UP045` (95 of the 342) is a safety trap, not a cleanup.** It rewrites
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
   `hitl_bypass` (the HITL gate). This is verified by
   `v4/tests/test_kubectl_tool.py::TestAlwaysConfirm`; see issue #54 for the full analysis.

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
