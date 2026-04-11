# KubeIntellect — License & Dependency Summary

KubeIntellect is available under a **dual-license model**:

| Use case | License required |
|----------|-----------------|
| Open-source projects, academic research, personal self-hosting (modifications published under AGPL-3.0) | **AGPL-3.0 — free** |
| Proprietary SaaS, embedded products, closed-source deployments | **Commercial license — required** |
| Revenue-generating services (annual revenue > USD 50,000 attributable to this software) | **Commercial license + revenue-sharing** |

---

## What requires a commercial license?

- Embedding KubeIntellect in a closed-source SaaS product
- Distributing KubeIntellect as part of a proprietary platform or managed service
- Using KubeIntellect internally at a company without publishing your modifications under AGPL-3.0
- Any use where annual revenue attributable to this software exceeds USD 50,000

## What does NOT require a commercial license?

- Self-hosting for your own team, with modifications published under AGPL-3.0
- Academic research and publication (citation required — see [CITATION.cff](CITATION.cff))
- Open-source projects that publish all modifications under AGPL-3.0
- Contributing back to this repository

## Revenue sharing

Commercial licensees whose annual revenue attributable to KubeIntellect exceeds USD 50,000 are required to negotiate a revenue-sharing agreement with the copyright holder.

## Contact

To inquire about a commercial license, open a GitHub issue with the label `commercial-license`, or contact via the repository's contact information.

---

## Dependency License Summary

### Python libraries (incorporated into the application)

These packages are installed as Python dependencies and run inside the KubeIntellect process. All are compatible with AGPL-3.0.

| Package | License | Notes |
|---------|---------|-------|
| `langchain`, `langgraph`, `langchain-openai`, `langchain-community`, `langchain-experimental` | MIT | LLM orchestration framework |
| `langsmith` | MIT | LangSmith tracing client |
| `langfuse` (SDK) | MIT | Langfuse tracing client |
| `openai` | MIT | OpenAI / Azure OpenAI Python client |
| `fastapi` | MIT | Web framework |
| `uvicorn` | BSD-3-Clause | ASGI server |
| `pydantic`, `pydantic-settings` | MIT | Data validation |
| `httpx`, `requests` | BSD-3-Clause / Apache-2.0 | HTTP clients |
| `python-dotenv` | BSD-3-Clause | `.env` file loader |
| `rich` | MIT | Terminal formatting |
| `mcp[cli]` | MIT | Model Context Protocol SDK |
| `kubernetes` | Apache-2.0 | Kubernetes Python client |
| `pymongo` | Apache-2.0 | MongoDB Python driver |
| `psycopg2-binary` | LGPL-3.0+ | PostgreSQL adapter (LGPL — linking exception applies; no source changes required) |
| `psycopg[binary,pool]` | LGPL-3.0+ | PostgreSQL adapter v3 (same) |
| `langgraph-checkpoint-postgres` | MIT | LangGraph PostgreSQL checkpointer |
| `prometheus-fastapi-instrumentator` | ISC | Prometheus metrics for FastAPI |
| `opentelemetry-api` | Apache-2.0 | OpenTelemetry tracing API |
| `pygithub` | LGPL-3.0 | GitHub REST API client (LGPL — linking exception applies) |
| `pyppeteer` | MIT | Headless browser automation |

**LGPL note:** `psycopg2`, `psycopg3`, and `PyGitHub` are LGPL-licensed. LGPL libraries can be used by AGPL software without triggering the LGPL's copyleft — the LGPL only requires that modifications *to the library itself* be released under LGPL. Using an LGPL library unchanged in an AGPL application is permitted.

---

### CLI (separate repository)

| Package | License | Source |
|---------|---------|--------|
| `kube-q` (terminal CLI) | MIT | [github.com/MSKazemi/kube_q](https://github.com/MSKazemi/kube_q) · [pypi.org/project/kube-q](https://pypi.org/project/kube-q/) |

`kube-q` is an independent MIT-licensed package. Its license is not affected by KubeIntellect's AGPL-3.0 license. Users and contributors can install and use `kube-q` freely.

---

### Deployed services (not incorporated into KubeIntellect source)

These run as independent containers/processes. KubeIntellect communicates with them over HTTP or a network socket — their source code is not incorporated into KubeIntellect's codebase.

| Service | License | Deployment notes |
|---------|---------|-----------------|
| **LibreChat** | MIT | Chat UI frontend. Source: [github.com/danny-avila/LibreChat](https://github.com/danny-avila/LibreChat). Deployed as a separate container; no source incorporation. |
| **PostgreSQL** | PostgreSQL License (permissive) | State store, HITL checkpoints, tool registry. |
| **MongoDB** | SSPL-1.0 | LibreChat chat history. SSPL only affects parties offering MongoDB *as a service to third parties*; self-hosting for your own deployment is not restricted. |
| **MeiliSearch** | SSPL-1.0 | LibreChat full-text search. Same SSPL note as MongoDB. |
| **Prometheus** | Apache-2.0 | Metrics collection. Deployed as separate service. |
| **Grafana** | AGPL-3.0 | Metrics dashboards. Same license family as KubeIntellect. |
| **Loki** | AGPL-3.0 | Log aggregation. Same license family as KubeIntellect. |
| **Langfuse** (self-hosted) | MIT | LLM trace viewer. Deployed as separate service. |
| **ingress-nginx** | Apache-2.0 | Kubernetes ingress controller. |

**SSPL note:** MongoDB and MeiliSearch use the Server Side Public License (SSPL-1.0). SSPL's copyleft obligation applies only to parties who make these databases available *as a service to third parties*. KubeIntellect operators deploy MongoDB and MeiliSearch for their own infrastructure — this is not a "service to third parties" under SSPL and does not trigger any SSPL obligations.

---

## License compatibility summary

All Python dependencies are MIT, BSD, Apache-2.0, or LGPL — all compatible with AGPL-3.0.
All deployed services are either permissive, AGPL-3.0 (same family), or SSPL with no impact on self-hosted deployments.

**KubeIntellect's AGPL-3.0 license applies to the KubeIntellect source code only.** It does not retroactively relicense any of the above dependencies or services.

---

*Copyright (C) 2026 Mohsen Seyedkazemi Ardebili. All rights reserved.*
