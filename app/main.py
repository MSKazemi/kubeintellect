"""KubeIntellect V2 — FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestLoggingMiddleware
from app.api.v1.router import api_router
from app.core.config import settings
from app.utils.logger import logger, setup_logging

# Configure logging before anything else so uvicorn handlers are patched early.
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import sys
    logger.info("KubeIntellect V2 starting up")
    from app.core.llm import get_coordinator_llm, get_subagent_llm
    get_coordinator_llm()
    get_subagent_llm()
    logger.info(f"LLM provider: {settings.LLM_PROVIDER}")
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
    yield
    logger.info("KubeIntellect V2 shutting down")
    from app.agent.workflow import close_graph
    await close_graph()
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

from app.api.v1.endpoints.health import router as health_router
app.include_router(health_router)          # /healthz — probe path (no version prefix)
app.include_router(api_router, prefix=settings.API_V1_STR)
