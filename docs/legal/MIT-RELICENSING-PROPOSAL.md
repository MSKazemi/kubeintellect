# Proposed MIT relicensing — NOT EFFECTIVE

> **DRAFT FOR RIGHTS REVIEW.** This file is a proposal, not a grant of rights and not a change to the license of any code. The current `LICENSE` files and release terms remain unchanged. Do not merge a change replacing them until the conditions below have been met.

## Decision under consideration

Use the standard **MIT License** for a future release of KubeIntellect, to make research, teaching, contributions, deployment, modification, redistribution, and commercial use straightforward.

**MIT is not noncommercial or research-only.** It explicitly permits commercial reuse, proprietary derivatives, sublicensing, and sales, provided its notice is retained. The project may *request* academic citation, but citation cannot be added as a mandatory MIT licensing condition.

See [the text proposed for review](MIT-LICENSE-TEMPLATE.md).

## Current status

- The repository, including its earlier research implementation and later code generations, currently publishes **AGPL-3.0-or-later** license files and a separate commercial-license offer.
- The historical AGPL-licensed releases and permissions validly granted under them are **not retroactively withdrawn or converted** by any future change.
- The project contains outside contributors whose code may require their separate authorization to relicense.
- The University of Bologna has raised a question about IP ownership and licensing authority for research-fellowship work; this is **not yet resolved**. The project’s GitHub ownership or maintainer status does not itself settle economic copyright ownership.
- A MIT relicensing statement affecting code with uncertain or third-party ownership must not be published as though authorized.

## Conditions before making MIT the active license

- [ ] Determine the economic copyright holder(s) for the research-era v1 implementation, and obtain written authorization from the University of Bologna / authorized rights holder(s) as needed.
- [ ] Inspect the research fellowship agreement, applicable institutional policies, grants, existing agreements, previous disclosures, and relevant correspondence.
- [ ] Review source-code provenance across `v1/`, `v2/`, `v3/`, `v4/`, shared `deploy/`, `scripts/`, and distributable packages. A later rewrite can still be derivative of an earlier work; creation after the fellowship is not by itself proof of independent ownership.
- [ ] Inventory all outside code and contributors (merged pull requests, commits, copied code, dependencies, assets, documentation) and record any written permission to relicense each copyrightable contribution under MIT. Exclude/rewrite code without necessary rights rather than assuming silence means consent.
- [ ] Review any publisher, grant, sponsor, or third-party obligations. Publishing a paper does not alone settle rights in the accompanying source code.
- [ ] Have qualified Italian IP counsel review the planned change in light of the university's challenge.
- [ ] Explicitly document the exact commit, paths, and future release to which an MIT grant can lawfully apply, and the correct copyright notice(s).

## Once permissions are documented

Implement a *separate* activation PR, then:

1. Replace the root `LICENSE` and relevant nested or packaged `LICENSE` files with the exact, approved MIT text and accurately identified copyright holder(s). Do not add conditions contradicting MIT.
2. Review and update `LICENSING.md`, `v4/LICENSING.md`, `README.md`, versioned READMEs, `CONTRIBUTING.md`, `DCO.md`, `GOVERNANCE.md`, `llms.txt`, docs, `.github/FUNDING.yml`, and historical `v1/LICENSE-COMMERCIAL.md` without rewriting old license history.
3. Update wheel/package license metadata, license-file inclusion, classifiers, website, containers, Snap distribution, and any other currently published distribution claims. Verify the resulting source distributions and wheels include the correct license.
4. Keep the existing history intact. Clearly date the MIT transition and define its scope. Older valid AGPL grants remain effective for copies already covered.
5. Run documentation/build/packaging and CI checks; ensure license text and metadata agree before a new tagged release.

## Change-control policy

**This proposal intentionally does not replace any active `LICENSE` file**, does not claim sole ownership, and does not announce MIT as the current license. Keep this PR in draft until rights and contributor consent are verified.

This is a project planning record, not a substitute for legal review.
