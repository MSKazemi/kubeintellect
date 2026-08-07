# Getting help with KubeIntellect

There is one maintainer and no support contract, so the fastest way to get help is
to pick the right channel and give enough detail to reproduce the problem. Nothing
here is a formality — every channel below is read.

## Pick a channel

| I want to… | Go here |
|---|---|
| **Try it without installing anything** | [kubeintellect.com/demo](https://kubeintellect.com/demo) — read-only, shared demo cluster |
| **Ask "how do I…?" or "is this supposed to…?"** | [Q&A discussion](https://github.com/MSKazemi/kubeintellect/discussions/categories/q-a) |
| **Report something broken** | [Bug report](https://github.com/MSKazemi/kubeintellect/issues/new?template=bug_report.yml) |
| **Request a feature or an integration** | [Feature request](https://github.com/MSKazemi/kubeintellect/issues/new?template=feature_request.yml) |
| **Propose a design change** | [RFC](https://github.com/MSKazemi/kubeintellect/issues/new?template=rfc.yml) — discuss before implementing |
| **Show what you built / where you run it** | [Show and tell](https://github.com/MSKazemi/kubeintellect/discussions/categories/show-and-tell) |
| **Tell us you're using it** | [Who's using KubeIntellect?](https://github.com/MSKazemi/kubeintellect/discussions/categories/show-and-tell) and [`ADOPTERS.md`](ADOPTERS.md) |
| **Report a security vulnerability** | **Privately** — [open a security advisory](https://github.com/MSKazemi/kubeintellect/security/advisories/new). See [SECURITY.md](SECURITY.md). Never in a public issue. |
| **Ask about commercial licensing** | mohsen.seyedkazemi@gmail.com — see [LICENSING.md](LICENSING.md) |

## Read these first

Most first-run problems are covered already:

- **[v4 README](v4/README.md)** — every install path (browser, CLI-only, local Kind, Docker Compose, existing cluster)
- **[v4 docs](v4/docs/)** — install, quickstart, configuration, architecture, security, CLI reference
- **[Troubleshooting](v4/docs/troubleshooting.md)** and **[FAQ](v4/docs/faq.md)**
- **[ROADMAP.md](ROADMAP.md)** — including what the project deliberately *will not* do

## What to include when you ask

A question with this attached usually gets answered in one round-trip. Without it,
the first reply is just going to ask for it.

```bash
kq --version
kubectl version --short
python3 --version
```

Plus:

- **How you installed it** — `pip`, `snap`, container image, or from source
- **Which generation** — `v4/` is the supported line (`v1/`–`v3/` are frozen; see [ROADMAP.md](ROADMAP.md))
- **Which backend** — the KubeIntellect server, OpenAI, Azure OpenAI, or an OpenAI-compatible endpoint
- **What you ran, what you expected, what happened** — the exact command and the
  actual output, in a code block rather than a screenshot
- **Logs** — re-run with `kq --debug` (or `KUBE_Q_LOG_LEVEL=DEBUG`), and server logs if you self-host

> **Redact before you paste.** Cluster output frequently contains node names,
> namespaces, image registries, and occasionally secrets. Nothing you paste into a
> public issue can be un-published.

## What to expect

- This is a volunteer, single-maintainer project. Best effort, no SLA. Issues are
  usually triaged within a week; a quiet issue is a backlog, not a rejection.
- Bug reports that include a reproduction get looked at first, because they can be.
- Questions answered in Q&A get marked as the accepted answer so the next person
  finds them.
- Bug reports against `v1/`–`v3/` are closed as won't-fix by design — those
  generations are frozen to keep the published paper reproducible.

## Want to help instead of asking?

Answering someone else's Q&A question is a real contribution and counts toward the
contributor ladder in [GOVERNANCE.md](GOVERNANCE.md). If you'd rather write code,
start with a [`good first issue`](https://github.com/MSKazemi/kubeintellect/labels/good%20first%20issue)
and [CONTRIBUTING.md](CONTRIBUTING.md).
