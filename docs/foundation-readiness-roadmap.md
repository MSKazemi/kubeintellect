# KubeIntellect Foundation Readiness Roadmap

> Status: Draft operational plan  
> Primary target: CNCF Sandbox  
> Secondary target: LF AI & Data Sandbox, later if the AI-agent ecosystem fit becomes stronger than the Kubernetes/cloud-native fit  
> Last updated: 2026-05-11

## Purpose

This document turns the idea of submitting or donating **KubeIntellect** to an open-source foundation into an executable roadmap.

KubeIntellect should be positioned as community infrastructure, not only as a personal product or SaaS:

> **KubeIntellect is an open-source agentic Kubernetes operations framework for evidence-backed diagnosis, root-cause analysis, and human-approved safe remediation.**

The goal is to make KubeIntellect credible for platform engineers, SRE teams, Kubernetes operators, researchers, and AI-for-operations developers.

---

## Recommended foundation path

## 1. Primary target: CNCF Sandbox

CNCF Sandbox is the best first foundation target because KubeIntellect is Kubernetes-native and fits the cloud-native ecosystem around:

- Kubernetes operations
- observability
- automation and configuration
- orchestration and management
- security and compliance
- operator experience
- AI-assisted cloud-native operations

KubeIntellect should prepare for **CNCF Sandbox**, not CNCF Incubation yet. Incubation should be a later target after stronger adoption, independent maintainers, stable releases, production users, and wider governance.

## 2. Secondary target: LF AI & Data

LF AI & Data can become relevant if KubeIntellect evolves into a broader AI-agent infrastructure platform beyond Kubernetes. For now, the Kubernetes/cloud-native identity is stronger.

## 3. Later option: Apache Incubator

Apache Incubator may be useful later if the project becomes a broader vendor-neutral platform, but it is not the best first target because the CNCF ecosystem is more directly aligned with Kubernetes operations.

---

## Strategic positioning

## One-sentence description

KubeIntellect is an agentic Kubernetes operations framework that converts natural-language incident questions into evidence-backed diagnosis, remediation plans, and optional human-approved safe actions.

## Tagline options

- Agentic RCA and safe remediation for Kubernetes.
- AI DevOps engineer for Kubernetes, with human approval for risky actions.
- Evidence-backed Kubernetes troubleshooting through LLM-orchestrated tools.

## Avoid weak positioning

Avoid describing KubeIntellect only as:

- “ChatGPT for Kubernetes”
- “a chatbot for kubectl”
- “an AI wrapper around kubectl”
- “a private commercial SaaS”

These descriptions understate the architecture and reduce foundation credibility.

---

## Current strengths

KubeIntellect already has several foundation-relevant signals:

- Public GitHub repository.
- Clear Kubernetes operational focus.
- Documentation site and website.
- Browser demo and CLI path.
- FastAPI + LangGraph architecture.
- Human-in-the-loop approval gate for destructive actions.
- Read-only demo mode.
- Authentication roles: admin, operator, readonly.
- Prometheus, Grafana, Loki integration direction.
- PostgreSQL/SQLite persistence.
- v1 legacy branch preserved.
- PyPI distribution.

---

## Main gaps before foundation submission

- Governance is not yet foundation-grade.
- Contributor onboarding needs to be explicit.
- Maintainer model needs to be documented.
- License strategy needs deliberate review because the repo currently advertises AGPL-3.0 plus commercial licensing, while CNCF-style adoption often benefits from permissive licensing.
- Security model needs a dedicated threat model and vulnerability disclosure flow.
- Evidence/adoption needs to be visible.
- Evaluation benchmark needs to be reproducible.
- Project scope and non-goals need to be precise.
- Community channels need to exist outside private conversations.
- Foundation proposal materials need to be prepared.

---

## Readiness milestones

