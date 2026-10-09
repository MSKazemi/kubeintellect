# Historical v1 dependency licensing inventory

> **Archived reference, not a license for KubeIntellect.** This document preserves
> a historical inventory of dependencies from an earlier KubeIntellect release.
> Package terms and compatibility assertions are historical and should be
> independently verified. This document is not a current offer of separate

## Dependency License Summary

### Python libraries (incorporated into the application)


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


---

### CLI (separate repository)

| Package | License | Source |
|---------|---------|--------|
| `kube-q` (terminal CLI) | MIT | [github.com/MSKazemi/kube_q](https://github.com/MSKazemi/kube_q) · [pypi.org/project/kube-q](https://pypi.org/project/kube-q/) |


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
| **Langfuse** (self-hosted) | MIT | LLM trace viewer. Deployed as separate service. |
| **ingress-nginx** | Apache-2.0 | Kubernetes ingress controller. |

**SSPL note:** MongoDB and MeiliSearch use the Server Side Public License (SSPL-1.0). SSPL's copyleft obligation applies only to parties who make these databases available *as a service to third parties*. KubeIntellect operators deploy MongoDB and MeiliSearch for their own infrastructure — this is not a "service to third parties" under SSPL and does not trigger any SSPL obligations.

---

## License compatibility summary


