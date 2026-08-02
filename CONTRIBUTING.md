# Contributing to KubeIntellect

First off — **thank you.** KubeIntellect is an open-source, human-governed AI SRE for
Kubernetes, and it gets better every time someone files a bug, sharpens a doc, or ships a
feature. This guide gets you from clone to merged PR.

New contributors are welcome. If you're looking for a place to start, browse issues labeled
[`good first issue`](https://github.com/MSKazemi/kubeintellect/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
and [`help wanted`](https://github.com/MSKazemi/kubeintellect/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22).

- 💬 **Questions / ideas** → [GitHub Discussions](https://github.com/MSKazemi/kubeintellect/discussions)
- 🐛 **Bugs** → [open an issue](https://github.com/MSKazemi/kubeintellect/issues/new/choose)
- 🔒 **Security** → **do not** open a public issue; see [SECURITY.md](SECURITY.md)
- 📜 **Conduct** → all interaction is governed by our [Code of Conduct](CODE_OF_CONDUCT.md)

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
# Canonical repo (the org repo is canonical)
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

---

## Quality gates — green before you push

From the version directory you changed (usually `v4/`):

```bash
uv run pytest            # test suite (coverage is tracked; don't regress it)
uv run ruff check .      # lint + format check
uv run mypy src          # type check
```

A PR that fails these will fail CI. If a gate is failing for a reason unrelated to your change,
say so in the PR.

### PR checklist

- [ ] Change is scoped to `v4/` (or a version whose contributions are open)
- [ ] New behavior has both a happy-path **and** an error-path test
- [ ] **Every write/mutating operation keeps its dry-run + diff + human-approval (HITL) gate** — this is a safety requirement, not a UX choice
- [ ] Secret values are never logged or returned (key names only)
- [ ] `pytest`, `ruff check`, and `mypy` all pass locally
- [ ] Docs updated if behavior/CLI/flags changed
- [ ] Commits are signed off (DCO)

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

Every merged contributor is a real contributor. We credit contributors in release notes and on
the repo. Thank you for helping build a safer way to operate Kubernetes with AI. 💙