| Milestone | Target outcome | Readiness meaning |
|---|---|---|
| M0 — Repository hygiene | Repo is easy to understand and contribute to | New users can install, run, test, and understand the project |
| M1 — Governance baseline | Roles and decisions are documented | Contributors know how work is accepted and maintainers are promoted |
| M2 — Security baseline | Safety, threat model, and disclosure are documented | Operators understand the risk model before using it on clusters |
| M3 — Evaluation benchmark | Fault-injection benchmark is reproducible | Claims are evidence-backed, not marketing-only |
| M4 — Community activation | Discussions, issues, demos, and roadmap are public | Project can attract contributors and users |
| M5 — Foundation package | CNCF-style proposal is ready | Submission can happen without rewriting core materials |

---

# Phase 0 — Repository hygiene and public trust

## Goal

Make KubeIntellect look like a serious open-source infrastructure project.

## Action items

- [ ] Confirm `main` contains the current v2 architecture.
- [ ] Add or update `CONTRIBUTING.md`.
- [ ] Add or update `CODE_OF_CONDUCT.md`.
- [ ] Add or update `SECURITY.md`.
- [ ] Add `GOVERNANCE.md`.
- [ ] Add `MAINTAINERS.md`.
- [ ] Add or update `ROADMAP.md`.
- [ ] Add `ADOPTERS.md` or `USERS.md`.
- [ ] Add `PROJECT_SCOPE.md` with scope and non-goals.
- [ ] Add `RELEASE.md` or release-process docs.
- [ ] Add this roadmap to the docs navigation.
- [ ] Link governance, security, roadmap, benchmark, and contribution docs from the README.
- [ ] Ensure all demo claims are reproducible.
- [ ] Ensure screenshots, logo, and architecture diagrams are current.

## Acceptance criteria

- A new contributor can understand the project in less than 10 minutes.
- A new contributor can run tests locally.
- A platform engineer can understand the safety model before installing.
- A foundation reviewer can find governance, license, security, roadmap, and release process from the README.

---

# Phase 1 — Governance and community model

## Goal

Make KubeIntellect look like community infrastructure, not only a personal repository.

## Documents to create

## `GOVERNANCE.md`

Should include:

- project mission
- maintainer responsibilities
- contributor role
- reviewer role
- maintainer promotion criteria
- decision-making process
- lazy consensus policy
- conflict resolution
- release authority
- security decision authority
- trademark/domain ownership note
- how governance can be changed

## `MAINTAINERS.md`

Should include:

- current maintainers
- areas of responsibility
- GitHub handles
- how to become a maintainer
- expected response time guidance

## `PROJECT_SCOPE.md`

In scope:

- Kubernetes diagnostics
- evidence-backed RCA
- safe remediation planning
- human-approved cluster changes
- observability integrations
- evaluation scenarios for Kubernetes failures
- agent orchestration for operations

Out of scope for now:

- fully autonomous destructive operations without approval
- general-purpose chatbot unrelated to Kubernetes operations
- replacing Kubernetes RBAC
- replacing SRE judgment in production incidents
- proprietary-only SaaS features in the open-source core

## Acceptance criteria

- Governance does not depend on private knowledge.
- A contributor knows how to get a PR accepted.
- A future foundation reviewer can see how the project can become vendor-neutral.

---

# Phase 2 — License and IP review

## Goal

Remove legal ambiguity before foundation conversations.

## Current concern

The repository currently advertises **AGPL-3.0** plus commercial licensing. That may be reasonable for an open-core/commercial strategy, but it may create friction for CNCF-style adoption. Foundation submission will require careful legal/IP review.

## Options

| Option | Description | Pros | Cons |
|---|---|---|---|
| Keep AGPL-3.0 | Continue copyleft/commercial dual-license strategy | Protects commercial interests | May reduce enterprise/foundation adoption |
| Re-license to Apache-2.0 | Make the open-source core permissive | Stronger CNCF fit, easier enterprise adoption | Less license-based commercial leverage |
| Split core/extensions | Apache-2.0 core, commercial/private extensions | Best foundation path if done cleanly | Requires architecture and IP discipline |

## Recommended direction

Evaluate an **Apache-2.0 open core** with clear boundaries:

- open-source core: agent orchestration, safety gate, Kubernetes tools, benchmark, local deployment
- optional commercial layer: hosted SaaS, enterprise integrations, advanced policy packs, managed observability, team/admin features

