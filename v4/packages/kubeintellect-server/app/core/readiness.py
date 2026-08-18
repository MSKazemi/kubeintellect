"""Readiness state — deliberately local-only, so `/readyz` can never cascade.

Kubernetes asks two different questions and they need two different answers:

* **liveness** — "is this process wedged?" → restart it. `/healthz` answers this and checks
  nothing on purpose: a liveness probe that touches a database turns one database blip into a
  cluster-wide restart loop.
* **readiness** — "should this replica receive traffic *right now*?" → route or drain.

The gap this closes is the shutdown path. On a rolling update Kubernetes sends SIGTERM and
removes the pod from Endpoints at the same time, but that removal propagates asynchronously
through kube-proxy. Until this module existed the server kept answering 200 on its readiness
probe right up to process exit, so requests were still being routed to a replica that was
already tearing its connections down.

**Why this deliberately does not check dependencies.** A readiness probe that pings Postgres
looks more thorough and is more dangerous: when the shared database blips, *every* replica goes
unready simultaneously and the Service has no endpoints at all — converting a degraded system
into a total outage. Dependency health belongs in alerting, not in a probe that controls
routing. So readiness here reflects one local fact: is this process willing to serve.
"""

from __future__ import annotations

_ready = False


def set_ready(value: bool) -> None:
    """Mark this process as willing (or no longer willing) to receive traffic."""
    global _ready
    _ready = value


def is_ready() -> bool:
    """True once startup finished and shutdown has not begun."""
    return _ready
