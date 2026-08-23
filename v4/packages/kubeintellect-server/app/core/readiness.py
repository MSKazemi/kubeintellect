"""Readiness state — deliberately local-only, so `/readyz` can never cascade.

Kubernetes asks two different questions and they need two different answers:

* **liveness** — "is this process wedged?" → restart it. `/healthz` answers this and checks
  nothing on purpose: a liveness probe that touches a database turns one database blip into a
  cluster-wide restart loop.
* **readiness** — "should this replica receive traffic *right now*?" → route or drain.

**This module does not drain a rolling update, and must not be relied on to.** It once
claimed to: the theory was that SIGTERM flips this flag, `/readyz` starts answering 503, and
Kubernetes stops routing before the pools close. Probing a real server through a real SIGTERM
disproved it (2026-08-19). uvicorn closes its listening socket first and runs the application's
shutdown hook — where ``set_ready(False)`` lives — only after in-flight work finishes, so from
the outside the transition is 200 → ECONNREFUSED. The 503 window is unobservable, and a request
arriving on a not-yet-updated kube-proxy route is *refused* rather than served, which is worse
than the problem the flag was introduced to fix.

What actually holds the socket open across that propagation gap is the chart's ``preStop``
sleep (``drainSeconds``), because Kubernetes runs preStop *before* SIGTERM. Readiness only
governs a running pod; a terminating pod is dropped from EndpointSlices by deletion itself,
whatever its probe says. The flag is kept because it is the honest answer to "is this process
willing to serve" for any in-process caller — not as a traffic-control mechanism.

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
