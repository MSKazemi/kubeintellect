# app/main.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import time
import uuid as _uuid

from app.api.v1.routers import api_router as api_v1_router
from app.api.v1.debug_middleware import DebugRequestMiddleware
from app.core.config import settings
from app.utils.logger_config import setup_logging, request_id_var, mask_headers
from prometheus_fastapi_instrumentator import Instrumentator
logger = setup_logging(
    app_name="kubeintellect",
    log_level=settings.LOG_LEVEL,
    log_format=settings.LOG_FORMAT,
)
logger.info("Initializing KubeIntellect Application")


app = FastAPI(
    title="KubeIntellect OpenAICompatibleAPI_PoC",
    version="0.1.0",
    description="Proof of Concept for an OpenAI Compatible API for KubeIntellect, interfacing with Azure OpenAI.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.ALLOWED_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if settings.DEBUG:
    if settings.UNSAFE_LOG_REQUEST_BODIES:
        app.add_middleware(DebugRequestMiddleware)
        logger.warning(
            "UNSAFE_LOG_REQUEST_BODIES=true — request body logging is active. Do not use in production."
        )
    else:
        logger.debug(
            "DEBUG mode enabled (request body logging disabled — "
            "set UNSAFE_LOG_REQUEST_BODIES=true to enable, never in production)."
        )

# Expose /metrics for Prometheus scraping.
# Instruments all HTTP routes automatically (request count, latency histograms, error rate).
Instrumentator().instrument(app).expose(app)

# Include the v1 API router
app.include_router(api_v1_router, prefix=settings.API_V1_STR)

@app.get("/healthz", tags=["Health"])
async def liveness():
    """Liveness probe — confirms the process is running. Never checks dependencies."""
    return {"status": "ok"}


@app.get("/health", tags=["Health"])
async def readiness():
    """Readiness probe — confirms the app can serve traffic (checks PostgreSQL and Kubernetes API)."""
    from app.services.kubernetes_service import check_kubernetes_connectivity, KubernetesConfigurationError

    checks: dict = {}

    # PostgreSQL — probe via the LangGraph connection pool (psycopg3)
    try:
        from app.orchestration.workflow import _langgraph_pool
        if _langgraph_pool is None:
            checks["postgres"] = "error: pool not initialised"
        else:
            async with _langgraph_pool.connection() as conn:
                await conn.execute("SELECT 1")
            checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    # Kubernetes API
    try:
        result = check_kubernetes_connectivity(timeout_seconds=3, max_retries=1)
        checks["kubernetes"] = "ok" if result["status"] == "success" else f"error: {result['message']}"
    except KubernetesConfigurationError as e:
        checks["kubernetes"] = f"error: {e}"
    except Exception as e:
        checks["kubernetes"] = f"error: {e}"

    if not all(v == "ok" for v in checks.values()):
        raise HTTPException(status_code=503, detail={"status": "degraded", "checks": checks})

    return {"status": "ok", "checks": checks}

_HEALTH_PATHS = frozenset(["/healthz", "/health"])

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log HTTP requests with request-ID correlation, timing, and masked headers."""
    # Skip noisy health-check paths
    if request.url.path in _HEALTH_PATHS:
        return await call_next(request)

    req_id = f"req-{_uuid.uuid4().hex[:12]}"
    token = request_id_var.set(req_id)
    start = time.time()
    try:
        logger.info(
            "http_request method=%s path=%s",
            request.method,
            request.url.path,
            extra={"http_method": request.method, "http_path": request.url.path},
        )
        logger.debug(
            "http_request_headers %s",
            mask_headers(dict(request.headers)),
        )
        response = await call_next(request)
        elapsed_ms = int((time.time() - start) * 1000)
        logger.info(
            "http_response status=%s duration_ms=%d",
            response.status_code,
            elapsed_ms,
            extra={"http_status": response.status_code, "duration_ms": elapsed_ms},
        )
        # For SSE streams, wrap the body iterator to capture actual stream duration.
        # The http_response log above measures header-flush latency only (typically 1ms);
        # http_stream_complete below measures time until the last byte is sent.
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            _stream_start = time.time()
            _http_status = response.status_code
            _original_body = response.body_iterator

            async def _body_proxy():
                try:
                    async for chunk in _original_body:
                        yield chunk
                    _stream_ms = int((time.time() - _stream_start) * 1000)
                    logger.info(
                        "http_stream_complete status=%s duration_ms=%d disconnected=false error=false",
                        _http_status, _stream_ms,
                        extra={
                            "event": "http_stream_complete",
                            "http_status": _http_status,
                            "duration_ms": _stream_ms,
                            "disconnected": False,
                            "error": False,
                        },
                    )
                except Exception:
                    _stream_ms = int((time.time() - _stream_start) * 1000)
                    logger.error(
                        "http_stream_complete status=%s duration_ms=%d disconnected=true error=true",
                        _http_status, _stream_ms,
                        extra={
                            "event": "http_stream_complete",
                            "http_status": _http_status,
                            "duration_ms": _stream_ms,
                            "disconnected": True,
                            "error": True,
                        },
                    )
                    raise

            response.body_iterator = _body_proxy()
        return response
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.error(
            "http_request_failed after %dms: %s",
            elapsed_ms,
            e,
            exc_info=True,
            extra={"duration_ms": elapsed_ms},
        )
        raise
    finally:
        request_id_var.reset(token)

@app.on_event("startup")
async def startup_event():
    """Initialize the workflow system and log startup."""
    from app.orchestration.workflow import initialize_workflow_async
    logger.info("Initializing KubeIntellect workflow (AsyncPostgresSaver) …")
    await initialize_workflow_async()
    logger.info("Application startup complete")
    logger.info("Available routes:")
    for route in app.routes:
        if hasattr(route, 'methods') and route.methods:
            methods_str = f"[{', '.join(route.methods)}]"
        else:
            route_type = route.__class__.__name__
            methods_str = f"[{route_type}]"
        logger.info(f"  {route.path} {methods_str}")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutting down")
    from app.orchestration.workflow import close_langgraph_checkpointer
    await close_langgraph_checkpointer()








if __name__ == "__main__":
    import uvicorn
    # Note: For production, use a proper ASGI server like Gunicorn with Uvicorn workers
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level="debug")
