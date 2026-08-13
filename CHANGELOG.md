# Changelog

All notable changes to this repository are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

> **Scope.** This is the **repo-wide** changelog. It tracks shared infrastructure and the
> **active development line** — currently the **v4** platform and the **v5** design tier
> (whose slices ship as default-off flags inside the v4 server, ADR-101). The frozen
> generations keep their own history: **v3** has `v3/CHANGELOG.md`; **v1** and **v2** are
> versioned by their git tags (`v1.0`, `v2.0.x`). See the root `README.md` for the v1→v5 lineage.

## [Unreleased]

### Added
- **Two more playbooks — `PvcPending` and `LivenessProbeFailing`** (#94, #95, #127, #128) —
  contributed by [@hariomlohardev](https://github.com/hariomlohardev). The library is now **23
  playbooks / 20 compiled detectors / 3 LLM-only**.

  Both arrived with a working `triggers:` block and a `detect:` block that could not fire, which
  is the [#114](https://github.com/MSKazemi/kubeintellect/pull/114) failure again: `PvcPending`
  declared `kind: PersistentVolumeClaim`, but `kind:` selects the observation *channel* and
  `WatchPredicate.matches()` only knows `Pod`/`Event`/`Node` — every other value falls through to
  `return False`. It parsed, loaded, counted toward the detector total and passed the schema
  check while being a permanent no-op. Re-seated on `kind: Event` + `involved_kind:
  PersistentVolumeClaim`, and `StorageClassNotFound` dropped because no controller emits it
  (`FailedBinding` and `ProvisioningFailed` do).

  `LivenessProbeFailing` matched `reason: ^Unhealthy$` with no message co-condition. The kubelet
  emits `Unhealthy` for **both** probe kinds, so it fired on every readiness failure as well —
  duplicating `ReadinessProbeFailing` and reporting a restart loop that was not happening. Both
  playbooks now carry the message that names the probe, and `ReadinessProbeFailing` no longer
  claims `Liveness probe failed`: a failed readiness probe pulls the pod out of Service
  endpoints, a failed liveness probe makes the kubelet restart the container. Different symptom,
  different fix.

  Guarded in `tests/test_detectors.py`: `test_every_watch_predicate_uses_a_known_observation_kind`
  is a class guard over every shipped detector for the dead-`kind:` family, plus `TestPvcPending
  Detector` and `TestProbeDetectorsDoNotCrossFire` (both directions). All five fail against the
  originals — verified by reverting.
- **Worked examples for the remaining eight `kq` subcommands** (#86–#93, #119–#126) — contributed
  by [@hariomlohardev](https://github.com/hariomlohardev). `v4/docs/examples.md` covered 2 of 10
  subcommands; it now covers all 10, as sections 10–17. Every transcript is real output, checked
  against `kube-q` 1.5.0 byte-for-byte at merge time.

  Adopted with edits. The eight PRs each numbered their section `10` and each closed with the
  same copied sentence — *"a zero-token local operation … it never contacts the server"* — which
  is true of `kq config show` and `kq completion`, and false of `replay`, `postmortem`, `export`,
  `detector`, `preference` and `v5-status`, all of which call the server. In an incident tool a
  confident wrong statement is the failure mode that matters, so each section now says what its
  command actually does. Six of the eight `cli-reference.md` anchors did not exist (`#kq-replay`
  vs the real `#kq-replay-session-id`, etc.) and are fixed; `mkdocs build` is clean. The `kq
  config show` transcript had the contributor's home directory in it, now `/home/you`.

### Changed
- **The memory recall similarity floor is configurable** (#14, #116) — contributed by
  [@uuzzrm](https://github.com/uuzzrm). The `pg_trgm` noise floor was hard-coded as `0.02` twice,
  independently, in `memory/episodes.py` and `memory/summaries.py`, so tuning recall for a
  cluster with an unusual vocabulary meant editing two constants in two modules and hoping they
  stayed equal. Both now read `MEMORY_RECALL_SIMILARITY_FLOOR`, a validated setting
  (`ge=0.0, le=1.0`) whose default is the same `0.02`. No behaviour change out of the box. The
  hybrid RRF path still receives the threshold in SQL and still does **not** re-apply it
  post-fetch, which is what keeps a lexical-only match from being dropped (ADR-014).
- **Cleared three ruff-0.16 rule families from the backlog** (#79, #110) — contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). `UP017` (`datetime.timezone.utc` →
  `datetime.UTC`, 5 sites), `UP041` (`asyncio.TimeoutError` → `TimeoutError`, 2 sites) and
  `RUF022` (sort `__all__`, 3 sites). All three are provably no-ops here: `datetime.UTC` is an
  alias for the same object since 3.11, `asyncio.TimeoutError` **is** the builtin `TimeoutError`
  since 3.11 (the same class, not a subclass, and both sites are `except` clauses), and nothing
  iterates `__all__` in an order-dependent way. Every package declares `requires-python = ">=3.12"`.

  The four `from datetime import datetime, timezone` lines were rewritten to import `UTC` rather
  than left importing a now-unused name, which is what keeps `F401` quiet under the pinned `ruff`
  without a second cleanup pass. The deliberate `# noqa: UP017` in `kube_q/cli/store.py` is
  untouched, and no annotation was widened — `UP045` stays out of scope because on this codebase
  it silently disables RBAC and the HITL gate (AGENTS.md invariant #6). One slice of #75; the pin
  itself stays until the rest clears.

### Fixed
- **`.env.example` was unreachable from a fresh clone** (#115, #117) — fixed by
  [@hariomlohardev](https://github.com/hariomlohardev). About fifteen places
  (`CONTRIBUTING.md`, `v4/README.md`, `v4/Makefile`, five deploy docs,
  `langfuse-provision.sh`) tell you to `cp .env.example .env`, and the file was not in the repo.
  The reported cause — a commit deleting it — was not the live one: `.gitignore`'s `.env.*`
  matches `.env.example`, so it could not be re-added at all until that pattern was negated.
  Now `!.env.example` / `!**/.env.example`, with `v4/.env.example` tracked.

  Adopted with one change: the PR restored the 244-line template from before the file was lost,
  which no longer describes the product — it omits the `qwen` and `anthropic` providers (both
  supported, and both asserted by `test_doc_claims`) and still tells you to copy Langfuse keys
  out of the UI, which `make langfuse-provision` replaced. Merged the current 289-line template
  instead. Scanned: every credential field is empty or an obvious placeholder.
- **The Homebrew formula's `desc` was still too long for `brew audit --strict`** (#113, #118) —
  fixed by [@hariomlohardev](https://github.com/hariomlohardev), who also sorted the `resource`
  blocks alphabetically and added `scripts/verify-brew.sh` so #113's two never-run commands are
  reproducible.

  Adopted with a correction: brew measures `"<name>: <desc>"`, not the description alone, so the
  PR's 75-character `desc` was still 83 with the `kube-q: ` prefix and would still have been
  flagged — and the script asserted `desc <= 80`, the wrong threshold, so it reported PASS on it.
  Both now use the real rule; the description is 78 including the prefix. `brew audit` and `brew
  install` themselves remain unrun — still no Homebrew on any machine here — so #113 stays open
  for that, but the static half is now checkable by anyone with `bash`.
- **The Homebrew formula could not install, and misstated the licence** (#56, #111) — fixed by
  [@uuzzrm](https://github.com/uuzzrm). It declared `license "MIT"` on AGPL code, pointed
  `homepage` at `MSKazemi/kube_q` (the #74/#78 defect, which the issue had not caught), targeted
  the pre-relicense 1.0.0, and carried four `resource` blocks for a seven-dependency package —
  **two of them with 63-character sha256 values**, which are not valid digests at all rather than
  merely stale ones. Now 1.5.0, `AGPL-3.0-or-later`, the canonical homepage, and the complete
  18-resource dependency tree with `certifi` taken from Homebrew.

  Verified by downloading every artifact and hashing the bytes — 19/19 match — and by resolving
  `kube-q==1.5.0` independently for Python 3.12: the closure is exactly complete, nothing missing
  and nothing extra. **`brew audit --strict` and `brew install --build-from-source` have still
  not been run by anyone**; neither contributor nor maintainer has Homebrew available, so the
  `maturin`/`rust` build path for `pydantic-core` is reasoned rather than observed. Tracked in
  #113. The formula is not a tap, so nothing is installable from here either way — the licence
  misstatement was the live defect, and it is fixed.

  The issue itself was **partly wrong** and has been corrected in place: `kube-q` 1.0.0 does
  exist (uploaded 2026-04-10, the oldest release) and its recorded sha256 matched the formula, so
  the "points at a release that does not exist" premise was false. It survived a re-verification
  because that check printed `sorted(releases)[-3:]` — the last three entries, which structurally
  cannot show 1.0.0.
- **The `kq` test suite failed depending on the contributor's terminal** (#106, #109) — fixed by
  [@floze-the-genius](https://github.com/floze-the-genius). The suite inherited `COLUMNS`, `TERM`
  and `NO_COLOR` from the invoking shell, which changed Rich's table wrapping and the theme the
  module-level console is built with. A contributor on an 80-column terminal could watch tests
  fail before touching a line of code, on a suite that was green in CI. On `2c1f676`:
  `COLUMNS=40` → 5 failed, `COLUMNS=60` → 1 failed, `TERM=dumb` → 1 failed, `NO_COLOR=1` →
  1 failed.

  The fix pins the rendering environment in `pytest_configure` — **before** test modules are
  imported — and re-applies it per test with an autouse fixture that `monkeypatch` can still
  override, restoring the invoking environment in `pytest_unconfigure`. **No assertion was
  weakened**, which was the point: the tempting fix is to loosen assertions until they pass at
  any width, which makes the suite green and stops it testing anything.

  The `pytest_configure` half is load-bearing, and
  [@AshSgDe29071999](https://github.com/AshSgDe29071999) is why that is documented rather than
  assumed. They independently diagnosed the same root cause and submitted the fixture-only
  form (#107); running it as a control showed it clears four of the five environments and
  still fails under `NO_COLOR=1`, because an autouse fixture runs after collection, by which
  point the module-level console already exists with the stripped theme. Now 312/312 on all
  five single-variable runs, on all three applied together, and on a clean environment.
- **The published `kube-q` package pointed users, and their bug reports, at the wrong
  repository** (#74, #78) — every `[project.urls]` entry resolved to `MSKazemi/kube_q`, a
  pre-AGPL snapshot that is not where `kube-q` is developed. On a package serving ~160
  downloads a month, "Homepage", "Repository" and "Bug Tracker" all led away from the canonical
  repo, and the PyPI page carried no link to the docs, the changelog, or this repository. Fixed
  by [@shaurya703](https://github.com/shaurya703), who also gave `ki-protocol` the `authors`,
  `[project.urls]` and `classifiers` it had never had — its page was blank, including **no
  licence classifier at all** on AGPL code.

  All three distributions moved to the [PEP 639](https://peps.python.org/pep-0639/) form
  (`license = "AGPL-3.0-or-later"` + `license-files`). This removes a defect the issue had not
  spotted: `kube-q` used `license = { file = "LICENSE" }`, which dumped the **entire 34 KB AGPL
  text** into the `License:` metadata header. Every wheel now reports a machine-readable
  `License-Expression` under `Metadata-Version: 2.4`, and the redundant
  `License :: OSI Approved :: …` classifier is gone — PyPI rejects an upload carrying both.

  **@shaurya703 also caught that `license-files` resolves relative to each package directory**,
  and that neither `ki-protocol` nor `kubeintellect-server` had a `LICENSE` of its own — so
  those wheels had been shipping **without the licence text**, a real compliance gap in an
  AGPL project that was invisible from the source tree. Both now carry a byte-identical copy,
  verified in the built artifacts (`twine check` passes on all six).

  The live PyPI pages stay wrong until the next publish; this fixes the source of them.
- **The `kube-q` documentation still sent readers to the old repository** — 13 files across
  `v4/` linked `MSKazemi/kube_q`, including `v4/packages/kube-q/README.md`, which **is** the
  body of the PyPI page: it told readers to `git clone https://github.com/MSKazemi/kube_q`.
  So the metadata fix above would have corrected the sidebar links while the page underneath
  still pointed elsewhere. Found by [@shaurya703](https://github.com/shaurya703) while working
  on #78, from a single `mkdocs.yml` observation. The from-source instructions now clone the
  monorepo and `cd kubeintellect/v4/packages/kube-q` (verified end to end: clone → `pip install
  -e .` → `kq --version` reports 1.5.0 — which only resolves at all now that `ki-protocol` is
  published). The mkdocs social link to `ghcr.io/mskazemi/kube_q` was a **404** and now points
  at the image that exists. The Homebrew formula's stale homepage is deliberately untouched —
  it belongs to #56.
- **Imports are sorted across `v4/`** (#76, #77) — `ruff` 0.16 enables `I001` by default and
  reported 75 unsorted blocks across 60 files, part of what blocks lifting the `ruff<0.16` pin.
  Cleared by [@shaurya703](https://github.com/shaurya703), verified at the AST level rather
  than by eye: the imported-symbol set is identical in all 60 files and there is no non-import
  code change anywhere. The one manual edit is the interesting one — ruff's autofix wraps the
  long `langchain_anthropic` import into parenthesized form, which moves
  `# type: ignore[import-not-found]` off the `from` line and breaks the suppression (mypy 0 →
  1). The ignore now sits on the `from … import (` line, keeping both the sorted form and the
  suppression. `UP045` remains untouched by design; the remaining ~364 findings are tracked in
  #75.
- **The Greetings workflow had never posted a single greeting** — it passed the
  `first-interaction` action its inputs hyphenated (`issue-message`, `pr-message`,
  `repo-token`) while the action declares them underscored. The runner exposes unknown inputs
  under their own names rather than rejecting them, so the action threw
  `Input required and not supplied: issue_message` on every issue and every pull request since
  the workflow landed; `repo-token` only appeared to work because `repo_token` defaults to
  `${{ github.token }}`. Every first-time contributor got silence plus a red X — precisely the
  two things the workflow's own header says it exists to prevent, and what #73's author saw.
  The failure was easy to dismiss as bot noise because it also fired on every Dependabot PR,
  leaving them permanently `UNSTABLE`.
- **The demo UI is ESLint-clean again** (`v4/packages/kube-q/web`, #50, #73) — 2 errors and 2
  warnings, reported by [@AdvaitVarhade](https://github.com/AdvaitVarhade), who also traced the
  two `react-hooks/set-state-in-effect` errors to `app/page.tsx`; the issue had attributed them
  to `PtyTerminal.tsx`. `PtyTerminal` now builds its status notifier inside the connection
  effect and reaches the current callback through a ref, so the effect depends on `authToken`
  alone — a re-rendered parent no longer tears down a live PTY session — and the unused
  `useState` import is gone.

  The two errors in `page.tsx` are fixed by removing the effects rather than deferring them.
  `sessionStorage` is now read through `useSyncExternalStore`, which is SSR-safe and derives the
  token during render, so the `tokenReady` state and its mount effect are both gone; and the
  auth-failure prompt is raised in the `onStatusChange` callback that causes it instead of from
  an effect watching the state that callback just wrote. Deferring the same `setState` behind
  `setTimeout(…, 0)` would satisfy the linter while making the behaviour worse — the cascading
  render still happens, now after paint, so the terminal pane visibly flashes empty on every
  load. Verified in a browser as well as by `npm run lint` and `npm run build`: the terminal
  mounts, connects, and propagates status, with no hydration or `getSnapshot` warning under
  dev StrictMode.

  The ref that carries the callback is updated from an effect rather than during render, since
  React may discard a render and a ref written there would outlive it. The xterm-init failure
  path now builds its message as a text node instead of assigning `innerHTML`.

### Added
- **An `HPANotScaling` playbook** (#97, #114) — the 21st, contributed by
  [@Chris7717](https://github.com/Chris7717). It covers the autoscaler that does nothing and
  looks identical to one that is simply not needed, and it separates the two root causes that
  Kubernetes reports through the *same* `FailedGetResourceMetric` /
  `FailedComputeMetricsReplicas` events but that need different fixes: metrics-server missing or
  unreachable (cluster-wide) versus a container with no `resources.requests.cpu`
  (workload-specific). Both were reproduced against a kind cluster, so the
  `investigation_steps` and `expected_evidence` are transcribed from real `kubectl` output
  rather than reasoned. It compiles to a detector — 18 of the 21 playbooks now do.

  Adopted with two maintainer fixes to the `detect:` block, neither of which any existing gate
  could see. The `reason_regex` was `"^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"`:
  the spaces around the alternation bar bind to the branches, so the compiled predicate required
  an event reason *containing a space* and could therefore never match anything — the detector
  loaded, counted, and passed the schema check while being a permanent no-op. The stored PromQL
  named `kube_horizontalpoduatoscaler_status_condition` (transposed) and matched `status="False"`,
  where kube-state-metrics emits the status label lower-case; that arm is latent until #20 lands,
  but it was stored wrong.

  The gate gap mattered more than either typo: the PR's tests exercised `match_playbooks()` — the
  prompt-side `triggers:` path — and nothing anywhere asserted that a compiled predicate can
  actually *fire*. `test_detectors.py` now checks this playbook against both real event reasons,
  and a new class guard rejects any shipped `reason_regex` whose alternatives carry surrounding
  whitespace, since a Kubernetes event reason never contains a space. Both fail against the
  original file.

- **A `DeploymentRolloutStuck` playbook** (#98, #112) — the 20th, contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). It covers the rollout that fails
  *without an outage*: the new ReplicaSet never becomes Available, the old one keeps serving
  traffic, and nothing looks broken from the outside until someone asks why the deploy never
  finished. It compiles to a detector (17 of the 20 playbooks now do), with a 600-second
  debounce so a slow-but-progressing rollout does not fire it.

  The part worth copying is that it **routes rather than duplicates**: `ProgressDeadlineExceeded`
  is almost always a symptom, so the investigation steps chain to `ReadinessProbeFailing`,
  `PendingInsufficientResources`, `PendingSchedulingConstraints` and `ImagePullBackOff` for the
  actual cause, and the fix template opens with "do **not** force-delete pods." The fourth
  `expected_evidence` entry names the condition under which the playbook does *not* apply,
  which is a discipline none of the previous nineteen had.

- **A `NetworkPolicyBlocking` playbook** (#99, #108) — the 19th, contributed by
  [@Priyanshu608](https://github.com/Priyanshu608). It covers the one failure in the set that
  emits **no evidence at all**: a NetworkPolicy denial is discarded in the CNI datapath, so the
  API server records no event and no Warning is ever produced. The Service is healthy, endpoints
  exist, DNS resolves — and the connection simply hangs. The playbook makes that *absence* the
  signal, which is what none of the other 18 could express.

  Two maintainer corrections landed on top. The submitted trigger also matched
  **`connection refused`**, which is evidence *against* a policy drop: a refusal means a TCP RST
  came back, so the packet was delivered. Matching it would have pointed the agent at a
  NetworkPolicy for what is almost always a wrong port or a missing endpoint —
  `ServiceUnreachable`'s territory. It now matches timeout signatures only, and
  `test_networkpolicy_blocking_ignores_connection_refused` locks that in. The `triggers:` block
  was also nested under `detect:`, where the loader never reads it
  ([`loader.py`](v4/packages/kubeintellect-server/app/agent/playbooks/loader.py) takes
  `triggers` from the top level), so the playbook loaded cleanly into the registry and then
  never fired — inert, with no error anywhere. `test_each_playbook_has_complete_schema` catches
  exactly this and did.

- **The test suites now run on Python 3.13 as well as 3.12** (`Tests (… · py3.13)`), closing
  a gap where the project was verified on an interpreter it does not ship. `v4/Dockerfile`'s
  runtime stage is `python:3.13-slim` and all three distributions declare
  `requires-python = ">=3.12"`, so every container and every `pip install` on a current
  machine already ran 3.13 — while CI pinned 3.12 in every job. Both suites pass unchanged
  (990 server + 312 `kq`), so this adds coverage rather than fixing a break; the point is that
  a future 3.13-only regression now fails a gate instead of reaching users. Added as a
  separate job rather than a `python-version` matrix axis on purpose: the axis would rename
  `Tests (server)`, and branch protection matches required checks by name. The
  `Programming Language :: Python :: 3.13` classifier on the server distribution is now
  earned rather than assumed.
- **A `Syntax warnings` CI gate (`make check-syntax`, `scripts/check-syntax-warnings.py`)** —
  compiles every tracked Python file outside the frozen v1–v3 trees with `SyntaxWarning`
  promoted to an error, on the newest supported interpreter. This closes the structural blind
  spot that let #63 reach an outside contributor: the pinned `ruff` does not report an invalid
  escape sequence in the CI-linted scope, `mypy` never compiles source, and pytest triggers the
  warning only on a cold `.pyc` cache — so a green suite was not evidence either way. The
  defect class is not cosmetic: #63's non-raw string was also corrupting the jsonpath examples
  in the coordinator prompt. Like the `File modes` gate it is deliberately dependency-free
  (stdlib only, no `uv sync`) so it stays correct however the `ruff` pin is eventually lifted,
  and it uses `compile()` rather than `compileall` so it leaves no `.pyc` files behind.
  `make setup` now runs all six gates instead of four.
- **A `File modes` CI gate (`make check-modes`, `scripts/check-file-modes.sh`)** enforcing one
  invariant outside the frozen v1–v3 generations: *a tracked file is executable if and only if
  it starts with a shebang*. This closes a structural blind spot rather than a one-off mess —
  `ruff` is pinned `<0.16` and `EXE002` only became a default rule in 0.16, so the lint
  gate could not see a stray `+x` bit at all, which is how 94 library modules silently acquired
  one. The guard is deliberately dependency-free (git + coreutils, no `uv sync`), so it stays
  correct whichever way the ruff upgrade lands, and it runs in seconds. It also covers the
  inverse defect (`EXE001`: a shebang'd script that is not executable, i.e. one you cannot run),
  which `ruff` only reports for Python. `make fix-modes` corrects violations in place, updating
  both the working tree and the index — a `chmod` alone would leave the committed mode unchanged.
- **One-command contributor setup: `make setup` (`scripts/dev-setup.sh`).** Installs `uv` if
  missing, installs the `v4` workspace, then runs the *exact* four gate commands CI runs
  (ruff, mypy, server suite, `kq` suite) and reports which passed. A contributor therefore
  learns their environment is correct *before* changing anything, and never debugs their setup
  and their change at the same time. It also names the pre-existing debt that is **not** their
  bug (`make lint`'s non-gate `ruff format --check`, the deliberate `ruff<0.16` pin). No
  cluster, Docker daemon, or LLM API key is required — the suites are mocked.
- **`.devcontainer/devcontainer.json`** — zero-install contribution via GitHub Codespaces or
  VS Code Dev Containers, pinned to the Python 3.12 that CI pins and running the same setup
  script on create.
- **`AGENTS.md`** ([agents.md](https://agents.md/) format) — machine-readable repository rules
  for AI coding agents: gate commands, the six safety invariants, testing expectations, and
  the pre-existing debt not to "fix". Human authority remains `CONTRIBUTING.md`.
- **A `Contributing` section on the README front page**, plus a live good-first-issue badge and
  a header nav link. The invitation previously existed only as one sentence under `Maintainer`
  near the bottom of a 221-line file.
- **`.github/workflows/greetings.yml`** — welcomes a contributor's first issue and first PR,
  and pre-empts the two things that most often make newcomers give up: silence, and a fork PR
  that appears stalled because GitHub runs no CI for first-time contributors until a maintainer
  approves the run.
- **`.github/workflows/labeler.yml` + `.github/labeler.yml`** — path-based `area/*` PR
  labelling mirroring the issue taxonomy in `TRIAGE.md`, so a contributor never needs to know
  the repo's internal structure to be routed correctly.
- **`.github/ISSUE_TEMPLATE/documentation.yml`** — a documentation issue template, making
  "the docs were unclear" a first-class report rather than a bug-report misfit.
- **A `Contributing` section in `llms.txt`**, so answer engines asked how to contribute to
  KubeIntellect have a grounded answer (Python-3.12-only prerequisite, `v4/` scope, the HITL
  invariant, and where the good first issues are).

- **`kq export <session-id>` — export a diagnosis report to JSON or YAML.** Serializes the
  same grounded postmortem `kq postmortem` renders (a view over the hash-chained decision
  log) for archiving, ticket attachments, or downstream tooling. `--format json|yaml`,
  `--output PATH`. Stdout stays machine-parseable (notes and warnings go to stderr), so
  `kq export <id> | jq` works. Exit codes follow the `kq replay` convention: `3` when the
  audit chain is broken, and `4` when the recorder holds no events for the session — in
  which case nothing is written, rather than emitting an empty-but-plausible report.
  Closes #58. Thanks to @AdvaitVarhade for proposing the capability and the initial
  implementation.

- **`Types (mypy)` is now a blocking CI gate**, and `make typecheck` runs it locally. The
  workspace type-checks with **zero errors** across all 171 source files, down from 30. The
  remaining `ruff format` debt is unaffected and still not a gate.
- `tests/test_workflow_config_injection.py` — a regression guard asserting that every graph
  node and tool declaring a `config` parameter actually receives the run config. See below
  for why that is not obvious.

### Fixed
- **Cleared the stray executable bit from 94 source files** under
  `v4/packages/kubeintellect-server/app/` and `v4/packages/ki-protocol/` — file-mode only
  (`100755 → 100644`), zero content changes, verified by the patch containing no `+`/`-` content
  lines. These are library modules with no shebang, so `chmod -x` is the correct fix rather than
  adding one. Clears `EXE002` in the CI-linted scope and takes the ruff 0.16 finding count from
  438 to 342, unblocking part of #64. Thanks to [@hariomlohardev](https://github.com/hariomlohardev)
  ([#70](https://github.com/MSKazemi/kubeintellect/pull/70)).
- **Extended the same mode-only sweep to the remaining 282 files** outside that lint path
  (the rest of `v4/`, `deploy/`, and three root files), and gave the four shebang'd scripts that
  were *not* executable their `+x` back. The frozen v1–v3 generations are deliberately untouched
  (ADR-001/002): they are closed to changes and not built by CI, so rewriting ~500 of their file
  modes would be churn against immutable history for no gate benefit.
- **The coordinator system prompt was silently corrupted.** `_COORDINATOR_SYSTEM` was a
  non-raw string containing jsonpath separators, so `{"\n"}` and `{"\t"}` were interpreted
  before the model saw them — a real newline split an example `kubectl get pods -o jsonpath=…`
  command mid-line, and a literal tab replaced another. Now a raw string. This also clears the
  `SyntaxWarning: invalid escape sequence '\`'` that Python is scheduled to turn into a
  `SyntaxError` (#63).
- **RCA synthesis crashed on providers that return content blocks.** `_synthesize` called
  `response.content.strip()`, which raises `AttributeError` when `content` is a list rather
  than a string; it now uses `response.text`, which flattens the blocks.
- **API keys are wrapped in `SecretStr`** before being handed to `ChatOpenAI` /
  `AzureChatOpenAI`, so a client repr or traceback cannot render the raw key.
- `init_graph()` raises a named error when `POSTGRES_DSN` is unset with `USE_SQLITE=false`,
  instead of failing inside the driver.
- **Four type errors in `app/cli.py`** (#55): `callable` used as a type annotation is now
  `Callable[[], None]`, and the two `subprocess.run` calls that rebound a
  `CompletedProcess[str]` variable declare `text=True`, so the type is consistent without
  changing runtime behaviour. Thanks to @hariomlohardev for the fix (#57).
- Dropped an f-prefix from a placeholder-free string in `v4/scripts/fix_pr_probe.py` (F541).

### Changed
- **The PyPI release is unblocked — all three distributions are published and current** (#66,
  closed). `ki-protocol` **1.0.0**, `kube-q` **1.5.0**, `kubeintellect` **2.2.0**, all reporting
  `AGPL-3.0-or-later` instead of the stale `MIT` metadata. Verified in clean venvs:
  `pip install kube-q` yields 1.5.0 with all ten subcommands (checked *without* `--help`, per
  the argparse trap), and `pip install kubeintellect` yields 2.2.0 with a working entry point.

  Two defects surfaced while unblocking it, neither of which the issue anticipated:
  **(1) `kube-q`'s trusted publisher authorized the wrong repository** — `MSKazemi/kube_q`
  rather than `MSKazemi/kubeintellect` — so the root `publish.yml` could never have published
  it, and confirming only the *workflow filename* would not have caught it.
  **(2) A trusted publisher registered without an environment does not match a workflow that
  runs with one.** `kubeintellect`'s publisher said `Environment: (Any)` while `publish.yml`
  runs `environment: pypi`, and every upload failed with `403 OIDC scoped token is not valid
  for project 'kubeintellect'` while the two publishers naming `pypi` explicitly succeeded in
  the same run. Registering a second publisher with the environment set explicitly fixed it.
- **README's PyPI warning block is gone** — it existed only while the published packages were
  behind, and both install paths now work as documented. The server quickstart also no longer
  claims `kubeintellect init` "creates a Kind cluster, deploys samples"; verified against the
  published 2.2.0 CLI, `init` writes `~/.kubeintellect/.env` and cluster creation is the
  separate `kind-setup` command.
- **`TRIAGE.md` no longer tells contributors that `mypy` is non-blocking debt.** It became a
  required CI check in `0c7b055`; a contributor following the old text would have pushed a PR
  expecting a `mypy` failure to be ignorable and had it blocked instead.
- `CONTRIBUTING.md` leads with the one-command path and states up front that **no cluster,
  Docker daemon, or LLM API key is needed** to run the suites — previously the requirements
  list implied all three were mandatory before you could contribute a typo fix.
- `types-PyYAML` added to the dev dependency group; the server's mypy baseline drops from
  29 to 27 errors (two pre-existing missing-stub errors resolved).
- **First `[tool.mypy]` configuration for the workspace** (#53) — `python_version = "3.12"`
  plus a per-module `ignore_missing_imports` override for `asyncpg`, which ships no `py.typed`
  marker. Clears the 5 remaining `[import-untyped]` errors without touching source. Thanks to
  @hariomlohardev (#65).
- `max_tokens=` → `max_completion_tokens=` on the OpenAI/Azure chat clients — the same
  pydantic field under its public alias, so the request payload is unchanged.
- Container runtime image moves from `python:3.12-slim` to `python:3.13-slim` (#59).
  Verified independently of CI, which does not build the image: the full dependency set
  resolves on CPython 3.13.14, both entry points start, and the 986-test server suite passes.
- `uvicorn[standard]` floor raised `>=0.32` → `>=0.52.1` to match the resolved version (#61).

## [2.2.0] – 2026-08-08

### Documentation
- **Canonical repo consolidated to `MSKazemi/kubeintellect`.** Repointed the README star badge
  (was rendering the org mirror's 0-star count), `CITATION.cff` `repository-code`, and the four
  `llms.txt` doc links from the `kubeintellect/kubeintellect` org mirror to the personal repo, which
  holds the organic stars. Ends the star-count fragmentation between the two identical public repos
  (org mirror to be redirected + archived).
- **v4 product docs — major quality pass.** Audited every page against the code and fixed
  drift (homepage playbook stat 10→18, `KUBE_Q_URL` default, `AZURE_OPENAI_API_VERSION`,
  demo-key TTL vars, a misplaced CLI flag table); documented the previously-undocumented
  `/v1/preferences` API and the `kq config` command group; added new **FAQ**,
  **Upgrade & feature-flag guide**, **Examples & cookbook**, and **Changelog** pages; deepened
  the quickstart with an end-to-end happy path; added front-matter descriptions to 13 pages;
  fixed all broken cross-page anchors; relocated the Qwen-hackathon submission collateral out
  of the product docs (to `v4/hackathon-submission/`) and clarified the Install-vs-Deploy nav
  split. `mkdocs build` is clean.
- **Investor pack + industry white paper refined (private, not in the public mirror).**
  Consistency/honesty pass on the fundraising docs (numeric cross-checks against the financial
  model, explicitly-labelled illustrative assumptions, real cited market sources — CNCF 2025,
  AIOps market size — with honest `[SOURCE NEEDED]` gaps, no fabricated traction) and the LaTeX
  white paper (MAPE-K/autonomic-computing citation, an evaluation results figure, benchmark-
  scoped detection claims, trimmed redundancy); all PDFs rebuild clean.

### Added
- **Snap packaging for the `kq` CLI.** New `snap/snapcraft.yaml` builds a `kubeintellect` snap
  (core24, amd64 + arm64) carrying the terminal client, with `snap/README.md` documenting the
  build, the confinement trade-offs, and the store-publishing steps. Deliberately **strict**
  confinement rather than the `classic` that `kubectl`/`helm`/`k9s` use: `kq` is an HTTP client
  whose filesystem needs are two known directories, so they are requested as explicit
  `personal-files` plugs — `dot-kube` (read `~/.kube`) and `dot-kube-q` (write `~/.kube-q`) —
  since the `home` interface excludes top-level dot-directories. `HOME` is remapped to
  `$SNAP_REAL_HOME` so `Path.home()` resolves outside the sandbox. Packaged as a single `python`
  part that vendors the client past its Next.js demo UI (sourcing `packages/kube-q` directly
  would drag ~600 MB of `node_modules` through the pull step) and installs it together with
  `ki-protocol` in one `pip` call, because `ki-protocol` is not on PyPI yet. Not published to
  the Snap Store yet — the name is unregistered and `personal-files` needs an approved snap
  declaration.
- **Snap CI.** `.github/workflows/snap.yml` builds both architectures on any PR touching
  `snap/`, `kube-q`, or `ki-protocol`, installs the built snap on the runner and smoke-tests
  `--version`, `--help`, and completion generation; publishing is a separate manually-dispatched
  job gated on a `SNAPCRAFT_STORE_CREDENTIALS` secret.
- **Community: adoption and prioritisation surfaces.** `ADOPTERS.md` (honestly empty rather
  than padded) plus a pinned adoption thread (#51), and a pinned roadmap voting index (#52)
  that makes 👍 reactions the explicit prioritisation signal for the "Next" list; the six
  roadmap issues carry a new `roadmap` label. New `adoption`, `needs-info`, and
  `area/packaging` labels.
- **Community: `SUPPORT.md` and `TRIAGE.md`.** `SUPPORT.md` routes questions to the right
  channel, says what to include, and states plainly that there is no SLA. `TRIAGE.md`
  documents the full issue lifecycle — the five triage questions, what every label means and
  what it promises, how to claim an issue, the two-week unassignment rule, and what each close
  reason means — written so that a non-maintainer can triage without repo permissions.
- **Container image publishing to GHCR and Docker Hub.** New `.github/workflows/docker-publish.yml`
  builds the v4 image once and publishes it to `ghcr.io/mskazemi/kubeintellect` and
  `docker.io/kazemi/kubeintellect`, tagged by semver plus `latest` and the commit sha, with OCI
  labels and `VERSION`/`GIT_SHA` build args. Triggered by a `v*` tag or manually, with a dry-run
  option. Docker Hub is skipped with a warning until `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` are set.
  Private material cannot reach the image: CI checks out the public repository, the build context is
  `v4/`, and the Dockerfile copies only the three workspace package source trees — verified against a
  local build, whose image contains just `app/`, `ki_protocol/` and `kube_q/`.
- **Doc-claims drift guard (v4).** New `v4/scripts/check_doc_claims.py` reads the canonical
  numbers straight from code — 18 shipped playbooks (loader), 16 baseline compiled detectors
  (`load_detectors()`), the valid LLM-provider set (`config.py`), and the count of `KI_V5_*`
  flags — and asserts every numbered claim in the docs still matches, exiting non-zero on drift.
  Wired as `make docs-check` and enforced by `tests/test_doc_claims.py`. It caught a real drift:
  the `api-reference.md` `/v1/findings` example said `"detectors": 18` (the playbook count) and is
  now corrected to `16` (what the endpoint actually reports at baseline). Chosen as a *check* rather
  than a prose auto-generator so hand-written docs are guarded, not clobbered.
- **Documentation standardized across all five versions (v1–v5).** New
  `design/adr/002-standard-doc-surface.md` defines a canonical doc surface + mkdocs nav +
  metadata standard (the doc analog of ADR-001); every version's docs were brought to it
  as-built. v3 gained 7 missing canonical pages (cli-reference, api-reference, capabilities,
  troubleshooting, operations, glossary, memory); verified-against-code accuracy fixes landed
  across v1 (agent count 13), v2 (18 playbooks), v3 (2 providers, 4 role tiers, DeepAgents
  agent-behaviors), and v4 (18 failure detectors, `openai|azure|qwen|anthropic` providers).
  Root `README.md` now documents the full v1→v5 lineage; `v5/README.md` added (design tier).
- **Architecture reference extended to five generations.** `architecture-comparison/`
  (both the Markdown edition and the LaTeX/PDF, now 31 pp) gained code-grounded **V4**
  (platform) and **V5** (design-tier) chapters, a V4 architecture diagram, and updated
  lineage/comparison/module-map tables — one as-built v1→v5 technical reference.
- **Cortex mypy cleanup (quality gate).** Full-gate audit (1251 tests + ruff green) found and fixed 3 latent type issues in `app/cortex` (synthesize `messages: list[BaseMessage]`, remember `isinstance` narrowing, optional `langchain_anthropic` import-ignore); `mypy app/cortex` now clean, no behavior change.
- **v5 experimental-flags reference.** New `docs/v5-experimental-flags.md` catalogs all ~60 `KI_V5_*`/`CORTEX_V5_ENABLED` flags (flag / default / purpose, generated from `config.py`), linked from `configuration.md` — one place to see every additive default-off toggle.
- **`/v1/v5/status` API-reference entry + boot-time version log.** Documented the endpoint (fields + example) in `docs/api-reference.md`; the server now logs its full version identity + active v5 flags at startup (ADR-019).
- **`kq v5-status` CLI + CLI-reference entry.** Terminal view of the v5 trust plane (version, active flags, kill switch / change freeze / spend cap) via `GET /v1/v5/status`; documented in `docs/cli-reference.md`.
- **v5 trust-plane observability + arm-3 calibration harness.** New `GET /v5/status` surfaces version identity, active `KI_V5_*` flags, and the fail-closed brakes (kill switch / change freeze / spend cap) in one read-only call. Added `calibrate_offline_weight` (ADR-102 arm-3 calibration harness; simulation-validated, production cap value awaits real matched offline+live shadow data).
- **v5 fix-PR write class validated END-TO-END with a real PR.** The misconfig fix-PR flow (repair → fix_pr → push → PR) opened a genuine GitHub pull request (MSKazemi/kubeintellect-private#1) proposing `runAsNonRoot: false → true` — the P3 first write class exercised against a live remote, not just locally.
- **v5 data-source activation (additive, default-off).** Agentic-workload + GPU-health metric collector (queries Prometheus for agent tool-call rate/cost, sandbox-escape, ResourceClaim/ECC/GPU-OOM → runs the predicates; active/dormant-until-breach) and a Postgres fleet-signal store that pools per-cluster signals into fleet-wide pattern detection (LIVE-validated on real PG: 3 clusters → critical FleetAlert, tenant-isolated).
- **v5 integration-activation (additive, default-off).** Pre-capture wired into the live watchtower
  (imminent predicted finding → arm recorders before death); change-watchdog loop activated in the
  consolidation worker (changes-since-last-sweep → read-only fan-out investigation, timestamp-deduped)
  — the P4 anticipation loop now runs end-to-end (agent change → ledger → sweep → investigate-the-diff).
- **v5 loop-closing + fleet slices (additive, default-off).** P3: spend usage source (OTel token
  spend → deny-before-breach, closes the spend-budget loop) and blast-radius composite gate (budget
  + staged-propagation + failure-domain in one fail-closed verdict). P5: fleet-wide signal pooling
  (same signal on ≥N clusters/tenant → FleetAlert, tenant-isolated) and fleet-store RLS tenancy
  scaffolding (ADR-105 policy defined, disabled pending GUC-binding). Each behind a `KI_V5_*` flag.
- **v5 P3/P4 hardening slices (additive, default-off).** P3: failure-domain + change-schedule budget
  (per-zone unavailability caps ≤~⅓ + maintenance windows, REQ-sysadmin-18). P4: NL-detector →
  statistical ladder (ADR-012 shadow-precision gate, human review retained), agentic-workload SRE +
  GPU-health detectors (agent-runaway/sandbox-escape + GPU/ResourceClaim health, the incumbent-empty
  surface D17/SD-D), predictive pre-capture (arm recorders before an imminent predicted death,
  A-CH-20-16). Each behind a `KI_V5_*` flag.
- **v5 P3 Trust plane + P4 Anticipation + P5 Fleet (additive, all default-off; validated live where a
  cluster/DB/LLM applied).** P3: blast-radius/spend budget gate (kill switch, change freeze, spend
  cap), statistical promotion engine (ADR-102) + Postgres outcome store, mutating-verb chokepoint
  (rollback classification + write-authority), server-side dry-run, machine-checkable postcondition
  oracle, transactional apply→verify→auto-rollback (TNR), two-axis capability sandbox (SA
  impersonation), security-outcome gate, misconfig LLM auto-repair + fix-PR generator + GitOps PR
  opener, staged propagation (never instant-global), failure-domain + change-schedule budget. P4:
  heterogeneous model routing + air-gap read-only floor (ADR-103), per-change ephemeral watchdog +
  fan-out dispatch, evidence-grounded rightsizing, predictive-detection fusion. P5: cross-cluster
  fleet memory exchange with strict tenant isolation + Postgres-backed durable store (ADR-105). Every
  slice gated behind a `KI_V5_*` flag (default off ⇒ byte-identical V4). LIVE-validated on n1 Kind +
  Azure gpt-4o + Postgres: P3 write path (real mutation + rollback + RBAC), fix-PR (Azure + git),
  promotion loop (real PG), fleet isolation (real PG). ADRs 101/102/103/105 technical gates met
  (awaiting owner ratification); ADR-104 deferred.

### Fixed
- **`CONTRIBUTING.md` gave commands that do not exist.** The quality-gate section told
  contributors to run `uv run mypy src` (there is no `src/` in the workspace layout) and
  `uv run ruff check .` (CI lints only `packages/kubeintellect-server/app/` and
  `packages/ki-protocol/`); both are replaced with the exact commands from `ci.yml`, plus a note
  that `make lint`'s `ruff format --check` is not a CI gate. The dev-setup snippet also still
  claimed the archived org mirror was canonical. Added a worked first-PR walkthrough against a
  real open issue.
- **Container image licence label corrected (legal).** `v4/Dockerfile` labelled the image
  `org.opencontainers.image.licenses="MIT"` while the project is AGPL-3.0. Every published image
  would have misrepresented its terms. Now `AGPL-3.0-only`, with `url` and `documentation` labels
  added and the `source` URL cased to match the canonical repo. Caught while inspecting the built
  image before enabling public publishing.
- **v3 HITL fail-open closed (safety).** `v3/app/agent/hitl.py` approval/denial detection now
  matches a leading decisive token, not only exact whole-message phrases — a multi-word denial
  ("no don't do that") is no longer silently treated as approval by the `resume = not is_denial(...)`
  resume gate. Part of the 2026-07-10 v3 code-improvement batch (registry single-source-of-truth,
  bounded `deepagents`, memory/429/snapshot truncation logging, `SNAPSHOT_MAX_CHARS` config) —
  full detail in `v3/CHANGELOG.md`.

## [2.1.0] – 2026-07-05

### Added
- **v5 P0 foundations + P2 investigation-core (additive, all default-off; ADR-019 → v4.x, NOT a fork).**
  P0: K8s-ACI v0 read verbs, the ADR-101 harness subagent contract + read-only fan-out seam and body,
  OTel GenAI spans as hash-chained `decision_log` rows, Wilson-LCB promotion-stats (ADR-102), and the
  OpsMemBench deterministic core + live driver. P2: adversarial verification ladder, never-silent
  responsiveness heartbeat + latency budget, escalation-avoidance briefs, and runbooks-as-skills. Every
  slice is inert unless its `CORTEX_V5_ENABLED` + `KI_V5_*` flag is set (flags off ⇒ byte-identical V4).
  Validated live on a Kind cluster (35/35 probes).
- **Version identity surface (ADR-019).** `GET /healthz` now returns the three version axes — `arm`
  (the `KI_VERSION` generation), `version` (the package SemVer, which is what distinguishes v4 / v4.1 /
  v4.2), and the active `experimental_flags` — so a running instance is fully identifiable. New
  `app/core/version.py` (`version_info` / `version_line`). Server SemVer **2.0.2 → 2.1.0**.

### Added
- **Qwen Cloud support (Qwen Cloud Hackathon — MemoryAgent track).** `LLM_PROVIDER=qwen`
  is a first-class provider that auto-targets Alibaba DashScope's OpenAI-compatible
  endpoint (`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`) with `qwen-max`
  (coordinator/synthesis) + `qwen-plus` (parallel RCA subagents); `DASHSCOPE_API_KEY` /
  `QWEN_API_KEY` are honest aliases for the key. `OPENAI_BASE_URL` is now honored by the
  OpenAI client factory (covers Cortex too). `scripts/verify_qwen.py` checks live chat +
  tool-calling. Cost table extended with qwen-max/plus/turbo pricing.
- **Operator-preference memory (MemoryAgent).** `app/memory/preferences.py` upgrades the
  thin `user_prefs` table into a learned layer: explicit (confidence 1.0, immortal) +
  behaviour-**inferred** preferences (e.g. `default_namespace` from RCA history) with
  confidence, decay, and **forgetting** (`preference_purge()`), learned/forgotten by the
  consolidation worker and injected into the prompt. New `kq preference set/list/forget`
  CLI + `GET/PUT/DELETE /v1/preferences` API.
- **Memory V5 upgrade — hybrid recall + bi-temporal knowledge graph (behind default-off
  flags; MemoryAgent).** Grounded in a state-of-the-art study (`design/memory-v5/`: 619
  systems / 1023 resources / ADRs 013–018). Two additive, flag-gated slices ship inside V4:
  (1) **`MEMORY_HYBRID_RETRIEVAL`** — episode recall fuses the `pg_trgm` channel with a
  full-text `ts_rank` channel via Reciprocal Rank Fusion (RRF, k=60) in one query, with a
  functional FTS GIN index (`idx_episodes_fts`, no table rewrite) and graceful fallback to
  the trigram baseline. (2) **`MEMORY_BITEMPORAL_ENABLED`** — the temporal KG gains a
  transaction-time axis (`ingested_at`/`retracted_at`): event-time `valid_from`, point-in-time
  `as_of(valid_t, tx_t)` queries, retract-not-delete supersede (audit-preserving), and a
  `mean_ingest_lag_seconds` freshness signal. (3) **`MEMORY_KG_PPR`** — multi-hop
  blast-radius over the KG: a bounded ≤3-hop induced subgraph via a recursive CTE, then
  Personalized PageRank computed **in-process** (dependency-free power iteration), so
  `kg.ppr_blast_radius(seeds)` ranks the entities most related to an incident. (4)
  **`MEMORY_WRITE_RECONCILE`** — Mem0-style write reconciliation: `kg.reconcile_edge()`
  decides ADD/UPDATE/**RETRACT**/NOOP against existing memory behind a query-independent
  salience gate (dedup + supersede); RETRACT sets `retracted_at` (never hard-deletes) and
  defaults to ADD when confidence is low. (5) **`MEMORY_PROMOTION`** — the learning loop: the
  consolidation worker promotes verified, recurring episodes into a new `semantic_rules` table
  (IF-context → THEN-guidance); a rule that recurs enough goes `active` (injected into the
  prompt) and is eligible to seed a detector *candidate* (reusing the existing human-review
  flow). (6) **`MEMORY_IMPORTANCE`** — importance/surprise-weighted retention (ADR-017): each
  episode write is scored for `importance` (incident severity — regression > partial > resolved,
  boosted by verified + confidence) and `surprise` (a KG-novelty proxy), stored on new
  `episodes.importance`/`surprise` columns; recall then ranks **recency × importance ×
  relevance** (importance modulates *ranking only*, never retention/audit), and a surprise gate
  drops redundant *low-value* auto-writes (unverified + report-only near-duplicates) while always
  keeping verified/actioned episodes. (7) **`MEMORY_PROSPECTIVE`** — first-class prospective
  memory (ADR-017): a new `prospective_memory` table lets the watchtower record a "re-check
  condition C at/after T" after an autonomous fix ("did the fix hold?"); the consolidation
  scheduler claims due re-checks (atomic `FOR UPDATE SKIP LOCKED`), fires each through the
  autonomy ladder (A0 namespaces never fire), and records the outcome. (8)
  **`MEMORY_SECURITY_HARDENING`** — security-hardened write path (ADR-018): defends the top
  threat the study surfaced, **MINJA-style query-only memory injection**, with a
  write-admission guard of **diverse, non-LLM-primary** validators (design review F5: a quorum
  of the same LLM fails together under MINJA) — provenance/**trust** scoring (sensor = ground
  truth, user-chat = low-trust), a heuristic persistent-instruction injection-signature check,
  a per-requester sliding-window rate limiter, and a caller-supplied contradiction check vs
  high-confidence sensor facts; a quarantined write is dropped, and a `[0,1]` provenance `trust`
  is stamped on every episode. Adds **tamper-evidence** (R8.2): an append-only, per-cluster
  SHA-256 **hash chain** over memory-mutating events (admitted writes and quarantined poison
  attempts) in a new `memory_audit` table, computed with the same primitive as the flight
  recorder (ADR-005) and verifiable via `verify_memory_chain` — a silent edit, delete, or reorder
  of learned memory is detectable. Ships with an always-available **right-to-be-forgotten**
  (`forget_subject`) and the pgbouncer-safe **`SET LOCAL ki.cluster_id`** tenant-context helper
  for the Row-Level-Security scaffolding (RLS policies documented in `schema.sql`, enabled with
  the paired GUC discipline). (9) **`MEMORY_SUMMARY_TREE`** — a RAPTOR/GraphRAG-style **summary
  hierarchy** (spec R7): the consolidation worker builds one deterministic theme summary per
  `(cluster, playbook|namespace)` signature into a new `memory_summaries` table, so the agent can
  answer theme-level questions ("what keeps failing in payments?") without scanning every episode;
  matching theme summaries are injected into the triage prompt alongside episode recall (via
  `memory_loader`), so a query gets cross-incident context, not just the top-k episodes.
  **Regeneration is tied to KG change-rate** — a theme is rebuilt only when new episodes arrived or
  the cluster's KG edge count moved (a conditional `ON CONFLICT … WHERE` upsert), never on a fixed
  clock; abstractive LLM roll-up is deferred (deterministic aggregates today). All default off;
  memory-never-breaks-a-request discipline preserved (the guard fails *open*). All migrations
  additive/idempotent (PostgreSQL 16-compatible).
- **OpsMemBench — an ops-memory benchmark (Memory V5 P9).** A new benchmark harness
  (`evaluation/opsmembench/`) measuring five memory-dependent operator abilities no existing
  benchmark covers — **M1** cross-incident recall, **M2** temporal/time-travel reasoning, **M3**
  knowledge updating/contradiction, **M4** learned detection, **M5** abstention/no-false-memory —
  over versioned, deterministic Kubernetes incident timelines whose gold answers derive from the
  injection script (no LLM judge for the core metrics; leakage-free). Ships pure metric functions
  (recall@k, MRR, set-F1, abstention/false-recall, latency percentiles), a `ScoreCard` grader with
  an `ablation_table` (the paper's core "V5 minus one decision at a time" result), a sample timeline
  (`oomkill-recurrence`), and a CLI (`selftest` / `grade` / `ablation`). Runnable today offline via
  `make opsmembench`; the live cluster driver (drive the agent through fault injection → record
  predictions → `grade`) is the documented completion step. Fills the gap between chat-memory
  benchmarks (LongMemEval, LOCOMO) and memoryless ops-RCA benchmarks (OpenRCA, RCAEval, PetShop).
  A second, harder timeline (`fleet-multi-theme`, distractor-heavy — stresses M1 precision and M5
  abstention) and a one-command ablation demo (`make opsmembench-demo`: No-memory / V4-flat /
  V5-full) reproduce the paper's core table offline, showing memory ability rise monotonically as
  design decisions are added.
- **Multi-cloud deployment — runs on any provider.** First-class Helm overlays,
  `make` targets, and runbooks for **AWS EKS** (`values-aws.yaml.example`,
  `make aws-deploy-kubeintellect`, `docs/deploy/aws.md`, ALB ingress + RDS) and
  **GCP GKE** (`values-gcp.yaml.example`, `make gcp-deploy-kubeintellect`,
  `docs/deploy/gcp.md`, GCE ingress + Cloud SQL), alongside the existing Azure AKS and
  Alibaba ACK/ECS paths. A shared `_cloud-deploy` recipe keeps the LLM provider
  decoupled from the cloud (azure | openai | qwen | anthropic on any of them), and
  `docs/deploy/cloud.md` gains a full provider matrix (ingress class / managed DB /
  storage class per cloud).
- **Memory-ablation experiment harness** (`evaluation/memory_ablation.py`): quantifies
  the "smarter after every incident" claim by running each recurring incident across two
  fresh sessions with the memory hierarchy toggled on vs off, and reports the Encounter-2
  delta in investigation steps / latency / recall rate. Runnable via
  `python -m evaluation.memory_ablation run --arm {on,off}` + `compare`.
- **Full-showcase feature flags are now chart-configurable + a one-file overlay.**
  The chart ConfigMap now exposes `PREDICTIVE_DETECTION_ENABLED`,
  `NL_DETECTOR_AUTHORING_ENABLED`, and `POSTMORTEM_LLM_NARRATIVE` (alongside the
  existing Cortex/Watchtower/Autonomy toggles), and `values-showcase.yaml.example`
  flips every V4 layer on in one file — layer it on any cloud overlay for the full
  MemoryAgent demo.
- **Anthropic/Claude wired into the Helm chart** (`secrets.anthropicApiKey` +
  `ANTHROPIC_API_KEY`/`ANTHROPIC_LARGE_MODEL`/`ANTHROPIC_SMALL_MODEL`), so all four
  LLM providers (azure/openai/qwen/anthropic) are deployable, not just runnable locally.
- **Alibaba Cloud deployment path.** Two overlays: `values-alibaba.yaml.example`
  (managed ACK/ACR/ApsaraDB RDS/ESSD, production-grade) and
  `values-ecs-k3s.yaml.example` (the cost-friendly single-ECS + single-node k3s path:
  in-cluster Postgres on `local-path`, `ClusterIP`, no RDS/SLB, trimmed to a 2C4G box).
  `make alibaba-deploy-kubeintellect` + `scripts/alibaba_ecs_k3s_bootstrap.sh` (one-shot
  k3s+helm install) + `docs/deploy/alibaba.md` runbook with coupon/no-out-of-pocket guardrails.
- **Hackathon submission docs:** `docs/memoryagent-design.md`, `docs/demo-script.md`,
  `docs/architecture-diagram.md`, `docs/qwen-cloud-integration.md`, `SUBMISSION.md`.
- **Three new V4 operator capabilities** (all flag-gated, fail-open, zero-token at
  runtime; ADR-010/011/012):
  - **Anticipatory / predictive detection** (`PREDICTIVE_DETECTION_ENABLED`): trend
    predicates project a range-PromQL metric toward its threshold via a hand-rolled
    least-squares slope and fire a `predicted` finding *before* a slow-burn failure
    (e.g. OOM) manifests. Predicted findings are capped at autonomy `A1` — they
    investigate but never auto-fix.
  - **Grounded incident postmortems** (`GET /v1/episodes/{id}/postmortem`,
    `kq postmortem`): a read-only narrative over the hash-chained flight recorder
    where every line cites its event sequence number and the audit chain is verified;
    an optional LLM narrative is constrained to the recorded events.
  - **Natural-language detector authoring** (`NL_DETECTOR_AUTHORING_ENABLED`,
    `POST /v1/detectors`, `kq detector`): compile a plain-English failure into a
    detector that runs in **shadow** (observes only, never reaches the watchtower)
    until a human promotes it.
- **Unified root infrastructure.** Cluster + observability are now a single
  source at the repository root: a root `Makefile`, `deploy/` (kind, Helm
  Langfuse chart, Grafana, docker-compose monitoring), and `scripts/`
  (`kind/create-kind-cluster.sh`, `langfuse-provision.sh`), shared by all
  versions against one `testbed-v2` cluster and the `monitoring` namespace.
- **Langfuse auto-provisioning** (`make langfuse-provision`): generates a project
  key pair once and injects it into `.env` (and fans it into `v2/v3/v4/.env`),
  then `make langfuse-install` deploys with those keys — eliminating the previous
  manual key sync and the `sk-lf-change-me` placeholder.
- **Per-version cost attribution** via a `KI_VERSION` config field and
  `version:vN` Langfuse trace tags emitted from all instrumented versions.
- **Paper manuscript** (`paper/`): FGCS `elsarticle` source for "From Assistant
  to Autonomous Operator," with the related-work survey, architecture/pillar
  sections, and the multi-version evaluation tables/figures.

### Changed
- **Relicensed v4 from MIT to AGPL-3.0-or-later (dual-licensed).** The open-source
  license is now GNU AGPL-3.0-or-later; a separate commercial license is available
  (`LICENSING.md`). Updated `v4/LICENSE`, `kube-q/LICENSE`, the three `pyproject.toml`
  declarations + OSI classifiers, README badge, and added `CITATION.cff` +
  `THIRD_PARTY_NOTICES.md`.
- **Per-version (`v2/`, `v3/`, `v4/`) Makefiles are now app-only** — shared infra
  targets and duplicated `deploy/`/`scripts/` directories moved to the root.
- **Stronger, independent evaluation judge.** The LLM-as-judge is decoupled from
  the system-under-test's coordinator and points at a separate Azure deployment
  (`EVAL_JUDGE_AZURE_*`), with reasoning-model request support.

### Fixed
- **Langfuse Redis auth**: the Langfuse Redis now runs with `--requirepass` and a
  matching client secret, clearing the recurring `langfuse-worker` `ERR AUTH`
  warnings.
- **Observability ingress**: routed `langfuse.local` / `prometheus.local` /
  `loki.local` correctly (ingress controller pinned to the node that maps host
  port 80), restoring host→cluster reachability for trace/metric/log collection.

### Notes
- The evaluation harness, run artifacts, and `.env` files remain local-only
  (git-ignored); they are not part of the published tree.
