# Contributing to KubeIntellect

First off — **thank you.** KubeIntellect is an open-source, human-governed AI SRE for
Kubernetes, and it gets better every time someone files a bug, sharpens a doc, or ships a
feature. This guide gets you from clone to merged PR.

New contributors are welcome. If you're looking for a place to start, browse issues labeled
[`good first issue`](https://github.com/MSKazemi/kubeintellect/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
and [`help wanted`](https://github.com/MSKazemi/kubeintellect/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22).

- 💬 **Questions / ideas** → [GitHub Discussions](https://github.com/MSKazemi/kubeintellect/discussions)
- 🐛 **Bugs** → [open an issue](https://github.com/MSKazemi/kubeintellect/issues/new/choose)
- 🆘 **Stuck / need help** → [SUPPORT.md](SUPPORT.md) tells you which channel to use and what to include
- 🙋 **Just using it?** → adding a row to [ADOPTERS.md](ADOPTERS.md) is a real contribution, and a fine first PR
- 🔒 **Security** → **do not** open a public issue; see [SECURITY.md](SECURITY.md)
- 📜 **Conduct** → all interaction is governed by our [Code of Conduct](CODE_OF_CONDUCT.md)
- 🧭 **Triage** → how issues get labelled and prioritised is written down in [TRIAGE.md](TRIAGE.md)

---

## This is a multi-generation monorepo

The repo holds several generations of the same product (`v1/` → `v4/`), each a self-contained
re-architecture. **`v4/` is the current, actively developed version — start there.** `v1/` is
the architecture described in the [published paper](https://doi.org/10.1007/s10723-026-09837-6)
and is frozen; please don't send behavioral changes to `v1/`–`v3/` unless an issue explicitly asks.

| Version | Role | Contributions welcome? |
|---|---|---|
| **`v4/`** | Current platform | ✅ Yes — this is where active work happens |
| `v3/`, `v2/` | Baseline / experimental lineage | 🔸 Bug fixes & docs only |
| `v1/` | Published, legacy | ❄️ Frozen — docs/typos only |

The repo **root** (`Makefile`, `deploy/`, `scripts/`) manages the *shared infrastructure*
(one Kind cluster, one observability stack, one Langfuse). Each version directory owns its own
*application* build, tests, and docs.

---

## Dev setup

**Requirements:** Python **3.12+**, [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl`, `helm`.

```bash
# Canonical repo. (github.com/kubeintellect/kubeintellect is an archived mirror.)
git clone https://github.com/MSKazemi/kubeintellect.git
cd kubeintellect/v4

cp .env.example .env      # fill in your LLM API key at minimum (OpenAI or Azure OpenAI)
uv sync                    # install the workspace (kubeintellect-server, kube-q, ki-protocol)
```

Most tests mock the Kubernetes client and LLM, so you can develop and run the suite
**without a cluster**. For end-to-end work against a real cluster, bring up the shared infra
from the repo root:

```bash
make kind-cluster-create     # one shared Kind cluster
make monitoring-install      # Prometheus + Grafana + Loki
make langfuse-provision      # shared Langfuse project + token
```

See the [v4 README](v4/README.md) for every install path in detail.

---

## The contribution workflow

1. **Find or open an issue first.** For anything larger than a typo, comment on the issue (or
   open one) so we can align on approach before you invest time. For big changes, open an
   [RFC](https://github.com/MSKazemi/kubeintellect/issues/new/choose) or a
   [Discussion](https://github.com/MSKazemi/kubeintellect/discussions) first.
2. **Fork & branch.** Branch from `main`: `git checkout -b fix/pod-log-truncation`.
3. **Write the change *and its tests* in one pass.** Every code change ships with tests.
4. **Run the gates locally** (below) — green before you push.
5. **Sign off your commits** (DCO, below).
6. **Open a PR** using the template. Link the issue (`Closes #123`), describe the *why*.

How issues get labelled, prioritised, assigned, and closed is written down in
[TRIAGE.md](TRIAGE.md) — including how to claim an issue and what "quiet for two
weeks" means.

---

## Your first PR, start to finish

A complete worked example, using a real open issue —
[#12: fix `ruff` F541 in `fix_pr_probe.py`](https://github.com/MSKazemi/kubeintellect/issues/12).
It is one line of code, which is the point: the goal here is to get the *process*
working once, on something that cannot go wrong.

**1. Claim it.** Comment "I'd like to take this" on the issue. No permission needed.

**2. Fork and clone.**

```bash
gh repo fork MSKazemi/kubeintellect --clone   # or fork in the UI, then git clone
cd kubeintellect
git checkout -b fix/f541-fix-pr-probe
```

**3. Set up just enough to run the gates.** You do **not** need a Kubernetes
cluster, a Docker daemon, or an LLM API key to run the test suite — almost
everything is mocked.

```bash
cd v4
uv sync            # ~1 min; installs the three workspace packages
```

**4. Reproduce the problem before fixing it.** This habit is worth more than the fix.

```bash
uv run ruff check scripts/fix_pr_probe.py     # shows the F541
```

**5. Make the change**, then prove it:

```bash
uv run ruff check scripts/fix_pr_probe.py     # now clean
```

**6. Run the same gates CI will run** (the section below has the exact commands).
For a one-line lint fix, `ruff check` plus the suite for the package you touched is
enough; run both suites if you're unsure.

**7. Commit with a DCO sign-off** — the `-s` is required (it is checked in review,
not by CI, so it is easy to forget; `git commit --amend -s` fixes it):

```bash
git commit -s -m "fix(server): drop f-prefix from placeholder-free string (F541)"
git push -u origin fix/f541-fix-pr-probe
```

**8. Open the PR.**

```bash
gh pr create --fill --body "Closes #12"
```

Fill in the template, say *why* in one or two sentences, and mention if you used AI
assistance (see below — it is welcome, just disclosed).

**9. Expect a reply, not silence.** Every PR gets a first response, even if the
answer is "not this way". If CI fails on something unrelated to your change, say so
in the PR — pre-existing `ruff format` debt is not your bug.

**Stuck at any step?** That is a documentation defect, not a you problem — say so in
[Discussions](https://github.com/MSKazemi/kubeintellect/discussions/categories/q-a)
and this section gets fixed.

---

## Quality gates — green before you push

These are the **exact** commands CI runs
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Run them from `v4/`:

```bash
uv sync                                                        # once, after cloning

# 1. Lint — the CI gate is `ruff check` only (not `ruff format`; see the note below)
uv run ruff check packages/kubeintellect-server/app/ packages/ki-protocol/

# 2. Types — must stay at zero errors
uv run mypy packages/kubeintellect-server/app packages/ki-protocol packages/kube-q/kube_q

# 3. Tests — two suites, run separately: the two `tests` packages collide
#    under a single pytest invocation.
uv run python -m pytest tests/ -q                              # server (~990 tests)
cd packages/kube-q && uv run python -m pytest tests/ -q        # kq CLI (~312 tests)
```

A PR that fails these will fail CI. If a gate is failing for a reason unrelated to your change,
say so in the PR.

> `make lint` in `v4/` also runs `ruff format --check`, which currently fails on pre-existing
> formatting. That is **not** a CI gate — use the `ruff check` command above to predict CI.

### PR checklist

- [ ] Change is scoped to `v4/` (or a version whose contributions are open)
- [ ] New behavior has both a happy-path **and** an error-path test
- [ ] **Every write/mutating operation keeps its dry-run + diff + human-approval (HITL) gate** — this is a safety requirement, not a UX choice
- [ ] Secret values are never logged or returned (key names only)
- [ ] `pytest`, `ruff check` and `mypy` pass locally (these are the CI gates)
- [ ] Docs updated if behavior/CLI/flags changed
- [ ] Commits are signed off (DCO)

> **On `mypy`:** it is now a blocking gate and the workspace sits at **zero errors** — if it
> reports something, it is from your change. Two annotations are load-bearing and mypy cannot
> see why: LangGraph and LangChain both decide whether to inject the run config by
> *pattern-matching the `config` parameter's annotation*, so respelling it silently disables
> RBAC and the HITL gate. `tests/test_workflow_config_injection.py` guards this — if it fails,
> read its docstring before changing an annotation.
>
> **On `ruff format`:** still **not** a gate — it would reformat ~108 files, which needs to land
> as its own commit. Tracked in [ROADMAP.md](ROADMAP.md). You are not expected to fix that in
> your PR. If a gate fails for a reason unrelated to your change, say so in the PR; it is not
> your bug.

---

## AI assistance

**AI assistance is welcome.** This project is itself an AI tool; it would be strange to ban the
tooling. What matters is not how a change was produced but whether you stand behind it.

Regardless of how you wrote it, you must:

- **Understand it** — every line, well enough to explain the approach in review.
- **Have run the tests locally**, and read the output.
- **Take responsibility for it** as your own work, including the DCO sign-off.

Please **disclose substantial AI assistance** in the PR description. This is not a black mark;
it just helps reviewers know where to look, exactly like "I copied this pattern from the
Kubernetes docs" would.

Two specific asks for this project in particular:

1. **Never let a generated change weaken the safety model.** The HITL approval gate, the RBAC
   checks, and the mutating chokepoint are load-bearing. A plausible-looking refactor that
   quietly bypasses them is the single most dangerous PR we could merge, and it is exactly the
   kind of thing generated code does confidently.
2. **Don't open a PR you can't defend.** We review on the merits, and "why this approach rather
   than X?" is a normal question. If you can answer it, the PR is fine — that's the whole
   filter, and it applies identically to hand-written code.

We will never reject a contribution *because* AI was used. We will reject one that is untested,
doesn't fit the design, or that the author cannot explain — the same bar as always.

---

## Design principles (read before adding agents/tools)

KubeIntellect earns every layer of complexity. Two rules that PRs are held to:

- **If a capability can be a new *tool* on an existing agent, don't make it a new *agent*.**
- **Every mutating action must pause for explicit human approval, gated by RBAC.** An LLM never
  acts on the cluster unilaterally. Read-only queries run immediately; scale/restart/delete/patch
  require a human `approve`.

Treat all retrieved cluster text (logs, events, ConfigMaps, user YAML) as **untrusted input** —
never embed it into a system prompt as instructions. This is the primary defense against indirect
prompt injection.

---

## Developer Certificate of Origin (DCO) + licensing

KubeIntellect is **dual-licensed** (AGPL-3.0-or-later **or** a commercial license — see
[LICENSING.md](LICENSING.md)). To keep that model viable, we require a **DCO sign-off** on every
commit. Add `-s` to your commit:

```bash
git commit -s -m "fix: truncate pod logs at token budget"
```

This appends a `Signed-off-by:` trailer certifying you wrote the patch (or have the right to
submit it) under the [Developer Certificate of Origin](https://developercertificate.org/), and
that your contribution may be distributed under the project's licenses. If you can't or don't want
to agree to this, open a Discussion and we'll figure it out together.

---

## Commit & PR style

- Use [Conventional Commits](https://www.conventionalcommits.org/) where you can:
  `feat(kube-q): …`, `fix(server): …`, `docs: …`, `test: …`, `refactor: …`.
- Keep PRs focused — one logical change. Split unrelated refactors out.
- Describe the *why*, not just the *what*.

---

## Recognition

Every merged contributor is a real contributor, and **code is not the only kind that counts.**
These are credited by name in release notes with equal weight:

- Documentation, examples, and tutorials
- Bug reports with a reproduction that actually reproduces
- Issue triage and answering questions in Discussions
- Testing on a platform, distribution, or cloud the maintainer doesn't have
- Design feedback and RFC review
- Translations
- Playbooks and detectors

If you contributed and weren't credited, that's a mistake on our side — **open an issue and
we'll fix it.** You will not be the one being awkward.

Where this leads is written down in [GOVERNANCE.md](GOVERNANCE.md): there is an explicit
contributor ladder, and people are invited up it. The project has one maintainer today, so if
you want to own an area, that door is genuinely open.

Thank you for helping build a safer way to operate Kubernetes with AI. 💙
