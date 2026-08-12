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

### Reviewers
Contributors trusted with review authority in **one area** — for example detectors, the memory
subsystem, the `kq` CLI, deploy/Helm, or the docs. Reviewers get review requests for their area
routed to them automatically (see [`.github/CODEOWNERS`](.github/CODEOWNERS)) and can triage and
label issues. They do **not** need merge rights and are not expected to watch the whole project.

Reviewers are invited after several quality contributions in an area plus sound judgement shown
in review comments. This rung exists deliberately: owning one area is a far more realistic ask
than co-maintaining everything, and it is the normal route to maintainer.

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

### Stepping back, and what happens if the lead maintainer disappears

Written down now, while it is hypothetical and nobody is upset.

- **Anyone can step back at any time**, from any rung, with no justification owed. Say so in an
  issue or by email so review requests stop being routed to you. Stepping back is not a
  failure, and returning later is welcome.
- **Inactivity is not misconduct.** A maintainer or reviewer inactive for ~6 months may be moved
  to emeritus to keep review routing honest. Emeritus status is reversible on request and keeps
  all past credit.
- **If the lead maintainer becomes unreachable for 3+ months**, the remaining maintainers may
  act by consensus to keep the project alive — cut releases, merge fixes, and update this
  document. Today there are no other maintainers, which is exactly why recruiting them is the
  project's top priority.
- **If the project is ever abandoned**, the intent is to archive it publicly with a clear
  notice in the README rather than let it rot silently, and to say so on PyPI. The AGPL means
  the code cannot be taken away from you regardless.
- **Copyright and the dual license** stay with the author; that is a legal fact, not a
  governance lever, and it does not affect anyone's AGPL rights. See
  [LICENSING.md](LICENSING.md).

### Code of Conduct enforcement
Reports go to **mohsen.seyedkazemi@gmail.com** and are handled per
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). This is currently a single person, which is an
acknowledged weakness — if a report concerns the lead maintainer, escalate publicly in a
Discussion or via a GitHub abuse report; that path is legitimate and will not be held against
you.

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
[LICENSING.md](LICENSING.md)). **Contributing asks nothing of you** — no CLA, no DCO sign-off,
no copyright assignment. Your contribution is licensed under AGPL-3.0-or-later and **you keep
the copyright in it.**

The commercial licence covers the copyright holder's own code. It does **not** reach your
contribution, and nothing here asks you to let it. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[`DCO.md`](DCO.md).

## Changing this document

This governance model will evolve. Propose changes via a pull request to `GOVERNANCE.md`; changes
are adopted by maintainer consensus.
