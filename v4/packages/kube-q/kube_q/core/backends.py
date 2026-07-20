"""
backends.py — Backend selection: kube-q server, direct OpenAI, or Azure OpenAI.

A ``BackendSpec`` captures everything the transport layer needs to dispatch a
request to the right place: base URL, chat path, auth scheme, health path, and
the model name to send. ``resolve_backend(cfg)`` turns a :class:`Config` into
a concrete spec.
"""

from __future__ import annotations

from dataclasses import dataclass

from kube_q.core.config import Config


@dataclass(frozen=True)
class BackendSpec:
    """Routing information for a single backend target."""
    kind:         str          # "kube-q" | "openai" | "azure"
    url:          str          # base URL (no trailing slash)
    api_key:      str | None
    chat_path:    str          # path (with query string if needed) for chat completions
    auth_scheme:  str          # "bearer" | "api-key" | "none"
    model:        str          # model name sent in payload
    health_path:  str | None   # None means "no health endpoint"
    label:        str          # human-readable name for logs / UI


def resolve_backend(cfg: Config) -> BackendSpec:
    """Produce a BackendSpec from the merged Config.

    The transport layer uses the spec exclusively — it does not read
    backend-specific fields from Config itself.
    """
    backend = (cfg.backend or "kube-q").lower()

    if backend == "openai":
        return BackendSpec(
            kind="openai",
            url=cfg.openai_endpoint.rstrip("/"),
            api_key=cfg.openai_api_key,
            chat_path="/v1/chat/completions",
            auth_scheme="bearer",
            # If user explicitly set --model, honour it; otherwise use openai_model.
            model=cfg.model if cfg.model != "kubeintellect-v2" else cfg.openai_model,
            health_path=None,
            label="OpenAI",
        )

    if backend == "azure":
        endpoint   = (cfg.azure_openai_endpoint or "").rstrip("/")
        deployment = cfg.azure_openai_deployment or ""
        api_ver    = cfg.azure_openai_api_version
        return BackendSpec(
            kind="azure",
            url=endpoint,
            api_key=cfg.azure_openai_api_key,
            chat_path=f"/openai/deployments/{deployment}/chat/completions?api-version={api_ver}",
            auth_scheme="api-key",
            # Azure ignores `model` in the body (the deployment selects the model),
            # but we still send it for cost-estimation + logging.
            model=cfg.model if cfg.model != "kubeintellect-v2" else deployment,
            health_path=None,
            label="Azure OpenAI",
        )

    # default: kube-q server
    return BackendSpec(
        kind="kube-q",
        url=cfg.url.rstrip("/"),
        api_key=cfg.api_key,
        chat_path="/v1/chat/completions",
        auth_scheme="bearer",
        model=cfg.model,
        health_path="/healthz",
        label="kube-q",
    )
