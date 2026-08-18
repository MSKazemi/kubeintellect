# Start here

Three groups of people arrive at this project wanting different things. Pick your route —
each is a short ordered path, not a list of everything.

If you are unsure which line of the code you want at all, read
[Which version do I want?](which-version.md) first. The short answer is **v4 to run it, v1 to
read the paper's architecture.**

---

## :material-flask: For scientists and researchers

You want to know what was measured, whether it reproduces, and how to cite it.

1. **[Research](research.md)** — the paper, the DOI, and the citation.
2. **[Which version do I want?](which-version.md)** — ⚠️ read this before running anything.
   The published architecture is **v1**; `v4/` is a deliberately different system. Reproducing
   the paper against v4 will not reproduce the paper.
3. **[Evaluation](evaluation.md)** — the harness, the metrics, and what the judge does and
   does not establish.
4. **[V2 vs V4 (models)](v2-vs-v4-models.md)** — the direct comparison behind the central
   claim that fewer agents beat more.
5. **[Architecture](architecture.md)** — the as-built component breakdown.

**The result worth knowing before you read anything else:** the capability-maximal design
(13 agents, ~100+ tools) performed *worse* than a lean coordinator. All four generations are
kept in the repository because that comparison is the evidence.

---

## :material-console: For engineers and SREs

You want it running against a real cluster, and you want to know what it will and will not do
to that cluster.

1. **[Quickstart](quickstart.md)** — shortest path to a first answer.
2. **[Install without a cluster](install/no-cluster.md)** — try it with no Kubernetes at all,
   then [Kind](install/kind.md) or an [existing cluster](install/existing-cluster.md).
3. **[How it works](how-it-works.md)** — the signal path, and why detection costs zero tokens.
4. **[What you can ask](capabilities.md)** and **[Examples & cookbook](examples.md)**.
5. **[CLI reference](cli-reference.md)** — the `kq` surface.
6. **[Autonomous operations](autonomy.md)** — the A0–A3 ladder, before you raise it above the
   default.
7. **[Troubleshooting](troubleshooting.md)** when something misbehaves.

**Read [Autonomous operations](autonomy.md) before granting write access.** The default is A1:
detectors may open an investigation, never apply a fix. That default is deliberate.

---

## :material-domain: For enterprise evaluators

You want to know the trust boundary, the audit story, the data path, and the licence — usually
in that order, and usually before a pilot is approved.

1. **[Security](security.md)** — RBAC scoping, the human-in-the-loop approval gate, and where
   the boundary is actually enforced (server-side at the mutating chokepoint, **not** in the
   prompt).
2. **[Data handling](data-handling.md)** — what leaves your network, and when.
3. **[Flight recorder](flight-recorder.md)** — the hash-chained decision log and `kq replay`,
   which is what makes an agent's actions auditable after the fact.
4. **[Autonomous operations](autonomy.md)** — the autonomy ladder as a hard cap, with
   protected namespaces pinned regardless of configuration.
5. **[Deploy](deploy/cloud.md)** — Helm on real infrastructure; also
   [AWS EKS](deploy/aws.md), [GCP GKE](deploy/gcp.md), [Alibaba Cloud](deploy/alibaba.md).
6. **[Operations guide](operations.md)** — running it day to day.

**Two things worth surfacing early in an evaluation**, because they usually come up anyway:
cluster context leaves your network unless you point the system at a self-hosted model, and
the software is **AGPL-3.0**. Neither is buried — see [Data handling](data-handling.md) and
the repository `LICENSING.md`.

---

## Everyone

- **[How it works](how-it-works.md)** — one page, one diagram, the whole signal path.
- **[FAQ](faq.md)** — including the questions that are awkward to answer.
- **[How it compares](comparison.md)** — versus k8sgpt and HolmesGPT.
- **[Glossary](glossary.md)** — the terms this project uses precisely.
