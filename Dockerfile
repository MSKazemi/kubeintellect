# syntax=docker/dockerfile:1.7
# ── Stage 1: dependency builder ───────────────────────────────────────────────
# ghcr.io/astral-sh/uv bundles Python 3.12 + uv — no separate pip install needed
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Build-time native deps (stays only in this stage, not the final image)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev

# Install Python dependencies from lockfile
# Bind-mount avoids copying lock files into a layer; cache mount reuses the uv download cache.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Copy only the application package (keeps the layer small)
COPY app ./app
COPY pyproject.toml uv.lock ./

# Install the project itself into the virtualenv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user — security best practice
RUN groupadd --gid 1001 app \
 && useradd  --uid 1001 --gid app --shell /bin/sh --no-create-home app

WORKDIR /app
RUN chown app:app /app

# Copy only the virtualenv and source — no uv, no build tools, no lock files
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app   ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# Liveness probe — matches the /health endpoint in app/main.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
        || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--log-level", "info"]
