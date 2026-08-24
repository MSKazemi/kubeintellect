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

## Knowing whether the stream was complete

A malformed or truncated SSE frame is counted, not raised — aborting an interactive answer on one
bad frame would be worse than delivering a partial one. But a caller that must be fail-closed
needs to know it happened, and the count is on the client after the stream ends:

```python
client = KubeQClient(url="http://localhost:8000")
for event in client.stream("why are my pods failing?"):
    ...

stats = client.last_stream_stats
if stats and not stats.lossless:
    raise RuntimeError(
        f"{stats.dropped_frames} frame(s) never reached me"
        f"{' and the stream ended mid-frame' if stats.truncated_tail else ''}")
```

`last_stream_stats` is set before the first event is yielded, so it is readable even if you
`break` out of the loop early — as the examples above do. A `WARNING` is also logged on
`kube_q.core.client`; until 2026-08-24 that log line was the only record and it was emitted only
when the loop ran to completion, so the documented `case FinalEvent(): break` pattern discarded
it, and `last_stream_stats` did not exist at all.

---

## Retry behaviour

Both `KubeQClient` and `AsyncKubeQClient` apply automatic retries for transient network errors (`httpx.TransportError`), waiting `2s, 5s, 10s` between four attempts. `stream()` retries only while it has yielded **no** events — once any event has reached you, a transport error is raised rather than retried, because a retry would deliver part of the stream twice.

How the two entry points end differs, and the difference is deliberate:

| | transport failure after every retry | HTTP 4xx / 5xx |
|---|---|---|
| `stream()` | raises the last `httpx.TransportError` | raises `httpx.HTTPStatusError` immediately |
| `query()` | returns `{"text": "", …}` — see its docstring | logs a warning and returns `{"text": "", …}` |

So a `stream()` that ends without raising means the server answered. Until 2026-08-24 that was true of `KubeQClient` only: `AsyncKubeQClient.stream` fell off the end of its retry loop, and a `return` from an async generator is a clean end-of-stream, so an unreachable server produced zero events and no exception. If you have code that treats an empty async stream as "nothing to report", it was reading a connection failure as an answer.
