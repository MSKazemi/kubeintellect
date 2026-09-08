# KubeIntellect Roadmap

_Last updated: 2026-08-06 · Maintained by [@MSKazemi](https://github.com/MSKazemi)_

This is a statement of **direction, not a set of commitments**. Nothing here is a promised
feature with a date. If something matters to you, say so in
[Discussions](https://github.com/MSKazemi/kubeintellect/discussions) — that is how items move
up this list.

Where the project is going is discussed in the open. Significant design decisions are recorded
as ADRs and RFCs before implementation, per [GOVERNANCE.md](GOVERNANCE.md).

---

## The one-line direction

Make it safe and boring to let an AI operate a Kubernetes cluster — by making every action it
takes **legible, gated, and auditable**, rather than by making the model more autonomous.

Everything below is judged against that. Features that increase capability while reducing
legibility do not get built, however impressive they demo.

---

## Now — shipped and supported

The `v4/` platform is the current line and the only one you should deploy.

- Conversational diagnosis correlating **kubectl + Prometheus + Loki**.
- **Human-in-the-loop approval gate** with RBAC (superadmin / admin / operator / readonly) on
  every mutating action, enforced server-side at the mutating chokepoint.
- **24 declarative failure playbooks** (`detect → investigate → remediate`), 20 of which compile
  to zero-token detectors.
- **Zero-token detection** — a detector engine over a kubectl `--watch` sensorium fires
  findings without invoking the LLM.
- **Flight recorder** — hash-chained decision log, `kq replay <session-id>`, non-zero exit on a
  broken chain.
- **Memory hierarchy** — episodes plus a bi-temporal knowledge graph.
- **Multi-provider LLMs** — `azure`, `openai`, `anthropic`, `qwen`, and any OpenAI-compatible
  endpoint via `OPENAI_BASE_URL`.
- Autonomy capped at **A1** by default (investigate, never auto-remediate).

## Next — actively being worked

These are concrete, scoped, and mostly open to contribution.

| Item | Status | Help wanted? |
|---|---|---|
| First-class **local / self-hosted LLM** provider (Ollama and friends) as a documented, tested path | Designed; `OPENAI_BASE_URL` already works, needs making first-class | [#17](https://github.com/MSKazemi/kubeintellect/issues/17) ✅ |
| **Additional observability backends** — Tempo, Pyroscope | Open | [#18](https://github.com/MSKazemi/kubeintellect/issues/18) ✅ |
| **Composable detectors** — AND / NOT / temporal windows | Open | [#21](https://github.com/MSKazemi/kubeintellect/issues/21) ✅ |
| **PromQL execution in playbook `detect` blocks** | Open | [#20](https://github.com/MSKazemi/kubeintellect/issues/20) ✅ |
| **Temporal KG ingesting nodes / events / PVCs** | Open | [#19](https://github.com/MSKazemi/kubeintellect/issues/19) ✅ |
| Grow the **playbook library** beyond 24 | Ongoing — a playbook is one YAML file; 18 → 23 in Aug 2026 via [#108](https://github.com/MSKazemi/kubeintellect/pull/108), [#112](https://github.com/MSKazemi/kubeintellect/pull/112), [#114](https://github.com/MSKazemi/kubeintellect/pull/114), [#127](https://github.com/MSKazemi/kubeintellect/pull/127) and [#128](https://github.com/MSKazemi/kubeintellect/pull/128) | [#13](https://github.com/MSKazemi/kubeintellect/issues/13) ✅ |

## Housekeeping — known debt, stated plainly

Not glamorous, but real, and each is a genuinely good first contribution.

- **`ruff format` is not enforced.** `ruff check` passes and runs in CI, but ~108 files are
  unformatted. The reformat needs to land as its own commit before the gate can be turned on.
- ~~**`mypy` is not clean** — 30 errors across 12 files.~~ **Cleared in v2.3.0.** The workspace
  type-checks with zero errors and `Types (mypy)` is a blocking CI gate. Configuration lives in
  `v4/pyproject.toml`; keep it at zero.
- **Worked examples missing for most `kq` subcommands** — [#15](https://github.com/MSKazemi/kubeintellect/issues/15).
- **The snap is built but not published.** `snap/snapcraft.yaml` produces a strict-confinement
  `kubeintellect` snap (amd64 + arm64) and CI builds and smoke-tests it on every relevant PR, but
  the store name is unregistered and the two `personal-files` plugs need an approved snap
  declaration before release. Build it yourself meanwhile — see
  [`snap/README.md`](snap/README.md). Blocked on the maintainer's store account, not on code.
- **`v1/`–`v3/` carry known-vulnerable dependencies.** They are frozen reference trees, not
  deployable software — see [SECURITY.md](SECURITY.md) for why they are not upgraded.

## Later — direction, not commitments

Deliberately vague, because these are not designed yet and pretending otherwise would be
dishonest.

- Deeper **predictive detection** (currently behind a default-off flag).
- Richer **postmortem narratives** over the flight recorder.
- Broader **cloud/K8s distribution** coverage in the deploy paths.
- Whatever the first serious production users tell us is missing — that feedback outranks
  everything in this section.

---

## Explicitly *not* planned

Saying no clearly is part of a roadmap. These are decisions, not oversights.

| Not doing | Why |
|---|---|
| **Autonomous remediation on by default** | The approval gate is the product. Auto-fix stays opt-in behind an explicit allowlist, capped by the autonomy ladder. |
| **Replacing your observability stack** | It queries Prometheus and Loki; it does not want to become them. |
| **Becoming a GitOps / CD pipeline** | Different problem, well-served by existing tools. |
| **Bypassing RBAC for convenience** | A "just let it run" mode is the single most requested shortcut and will not be added. `--auto-approve` exists for testing and is documented as unsafe in production. |
| **Reviving `v1/`–`v3/`** | Frozen for architectural lineage and to keep the published paper's results reproducible. Bug reports against them will be closed as won't-fix. |

---

## How to influence this

0. **👍 the "Next" items you want**, on the issues linked in the table above. The counts are the
   only prioritisation signal this project has, and they genuinely reorder the list —
   [#52](https://github.com/MSKazemi/kubeintellect/issues/52) is the index. A one-line comment
   saying *why* you need it outweighs ten reactions, because it says what "done" has to mean.
   And if you're running KubeIntellect anywhere,
   [#51](https://github.com/MSKazemi/kubeintellect/issues/51) is where to say so — listed
   environments are the ones that get tested against.
1. **Open a [Discussion](https://github.com/MSKazemi/kubeintellect/discussions)** describing the
   problem you have — not the feature you want. Problems are more useful than solutions.
2. For anything architectural, open an **RFC issue** (there is a template). Significant changes
   get discussed and recorded before implementation.
3. Pick up something labelled
   [`help wanted`](https://github.com/MSKazemi/kubeintellect/labels/help%20wanted) or
   [`good first issue`](https://github.com/MSKazemi/kubeintellect/labels/good%20first%20issue).

The project currently has **one maintainer**. That is the single biggest risk to everything
above, and the most valuable contribution anyone can make is becoming the second — see the
contributor ladder in [GOVERNANCE.md](GOVERNANCE.md).
