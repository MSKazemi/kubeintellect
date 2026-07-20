# Python SDK

`kube_q.core` exposes a typed SDK you can use directly in scripts, notebooks, and other tools — no CLI required.

---

## Installation

```bash
pip install kube-q
```

---

## KubeQClient

```python
from kube_q.core.client import KubeQClient

client = KubeQClient(
    url="http://localhost:8000",
    api_key="your-key",           # optional
    ca_cert="/path/to/ca.pem",    # optional, for custom TLS
    timeout=120.0,
    model="kubeintellect-v2",
)
```

### Non-streaming query

```python
result = client.query(
    "why are my pods failing?",
    conversation_id="abc123",     # optional, for multi-turn
    user_id="alice",              # optional
)
print(result["text"])
```

### Streaming

```python
from kube_q.core.events import TokenEvent, StatusEvent, FinalEvent

for event in client.stream("list all deployments in default namespace"):
    match event:
        case TokenEvent(data=d):
            print(d.content, end="", flush=True)
        case StatusEvent(data=d):
            print(f"\n[{d.phase}] {d.message}")
        case FinalEvent():
            break
```

### Health check

```python
healthy = client.health()    # returns True / False
```

---

## AsyncKubeQClient

An async variant using `httpx.AsyncClient` for non-blocking use in web servers and async frameworks:

```python
import asyncio
from kube_q.core.client import AsyncKubeQClient
from kube_q.core.events import TokenEvent, FinalEvent

async def ask(question: str) -> str:
    async with AsyncKubeQClient(url="http://localhost:8000") as client:
        chunks = []
        async for event in client.stream(question):
            match event:
                case TokenEvent(data=d):
                    chunks.append(d.content)
                case FinalEvent():
                    break
        return "".join(chunks)

print(asyncio.run(ask("show all namespaces")))
```

---

## Event types

All backend events are modelled as a typed Pydantic discriminated union in `kube_q.core.events`:

| Event type | Key fields |
|---|---|
| `TokenEvent` | `data.content`, `data.role` |
| `StatusEvent` | `data.phase`, `data.message` |
| `ToolCallEvent` | `data.tool_name`, `data.args`, `data.call_id`, `data.dry_run` |
| `ToolResultEvent` | `data.call_id`, `data.ok`, `data.summary`, `data.truncated` |
| `HitlRequestEvent` | `data.action`, `data.risk`, `data.diff`, `data.approval_id` |
| `UsageEvent` | `data.prompt_tokens`, `data.completion_tokens`, `data.total_tokens`, `data.model` |
| `FinalEvent` | `data.content`, `data.usage`, `data.elapsed_ms` |
| `ErrorEvent` | `data.code`, `data.message`, `data.retryable` |

---

## Retry behaviour

Both `KubeQClient` and `AsyncKubeQClient` apply automatic retries with exponential back-off for transient network errors (`httpx.TransportError`). The retry schedule is `[1s, 3s, 5s]` before giving up and raising.

Non-retryable errors (4xx, 5xx) are raised immediately.
