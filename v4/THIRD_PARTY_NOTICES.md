# Third-Party Notices

KubeIntellect integrates with several third-party open-source projects. **Their source code
is not included in, vendored into, modified by, or relicensed by this repository.** They are
used only as:

- **external services**, deployed via their official container images / Helm charts
  (Prometheus, Grafana, Loki, Langfuse, PostgreSQL), and queried over their network APIs
  (PromQL, LogQL, HTTP), or as the managed target cluster (Kubernetes); and
- **libraries** linked as normal package dependencies, all under permissive licenses.

Each component remains under its own license, held by its respective copyright holders.

## External services (deployed as separate processes / queried via API)

| Component | How KubeIntellect uses it | Upstream license |
|---|---|---|
| Kubernetes / `kubectl` | Managed cluster + CLI tool invoked as a subprocess | Apache-2.0 |
| Prometheus | Metrics queried via PromQL HTTP API | Apache-2.0 |
| Grafana | Optional dashboards (official image; not modified) | AGPL-3.0 |
| Loki | Logs queried via LogQL HTTP API (official image; not modified) | AGPL-3.0 |
| Langfuse | Optional LLM tracing via its SDK/API | MIT |
| PostgreSQL | Memory/audit persistence via the wire protocol | PostgreSQL License |

> **Note on Grafana/Loki (AGPL-3.0):** these are run as unmodified, independent services.
> KubeIntellect does not copy, modify, or statically/dynamically link their source, so their
> AGPL obligations are not triggered by this project. (KubeIntellect itself is independently
> licensed under AGPL-3.0-or-later; see [LICENSE](LICENSE).)

## Libraries (linked dependencies — all permissive)

| Library | Purpose | License |
|---|---|---|
| LangChain / LangGraph / langchain-core / langchain-openai | Agent orchestration + model clients | MIT |
| FastAPI | HTTP/SSE server | MIT |
| Uvicorn | ASGI server | BSD-3-Clause |
| Pydantic / pydantic-settings | Config + schema validation | MIT |
| httpx | HTTP client | BSD-3-Clause |
| asyncpg | PostgreSQL driver | Apache-2.0 |
| Langfuse SDK | LLM tracing client | MIT |

## Model & API providers

KubeIntellect calls hosted LLM APIs; it does not redistribute any model weights.

| Provider | Access | Terms |
|---|---|---|
| Alibaba Qwen / DashScope (OpenAI-compatible) | Hosted API (Qwen Cloud) | Alibaba Cloud / DashScope service terms |
| Azure OpenAI / OpenAI | Hosted API | Provider service terms |
| Anthropic (optional, Cortex) | Hosted API | Provider service terms |

Users are responsible for complying with the terms of whichever provider they configure via
`LLM_PROVIDER`.

---

_This file is informational and is not a substitute for the full license texts of the
components listed above. Where a component is redistributed (e.g. bundled container images),
its own license and notices travel with it._
