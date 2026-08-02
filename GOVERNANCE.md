# KubeIntellect Governance

This document describes how decisions get made in KubeIntellect. It is deliberately lightweight —
the project is young — and will grow as the community grows.

## Roles

### Users
Anyone who runs KubeIntellect. Users contribute by filing issues, joining Discussions, sharing
use cases in *Show & Tell*, and helping answer questions. No commitment required — you're part of
the community the moment you show up.

### Contributors
Anyone who has a merged pull request, an accepted design/RFC, a triaged issue, or meaningful docs
work. Contributors are credited in release notes.

### Maintainers
Contributors trusted with review and merge rights. Maintainers:
- Review and merge pull requests.
- Triage issues and shepherd RFCs.
- Cut releases and keep CI green.
- Uphold the [Code of Conduct](CODE_OF_CONDUCT.md) and the project's design principles.

New maintainers are invited by existing maintainers based on a sustained track record of
high-quality contributions and good community judgment.

### Lead maintainer / BDFL (for now)
KubeIntellect was created by **Mohsen Seyedkazemi Ardebili** and is currently led by the author,
who also holds the copyright that makes the dual-license model possible (see below). The lead
maintainer has final say on direction and on decisions where consensus can't be reached — a role
we intend to dilute into a maintainer group as the project matures.

## How decisions are made

We prefer **lazy consensus**: propose a change (issue, PR, RFC, or Discussion); if no one objects
within a reasonable window, it's accepted. Disagreements are resolved by discussion first.

- **Small changes** (bug fixes, docs, tests): a single maintainer review + green CI is enough.
- **Substantial changes** (new agents, new subsystems, protocol/interface changes, anything that
  affects the safety model): open an **RFC** issue or a Discussion first. These get broader review
  and a maintainer sign-off before implementation.
- **Architecture decisions** are recorded as ADRs in the developer docs so the *why* is preserved.

## Design principles are non-negotiable

Contributions are held to the project's core principles — most importantly:

1. **Every mutating cluster action pauses for explicit human approval, gated by RBAC.** The HITL
   safety gate is a hard invariant, not a feature toggle.
2. **Earn every layer of complexity** — a new tool beats a new agent; a new agent beats a new
   framework.
3. **Least privilege and untrusted-input handling** for all cluster-derived data.

A change that violates these will be asked to change, no matter how useful it otherwise is.

## Licensing & contributions

KubeIntellect is **dual-licensed** (AGPL-3.0-or-later **or** commercial — see
[LICENSING.md](LICENSING.md)). To keep the commercial option available, contributions are accepted
under a **Developer Certificate of Origin (DCO)** sign-off (`git commit -s`), which certifies you
have the right to submit the code and agree it may be distributed under the project's licenses.
See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Changing this document

This governance model will evolve. Propose changes via a pull request to `GOVERNANCE.md`; changes
are adopted by maintainer consensus.