## Action items

- [ ] Inventory all dependencies and licenses.
- [ ] Check whether generated code, copied snippets, or assets have unclear rights.
- [ ] Decide whether to keep AGPL or prepare an Apache-2.0 core.
- [ ] If re-licensing, confirm contributor consent requirements.
- [ ] Add `NOTICE` if needed.
- [ ] Add dependency license scanning to CI.
- [ ] Document commercial/open-source boundaries if open-core remains.

## Acceptance criteria

- License strategy is deliberate and documented.
- Dependency licenses are visible and checked in CI.
- No unclear copied assets or generated artifacts remain.

---

# Phase 3 — Security, safety, and threat model

## Goal

Make KubeIntellect credible for real cluster operations.

## Documents to create or improve

## `SECURITY.md`

Should include:

- vulnerability reporting method
- supported versions
- security response expectations
- sensitive data handling
- token/API-key handling
- Kubernetes credential handling
- safe demo guidance

## `docs/security/threat-model.md`

Should include:

- assets: kubeconfig, cluster state, secrets, logs, API keys, prompts, chat history, audit records
- trust boundaries: user, CLI, API server, LLM provider, Kubernetes API, Prometheus, Loki, database
- threats:
  - prompt injection through logs/events/resources
  - accidental destructive actions
  - over-privileged service account
  - leaked kubeconfig or API key
  - malicious workload logs influencing the agent
  - hallucinated commands
  - unsafe remediation plan
  - data exfiltration through LLM provider
  - RBAC bypass attempt
  - audit-log tampering
- mitigations:
  - read-only default mode
  - role-based API keys
  - Kubernetes RBAC least privilege
  - human approval for mutating verbs
  - dry-run before writes where possible
  - command allow/deny policy
  - evidence-first answer format
  - audit logging
  - secret redaction
  - separate production guidance

## `docs/security/safety-model.md`

Should include:

- read-only path
- operator path
- admin path
- HITL gate
- mutating verb policy
- dry-run policy
- evidence requirements before remediation
- production deployment warning

## Acceptance criteria

- Safety is documented as architecture, not marketing.
- KubeIntellect can answer: “Why is this safe to run near my cluster?”
- The docs clearly state what is not safe yet.

---

# Phase 4 — Reproducible benchmark and evidence

## Goal

Make technical claims defensible.

## Benchmark scope

Create a reproducible `benchmarks/fault-injection/` suite with Kubernetes failure scenarios.

Recommended initial scenarios:

1. CrashLoopBackOff due to bad command.
2. CrashLoopBackOff due to missing environment variable.
3. ImagePullBackOff due to wrong image tag.
4. Pod pending due to insufficient CPU/memory.
5. Pod pending due to unbound PVC.
6. Service selector mismatch.
7. ConfigMap missing.
8. Secret missing.
9. Readiness probe misconfiguration.
10. Liveness probe misconfiguration.
11. OOMKilled workload.
12. RBAC forbidden error.
13. NetworkPolicy blocks traffic.
14. Deployment rollout stuck.
15. Job backoff limit exceeded.
16. HPA scaling not triggered or metrics unavailable.

## Each scenario should contain

- `setup.yaml`
- `task.md`
- `expected_signals.md`
- `expected_root_cause.md`
- `expected_remediation.md`
- `cleanup.yaml`
- scoring rubric
- pass/fail criteria

## Evaluation dimensions

- correct root cause
- correct evidence collection
- correct remediation plan
- no unsafe command suggested without approval
- no fabricated evidence
- correct confidence calibration
- correct follow-up questions when evidence is insufficient
- recovery from ambiguous logs/events

## Baselines

Compare KubeIntellect against:

- tool-less LLM baseline
- single-agent kubectl-only baseline
- human-written runbook baseline where applicable

## Acceptance criteria

- Benchmark can be run by a new contributor.
- Results are reproducible.
- Claims in README and website point to benchmark data.
- Failure cases are documented honestly.

---

# Phase 5 — Community activation

## Goal

Create visible external traction before foundation application.

## Public channels

