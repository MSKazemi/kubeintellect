"""KubeIntellect V2 — FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestLoggingMiddleware
from app.api.rate_limit import RateLimitMiddleware
from app.api.v1.router import api_router, public_router
from app.core.config import settings
from app.utils.logger import logger, setup_logging

# Configure logging before anything else so uvicorn handlers are patched early.
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import sys
    logger.info("KubeIntellect V2 starting up")
    # Surface the full version identity + active v5 experimental flags at boot (ADR-019).
    try:
        from app.core.version import version_line
        logger.info(version_line())
    except Exception:  # never let a diagnostic log break startup
        pass
    from app.core.llm import get_coordinator_llm, get_subagent_llm
    get_coordinator_llm()
    get_subagent_llm()
    logger.info(f"LLM provider: {settings.LLM_PROVIDER}")
    # A guard entry that cannot match protects nothing, and every parser here discards
    # silently. Logged loudly at startup and surfaced on GET /v1/v5/status; never fatal —
    # an operator's typo must not take the agent offline, only become impossible to miss.
    from app.core.config_audit import log_guard_config_problems

    log_guard_config_problems()
    # Fail fast, before the port opens. An operator who set REQUIRE_AUTH asked for exactly one
    # thing: that this server never serve an unauthenticated request. Discovering the
    # misconfiguration from a 401 in production is discovering it too late.
    if settings.REQUIRE_AUTH and not settings.auth_enabled:
        logger.error(
            "Startup failed: REQUIRE_AUTH=true but no API keys are configured.\n\n"
            "  Without keys every unauthenticated caller is treated as `admin` — full "
            "HITL-gated write access to the cluster.\n"
            "  Fix: set KUBEINTELLECT_ADMIN_KEYS / _OPERATOR_KEYS / _READONLY_KEYS "
            "(or DEMO_KEY_HMAC_SECRET), or unset REQUIRE_AUTH for local development."
        )
        sys.exit(1)
    from app.agent.workflow import init_graph
    try:
        await init_graph()
    except Exception as exc:
        _hint = _startup_hint(exc)
        logger.error(f"Startup failed: {exc}\n\n{_hint}")
        sys.exit(1)
    from app.db.audit import init_audit_pool
    try:
        await init_audit_pool()
    except Exception as exc:
        _hint = _startup_hint(exc)
        logger.error(f"Startup failed: {exc}\n\n{_hint}")
        sys.exit(1)
    # Flight recorder degrades gracefully — init_recorder never raises.
    from app.db.flight_recorder import init_recorder
    await init_recorder()
    from app.memory.service import init_memory
    await init_memory()
    # Singleton workers run on exactly ONE replica. Without this gate, scaling the Deployment
    # duplicates perception rather than sharing it — two watch streams, two engines firing the
    # same finding, two watchtowers acting on it against a live cluster. See app/core/leader.py.
    async def _start_singleton_workers() -> None:
        from app.detectors import service as _sensorium
        if not settings.SENSORIUM_ENABLED:
            _sensorium.record_disabled_by_flag()
            return
        try:
            await _sensorium.start_sensorium()
        except Exception as exc:
            # Swallowed on purpose — perception failing must never cost availability. But it is
            # recorded, because a swallowed start is an outage and `/v1/findings` and the digest
            # used to describe it as `SENSORIUM_ENABLED=false, or no compiled detectors loaded`.
            _sensorium.record_start_failure(exc)
            logger.warning(f"sensorium failed to start (continuing without): {exc}")

    async def _stop_singleton_workers() -> None:
        from app.detectors import service as _sensorium
        try:
            # Losing the lock is not a fault: this replica serves the API and another one
            # watches. Saying so is the difference between "normal" and "nothing is monitored".
            await _sensorium.stop_sensorium(
                _sensorium.STANDBY, "another replica holds the singleton lock"
            )
        except Exception as exc:
            logger.warning(f"sensorium failed to stop cleanly: {exc}")

    from app.core import leader as _leader

    dsn = settings.POSTGRES_DSN
    if settings.LEADER_ELECTION_ENABLED and dsn:
        election = _leader.LeaderElection(
            dsn,
            scope=settings.CLUSTER_ID or "",
            poll_seconds=settings.LEADER_ELECTION_POLL_SECONDS,
            on_acquire=_start_singleton_workers,
            on_lose=_stop_singleton_workers,
        )
        app.state.leader_election = election
        await election.start()
    else:
        # SQLite or election disabled: there is exactly one process by construction, so it leads.
        # Recorded explicitly rather than left implicit — /healthz must never imply an election
        # happened when none did.
        app.state.leader_election = None
        await _start_singleton_workers()
    # Everything above either succeeded or degraded deliberately — accept traffic.
    from app.core.readiness import set_ready

    set_ready(True)
    yield
    # Record that this process is no longer willing to serve. Note what this does NOT do:
    # uvicorn has already closed the listening socket by the time it runs us, so no probe can
    # observe the 503 and this cannot drain a rolling update. The chart's preStop sleep
    # (`drainSeconds`) is the mechanism that does. See app/core/readiness.py.
    set_ready(False)
    logger.info("KubeIntellect V2 shutting down (readiness now failing — draining)")
    from app.agent.workflow import close_graph
    await close_graph()
    # Workers FIRST, lock second. Releasing the lock first would let a standby acquire it and
    # start its own watch stream while this pod's is still running — briefly producing exactly
    # the duplicate perception the election exists to prevent.
    from app.detectors.service import stop_sensorium
    await stop_sensorium()
    _election = getattr(app.state, "leader_election", None)
    if _election is not None:
        await _election.stop()
    from app.memory.service import close_memory
    await close_memory()
    from app.db.flight_recorder import close_recorder
    await close_recorder()
    from app.db.audit import close_audit_pool
    await close_audit_pool()


def _startup_hint(exc: Exception) -> str:
    msg = str(exc).lower()

    # Database errors
    if "password authentication failed" in msg:
        return (
            "Fix: POSTGRES_PASSWORD in ~/.kubeintellect/.env does not match your postgres user.\n"
            "     Check the password, then run: kubeintellect status"
        )
    if "connection refused" in msg or "connection failed" in msg or "nodename nor servname" in msg:
        return (
            "Fix: postgres is not running or unreachable.\n"
            "     Option 1 — start with Docker:\n"
            "       docker run -d --name ki-pg \\\n"
            "         -e POSTGRES_USER=kubeuser -e POSTGRES_PASSWORD=<pass> \\\n"
            "         -e POSTGRES_DB=kubeintellectdb -p 5432:5432 postgres:16\n"
            "     Option 2 — use SQLite: add USE_SQLITE=true to ~/.kubeintellect/.env\n"
            "     Then re-run: kubeintellect db-init && kubeintellect serve"
        )
    if "does not exist" in msg and "database" in msg:
        return (
            "Fix: database has not been initialised yet.\n"
            "     Run: kubeintellect db-init"
        )
    if "role" in msg and "does not exist" in msg:
        return (
            "Fix: the postgres user/role does not exist.\n"
            "     Check POSTGRES_USER in ~/.kubeintellect/.env, or create the role:\n"
            "       createuser -h localhost -s kubeuser"
        )
    if "ssl" in msg:
        return (
            "Fix: SSL/TLS error connecting to postgres.\n"
            "     If your database requires SSL, add ?sslmode=require to DATABASE_URL.\n"
            "     Example: DATABASE_URL=postgresql://user:pass@host:5432/db?sslmode=require"
        )

    # LLM / API errors
    if "authenticationerror" in msg or "invalid api key" in msg or "incorrect api key" in msg:
        provider = settings.LLM_PROVIDER
        if provider == "openai":
            return (
                "Fix: OPENAI_API_KEY is invalid or expired.\n"
                "     Get a new key at https://platform.openai.com/api-keys\n"
                "     Update OPENAI_API_KEY in ~/.kubeintellect/.env"
            )
        return (
            "Fix: AZURE_OPENAI_API_KEY is invalid or expired.\n"
            "     Azure Portal → your OpenAI resource → Keys and Endpoint → regenerate KEY 1\n"
            "     Update AZURE_OPENAI_API_KEY in ~/.kubeintellect/.env"
        )
    if "deploymentnotfound" in msg or "the api deployment" in msg:
        return (
            "Fix: the Azure deployment name does not exist in your resource.\n"
            "     Check AZURE_COORDINATOR_DEPLOYMENT and AZURE_SUBAGENT_DEPLOYMENT in\n"
            "     ~/.kubeintellect/.env — must match names in Azure AI Foundry → Deployments"
        )
    if "ratelimit" in msg or "rate limit" in msg or "429" in msg:
        return (
            "The LLM API rate limit was hit at startup.\n"
            "     Wait a moment and restart: kubeintellect serve\n"
            "     Check your quota at platform.openai.com or Azure Portal."
        )
    if "resourcenotfound" in msg or "404" in msg:
        return (
            "Fix: AZURE_OPENAI_ENDPOINT may be wrong — resource not found.\n"
            "     Check AZURE_OPENAI_ENDPOINT in ~/.kubeintellect/.env\n"
            "     Format: https://<resource-name>.openai.azure.com/"
        )

    return (
        "Run 'kubeintellect status' to check your configuration.\n"
        "    Config file: ~/.kubeintellect/.env"
    )


app = FastAPI(
    title="KubeIntellect V2",
    version="2.0.0",
    lifespan=lifespan,
)

# Middleware is applied in reverse order (last added = outermost).
# RequestLogging must wrap CORS so the request_id is set before CORS runs.
#
# The rate limiter is added FIRST, which makes it the INNERMOST of the three, and both
# neighbours are load-bearing (enterprise A16):
#   * inside CORS, so a 429 still carries Access-Control-Allow-Origin — outside it, a browser
#     client sees an opaque network error instead of the status that explains the rejection;
#   * inside RequestLogging, so a rejected request still appears in the access log — a limiter
#     nobody can see firing is a limiter nobody can operate.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

# ── Self-metrics ──────────────────────────────────────────────────────────────────────────────
# `prometheus-fastapi-instrumentator` has been a declared dependency for some time and nothing
# ever mounted it: there was no /metrics endpoint, and app/api/middleware.py already excluded
# /metrics from request logging as though one existed. The control plane that watches everyone
# else's workloads was the one workload nobody could see — no request rate, no latency
# histogram, no error ratio, so an operator could not answer "is KubeIntellect itself healthy?"
# with anything better than a liveness probe that checks nothing by design.
#
# Unauthenticated on purpose: Prometheus scrapes it, and a scrape target that needs a bearer
# token is a target that silently stops being scraped when the token rotates. It exposes request
# counts and latencies only -- no cluster data, no prompts, no credentials. Restrict it with the
# NetworkPolicy, which is the layer that can actually express "only the monitoring namespace".
if settings.METRICS_ENABLED:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        should_group_status_codes=False,      # 401 vs 403 vs 500 are different operational facts
        excluded_handlers=["/metrics", "/healthz", "/readyz"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

from app.api.v1.endpoints.health import router as health_router

app.include_router(health_router)          # /healthz — probe path (no version prefix)
app.include_router(public_router, prefix=settings.API_V1_STR)  # /v1/healthz, /v1/readyz
app.include_router(api_router, prefix=settings.API_V1_STR)
