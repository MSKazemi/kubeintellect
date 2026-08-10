# Adopters

Who is using KubeIntellect, and for what.

This file exists because "is anyone actually running this?" is the first question a
platform team asks before adopting a young project — and because knowing *how*
people run it is what tells the maintainers which parts to harden next. Adding
yourself is the single lowest-effort, highest-value contribution to the project.

> **Status: none listed yet.** KubeIntellect went public recently, so this list is
> honestly empty rather than padded. If you're running it — even a Kind cluster on
> a laptop — you would be the first entry.

## The list

<!-- Keep alphabetical by name. Add a row; do not reformat other rows. -->

| Organization / person | Since | Environment | How it's used | Contact |
|---|---|---|---|---|
| _(you could be here)_ | | | | |

**Columns**

- **Organization / person** — a company, a team, a university group, or just your
  GitHub handle. Individuals are as welcome as companies.
- **Since** — roughly when you started (`2026-08` is precise enough).
- **Environment** — e.g. `Kind (laptop)`, `EKS, 3 clusters, ~400 pods`, `on-prem
  k3s`, `GKE staging only`. Approximate is fine; scale is the useful part.
- **How it's used** — e.g. `incident triage`, `on-call assistant`, `teaching RCA`,
  `evaluating for production`. "Evaluating" is a perfectly good answer.
- **Contact** — optional. A GitHub handle is enough; leave blank if you'd rather not.

## How to add yourself

Pick whichever is less friction for you — both are equally welcome:

1. **Open a PR** editing this file. It is one table row, it needs no tests, and it
   is a completely legitimate first contribution to the project.
2. **Comment on [Who's using KubeIntellect? (#51)](https://github.com/MSKazemi/kubeintellect/issues/51)**
   with the same information and a maintainer will add the row for you.

If you'd rather not be listed publicly but still want the maintainer to know the
project is used somewhere real, email **mohsen.seyedkazemi@gmail.com** — an
anonymous "a mid-size fintech runs this on EKS" is still useful signal, and nothing
is published without your say-so.

## What listing does and does not mean

- It **does not** transfer any rights, imply endorsement of your organization by the
  project, or endorsement of the project by your organization.
- It **does not** create any support obligation in either direction. See
  [SUPPORT.md](SUPPORT.md) for what support actually looks like.
- It **does** mean maintainers weight your environment when deciding what to test,
  what to keep backward-compatible, and what to deprecate. Listed environments are
  the ones that get protected.
- Remove yourself at any time by opening a PR that deletes your row. No explanation
  needed.

## Just want to signal interest?

You don't have to be running it in production to be counted:

- ⭐ **Star the repo** — the crudest signal, but the one other engineers actually
  look at when deciding whether a young project is worth their evening.
- 👍 **React on a [roadmap item](https://github.com/MSKazemi/kubeintellect/labels/roadmap)**
  — reactions are how the roadmap gets reordered.
- 💬 **Say what would make you adopt it** in
  [Discussions](https://github.com/MSKazemi/kubeintellect/discussions/categories/ideas).
  "I'd use this if it supported X" is more actionable than a feature request,
  because it comes with the reason attached.
