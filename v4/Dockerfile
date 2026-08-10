# syntax=docker/dockerfile:1.7
#
# Build:
#   docker build -t kubeintellect:latest .
#
# Override kubectl version at build time:
#   docker build --build-arg KUBECTL_VERSION=v1.33.0 -t kubeintellect:latest .
#
# CI — pass image metadata:
#   docker build \
#     --build-arg VERSION=$(git describe --tags --always) \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) \
#     -t kubeintellect:latest .
#
# Update KUBECTL_VERSION when a new minor release is available:
#   https://dl.k8s.io/release/stable.txt

# ── Stage 1: Python dependency builder ────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Native build deps — only in this stage, never in the final image
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev

# Layer 1: install third-party dependencies only (cached until a pyproject or the
# lockfile changes). The workspace member pyprojects are needed for resolution but
# their source is not — so only the manifests are copied here.
# --frozen:                treat uv.lock as immutable truth; fail if resolution would differ.
# --no-install-workspace:  install all third-party deps but none of the workspace
#                          packages themselves; their module dirs are copied flat into
#                          the final image and imported from the working directory
#                          (same cwd-import pattern the V2 image used for app/).
COPY pyproject.toml uv.lock ./
COPY packages/kubeintellect-server/pyproject.toml packages/kubeintellect-server/
COPY packages/kube-q/pyproject.toml packages/kube-q/
COPY packages/ki-protocol/pyproject.toml packages/ki-protocol/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --all-extras

# Layer 2: bring in the workspace source trees (copied to the final stage below).
COPY packages/kubeintellect-server/app packages/kubeintellect-server/app
COPY packages/kube-q/kube_q packages/kube-q/kube_q
COPY packages/ki-protocol/ki_protocol packages/ki-protocol/ki_protocol

# ── Stage 2: kubectl fetcher (curl never enters the runtime image) ─────────────
FROM debian:bookworm-slim AS kubectl-fetcher

# Pin the version; update when a new minor release ships.
# See: https://dl.k8s.io/release/stable.txt
ARG KUBECTL_VERSION=v1.32.4

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
 && curl -fsSL \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" \
        -o /tmp/kubectl.sha256 \
 && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod +x /usr/local/bin/kubectl \
 && rm -rf /var/lib/apt/lists/* /tmp/kubectl.sha256

# ── Stage 3: lean runtime image ───────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ARG VERSION=dev
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="KubeIntellect" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.description="AI-powered Kubernetes management" \
      org.opencontainers.image.source="https://github.com/MSKazemi/kubeintellect" \
      org.opencontainers.image.url="https://kubeintellect.com" \
      org.opencontainers.image.documentation="https://mskazemi.github.io/kubeintellect/" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# ca-certificates: TLS verification for Azure OpenAI, LangSmith, Langfuse, and similar APIs.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# kubectl binary only — no curl, no apt overhead in this stage
COPY --from=kubectl-fetcher /usr/local/bin/kubectl /usr/local/bin/kubectl

# Non-root user with a home directory.
# Home dir is required because kubectl_tool.py calls os.path.expanduser(KUBECONFIG_PATH).
# Without it, ~ resolves to / and KUBECONFIG=/.kube/config which fails.
RUN groupadd --gid 1001 app \
 && useradd --uid 1001 --gid app --create-home --shell /bin/sh app

WORKDIR /app
RUN chown app:app /app

# Copy virtualenv and the workspace module trees — nothing else.
# Modules resolve from the working directory (/app), not from site-packages.
COPY --from=builder --chown=app:app /build/.venv /app/.venv
COPY --from=builder --chown=app:app /build/packages/kubeintellect-server/app ./app
COPY --from=builder --chown=app:app /build/packages/ki-protocol/ki_protocol  ./ki_protocol
COPY --from=builder --chown=app:app /build/packages/kube-q/kube_q            ./kube_q

ENV PATH="/app/.venv/bin:$PATH" \
    HOME="/home/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# Docker Compose / local healthcheck.
# In Kubernetes, the Deployment manifest uses httpGet liveness/readiness probes instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" \
        || exit 1

# --no-access-log: RequestLoggingMiddleware already logs every request with
# structured fields and request IDs; uvicorn's access log would duplicate them.
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--log-level", "info", \
     "--no-access-log"]
