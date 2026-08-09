# Triage guide

How an issue or PR travels from "opened" to "closed", and what every label means.

This is written down for two reasons. Contributors deserve to know why their issue
has the labels it has and roughly when it will be looked at. And triage is one of
the most useful things a non-maintainer can do here — but only if the rules are
public. **You do not need commit access to triage.** Anyone can do most of the
steps below in a comment, and doing it well is a documented route up the
contributor ladder in [GOVERNANCE.md](GOVERNANCE.md).

## The flow

```
opened ──▶ needs-triage ──▶ accepted (area/* + kind/* + priority/*) ──▶ assigned ──▶ PR ──▶ closed
                │
                ├──▶ needs more info ──▶ (stale after 30d) ──▶ closed, reopenable
                ├──▶ duplicate / invalid / wontfix ──▶ closed with a reason
                └──▶ moved to Discussions (it was a question, not a bug)
```

Every new issue starts as `needs-triage`. Triage means answering five questions:

1. **Is it a question rather than a defect?** → answer it, or point it at
   [Discussions Q&A](https://github.com/MSKazemi/kubeintellect/discussions/categories/q-a),
   then close. Questions are welcome; they are just not issues.
2. **Is it reproducible?** → if the report has no version, no command, and no
   output, ask for the details in [SUPPORT.md § What to include](SUPPORT.md).
   Label `needs-info`.
3. **Which generation?** → bugs against `v1/`–`v3/` are closed as `wontfix` by
   design; those generations are frozen so the published paper stays reproducible.
   Say so kindly and link [ROADMAP.md](ROADMAP.md).
4. **Which area?** → add exactly one `area/*` label (below). This is what makes an
   issue findable by someone who knows that subsystem.
5. **Does it touch the safety model?** → if it touches HITL approval, the mutating
   chokepoint, or RBAC, add `kind/safety`. These get maintainer review regardless
   of size, and are never auto-merged.

Once those are answered, drop `needs-triage` and add `priority/*`.

## Labels and what they actually mean

**`area/*` — which subsystem.** Exactly one per issue.

| Label | Covers |
|---|---|
| `area/server` | `packages/kubeintellect-server` — API, config, core |
| `area/kube-q` | The `kq` CLI and its web terminal |
| `area/agents` | Agents, orchestration, playbooks, prompts |
| `area/detectors` | Detectors, sensorium, PromQL/LogQL signals |
| `area/memory` | Episodes, summaries, temporal knowledge graph |
| `area/deploy` | Helm, Kind, Compose, packaging, install paths |
| `area/eval` | Evaluation harness and benchmarks |

**`kind/*` — what sort of change.** Zero or more.

| Label | Meaning |
|---|---|
| `kind/integration` | A new backend, provider, or observability system |
| `kind/safety` | Touches HITL, RBAC, or the mutating chokepoint — highest review bar |

**`priority/*` — when.** At most one; absence means "normal".

| Label | Meaning in practice |
|---|---|
| `priority/high` | Data loss, a safety-gate bypass, a broken documented install path, or a security fix. Looked at before anything else. |
| _(none)_ | The normal backlog. Ordered by 👍 reactions and by whether a PR exists. |
| `priority/low` | Agreed as desirable, not scheduled. A PR is very welcome and will be reviewed. |

**Contributor-facing labels.**

| Label | Promise attached to it |
|---|---|
| `good first issue` | Self-contained, the file and line are named in the body, and the verification command is written out. If one of these turns out not to be small, that is a bug in the label — say so and it will be relabelled. |
| `help wanted` | The maintainer is not actively working on it and would merge a good PR. |
| `roadmap` | A [ROADMAP.md](ROADMAP.md) item. 👍 reactions on these directly reorder the roadmap. |
| `adoption` | Someone reporting where and how they run KubeIntellect — see [ADOPTERS.md](ADOPTERS.md). |
| `RFC` | A design proposal. Discuss and reach agreement *before* implementation. |
| `discussion` | Needs community input before anyone should start work. |
| `needs-triage` | Not yet reviewed by a maintainer. Removing this is the triage act. |
| `needs-info` | Waiting on the reporter. Closed after 30 quiet days, and reopened the moment the info arrives. |

## Claiming an issue

Comment "I'd like to take this." That's it — no permission needed, no assignment
ceremony. The maintainer will assign it to you so nobody duplicates your work.

If you go quiet for **two weeks** on an assigned issue, it gets unassigned so
someone else can pick it up. This is not a reprimand and you can re-claim it; life
happens, and a silently-blocked issue is worse for you than for the project.

Before starting anything **larger than a `good first issue`**, say what you plan to
do and wait for a 👍. Fifteen minutes of alignment is cheaper than a rewritten PR,
and the maintainer will tell you if there's a design constraint you can't see from
outside.

## PR review

- **Every PR gets a first response.** Even if the answer is "not this way", it
  arrives rather than silence.
- CI must be green: `ruff check`, `mypy`, both pytest suites (on Python 3.12 **and** 3.13),
  file modes, and syntax warnings. The exact commands are in
  [CONTRIBUTING.md § Quality gates](CONTRIBUTING.md#quality-gates--green-before-you-push),
  and `make setup` from the repo root runs all six for you. `mypy` **is** blocking as of
  v2.2.0 and the workspace sits at zero errors — if it reports something, it is from your
  change. `ruff format` remains known debt and is **not** a gate; a failure there is not
  your bug.
- Review looks at four things, in this order: does it preserve the safety model,
  is it tested, does it fit the design principles, is it documented.
- Commits need a DCO sign-off (`git commit -s`).
- A PR that only edits [ADOPTERS.md](ADOPTERS.md), docs, or a typo gets a fast lane.

## How things get closed

Closing is a decision with a reason attached, never a silent cleanup:

| Closed as | When |
|---|---|
| **Completed** | A PR merged, with the contributor credited in the release notes. |
| **Duplicate** | Linked to the original, which is where the discussion continues. |
| **Wontfix** | Out of scope — usually something in ROADMAP's "explicitly not planned" table, or a `v1/`–`v3/` bug. The reason is always stated. |
| **Stale** | `needs-info` with no reply for 30 days. Comment on it and it reopens. |
| **Moved** | It was a question or an idea; it now lives in Discussions with a link both ways. |

Disagree with a close? Say so on the issue. Reopening after new information is
normal and nobody has to be persuaded twice.

## Want to help with triage?

Pick any issue labelled
[`needs-triage`](https://github.com/MSKazemi/kubeintellect/labels/needs-triage)
and post a comment that answers the five questions at the top. Try to reproduce it
and say what happened. That is genuinely one of the highest-leverage contributions
available here, it needs no repo permissions, and it is credited in release notes
exactly like code is.