- [ ] Enable GitHub Discussions.
- [ ] Create discussion categories: Announcements, Q&A, Ideas, Show and Tell, Troubleshooting, Foundation readiness.
- [ ] Add issue templates.
- [ ] Add PR template.
- [ ] Add `good first issue` candidates.
- [ ] Add `help wanted` issues.
- [ ] Publish a contributor guide for adding a new failure scenario.

## Community growth actions

- [ ] Publish a blog post: “Agentic RCA for Kubernetes: why KubeIntellect exists.”
- [ ] Publish a benchmark post with reproducible scenarios.
- [ ] Publish a safety post focused on human approval and RBAC.
- [ ] Share the demo with Kubernetes/SRE communities carefully and transparently.
- [ ] Ask early users to open issues rather than sending private feedback only.
- [ ] Record a short demo video for one realistic failure scenario.
- [ ] Add a public roadmap board.

## Acceptance criteria

- External users can ask questions publicly.
- At least 5–10 issues exist that are understandable for contributors.
- At least 2–3 external feedback items are captured publicly.
- Documentation explains how to contribute scenarios, tools, and integrations.

---

# Phase 6 — CNCF Sandbox proposal package

## Goal

Prepare the materials required to apply without rushing.

## Create `docs/foundation/cncf-sandbox-proposal-draft.md`

The proposal draft should include:

- project name
- project description
- problem statement
- why it belongs in CNCF
- CNCF landscape category proposal
- relation to Kubernetes and CNCF projects
- why now
- architecture summary
- security model
- governance model
- maintainers
- license and IP status
- release process
- communication channels
- issue tracking
- website/docs/demo links
- user/adopter evidence
- roadmap
- risks and limitations
- requested CNCF support

## Suggested CNCF ecosystem fit

Possible categories:

- Orchestration and Management
- Automation and Configuration
- Observability
- Security and Compliance

Suggested primary framing:

> Orchestration and Management / Automation and Configuration, with strong observability and security-adjacent safety concerns.

## Action items

- [ ] Prepare proposal draft.
- [ ] Identify relevant CNCF TAGs and working groups.
- [ ] Ask for informal feedback before formal submission.
- [ ] Prepare a neutral project pitch.
- [ ] Prepare a 5-minute demo.
- [ ] Prepare a 1-page technical architecture summary.
- [ ] Prepare a 1-page safety model summary.
- [ ] Prepare evidence of users/adopters/experiments.

## Acceptance criteria

- Proposal can be reviewed by someone outside the project.
- The project can explain why CNCF is the right home.
- Legal, governance, safety, and scope questions have written answers.

---

# Issue plan

Create the following GitHub issues to execute this roadmap:

1. Foundation readiness: add governance, maintainer, and scope docs.
2. Foundation readiness: review license/IP strategy for CNCF compatibility.
3. Security readiness: document threat model and safety model.
4. Evaluation readiness: create reproducible fault-injection benchmark structure.
5. Community readiness: add issue templates, PR template, and contributor onboarding.
6. CNCF readiness: draft Sandbox proposal package.
7. README readiness: link governance, security, roadmap, benchmark, and contribution docs.
8. Adoption readiness: add adopters/users/evidence tracking.

---

# Definition of done before applying

KubeIntellect should not apply until the following are true:

- Governance, maintainers, contributing, security, roadmap, and scope docs exist.
- License/IP strategy is intentionally decided.
- Threat model and safety model are documented.
- Fault-injection benchmark is reproducible.
- README links to all foundation-critical documents.
- At least some community signals are public.
- A CNCF Sandbox proposal draft exists.
- The project can explain why it belongs in CNCF instead of being only a personal SaaS/product.

---

# Immediate next step

Start with three parallel workstreams:

1. **Governance baseline** — `GOVERNANCE.md`, `MAINTAINERS.md`, `PROJECT_SCOPE.md`.
2. **Security baseline** — `SECURITY.md`, threat model, safety model.
3. **Benchmark baseline** — `benchmarks/fault-injection/` with 3 initial scenarios.

These three make the project much more credible quickly and create the foundation for CNCF-readiness.
