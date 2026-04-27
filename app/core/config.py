from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ~/.kubeintellect/.env is written by `kubeintellect init` for laptop installs.
# The project-local .env (CWD) overrides it so developers can still override
# settings per-project without touching the global config.
_HOME_ENV = Path.home() / ".kubeintellect" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files in the tuple have higher priority.
        # Home env is loaded first; the CWD .env wins if both define the same key.
        env_file=(str(_HOME_ENV), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── API ───────────────────────────────────────────────────────────────────
    API_V1_STR: str = "/v1"

    # ── LLM provider ─────────────────────────────────────────────────────────
    LLM_PROVIDER: str = Field(default="azure")

    # Azure OpenAI
    AZURE_OPENAI_API_KEY: Optional[str] = None
    AZURE_OPENAI_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_API_VERSION: str = "2024-10-01-preview"  # enables automatic prefix caching
    AZURE_COORDINATOR_DEPLOYMENT: str = "gpt-4o"
    AZURE_SUBAGENT_DEPLOYMENT: str = "gpt-4o-mini"

    # OpenAI direct
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_COORDINATOR_MODEL: str = "gpt-4o"
    OPENAI_SUBAGENT_MODEL: str = "gpt-4o-mini"

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    # When DATABASE_URL is set (e.g. external managed DB), it takes precedence
    # over the individual POSTGRES_* vars below.
    DATABASE_URL: Optional[str] = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = "kubeintellect"
    POSTGRES_USER: str = "kubeintellect"
    POSTGRES_PASSWORD: str = "password"
    POSTGRES_POOL_MIN_CONN: int = Field(default=1, gt=0)
    POSTGRES_POOL_MAX_CONN: int = Field(default=10, gt=0)

    @property
    def POSTGRES_DSN(self) -> Optional[str]:
        if self.USE_SQLITE:
            return None
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ── SQLite fallback (local / no-Docker mode) ──────────────────────────────
    # Set USE_SQLITE=true in .env or auto-detected by `kubeintellect serve`.
    # Not used in Helm deployments — DATABASE_URL is always set there.
    USE_SQLITE: bool = False
    SQLITE_PATH: str = "~/.kubeintellect/kubeintellect.db"

    # ── Kubernetes ────────────────────────────────────────────────────────────
    KUBECONFIG_PATH: str = "~/.kube/config"
    KUBECTL_TIMEOUT_SECONDS: int = 30
    KUBECTL_DESTRUCTIVE_TIMEOUT_SECONDS: int = 300

    # Namespaces the kubectl tool will never touch, regardless of user role.
    # In Helm deployments this is set via config.blockedNamespaces in values.yaml
    # (injected as KUBECTL_BLOCKED_NAMESPACES env var by the ConfigMap).
    # For local dev, override in .env or ~/.kubeintellect/.env.
    KUBECTL_BLOCKED_NAMESPACES: str = Field(
        default="kubeintellect,monitoring,kube-system,kube-public,kube-node-lease,ingress-nginx,cert-manager"
    )

    # Resource types the kubectl tool will never access, regardless of user role.
    # In Helm deployments set via config.blockedResources in values.yaml.
    KUBECTL_BLOCKED_RESOURCES: str = Field(
        default="secret,secrets,serviceaccount,serviceaccounts"
    )

    @property
    def kubectl_blocked_namespaces(self) -> frozenset[str]:
        return frozenset(
            ns.strip()
            for ns in self.KUBECTL_BLOCKED_NAMESPACES.split(",")
            if ns.strip()
        )

    @property
    def kubectl_blocked_resources(self) -> frozenset[str]:
        return frozenset(
            r.strip()
            for r in self.KUBECTL_BLOCKED_RESOURCES.split(",")
            if r.strip()
        )

    # ── Observability ─────────────────────────────────────────────────────────
    # URLs of Prometheus and Loki running in the target cluster.
    # Empty = not configured; metric/log queries will return a "no data source" error
    # rather than crashing — the app remains fully functional for kubectl-based queries.
    PROMETHEUS_URL: str = ""
    LOKI_URL: str = ""

    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: Optional[str] = None
    LANGFUSE_SECRET_KEY: Optional[str] = None
    # Set by each deployment method: localhost:3001 (compose), in-cluster DNS (Helm).
    # Empty default keeps Langfuse disabled until a host is explicitly configured.
    LANGFUSE_HOST: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"
    DEBUG: bool = False
    ALLOWED_ORIGINS: str = "http://localhost:3080"

    # ── KubeIntellect behavior flags ───────────────────────────────────────────
    # Each behavior is feature-flagged so it can be toggled without a redeploy.
    KUBECTL_ERROR_HINTS_ENABLED: bool = True
    INVESTIGATION_PLAN_ENABLED: bool = True
    PLAYBOOKS_ENABLED: bool = True
    # off | lenient | strict
    SNAPSHOT_SUFFICIENCY_MODE: str = "lenient"
    # Snapshot is treated as fresh for this many seconds; older = always fetch.
    SNAPSHOT_FRESHNESS_SECONDS: int = 30

    # ── Auth / RBAC ───────────────────────────────────────────────────────────
    # Four-tier role model (all comma-separated; empty = auth disabled):
    #   superadmin — admin capabilities + write access to all namespaces (no ns block)
    #   admin      — high + medium risk ops, always HITL-gated; infra ns writes blocked
    #   operator   — medium risk ops only (create, apply, scale, exec…), HITL-gated; high-risk blocked
    #   readonly   — read-only ops only; all writes rejected before reaching the agent
    KUBEINTELLECT_SUPERADMIN_KEYS: str = Field(default="")
    KUBEINTELLECT_ADMIN_KEYS: str = Field(default="")
    KUBEINTELLECT_OPERATOR_KEYS: str = Field(default="")
    KUBEINTELLECT_READONLY_KEYS: str = Field(default="")

    # AUTH_BACKEND controls how readonly (demo) keys are validated:
    #   static — keys are checked against KUBEINTELLECT_READONLY_KEYS (default)
    #   hmac   — keys of the form ki-ro-<payload>.<sig> are validated via HMAC;
    #            no list needed — any unexpired key signed with the right secret is valid
    AUTH_BACKEND: str = Field(default="static")

    # Secret used to sign and verify HMAC demo keys (AUTH_BACKEND=hmac).
    # Rotate to invalidate all outstanding demo keys instantly.
    # Generate: openssl rand -hex 32
    DEMO_KEY_HMAC_SECRET: Optional[str] = None

    @property
    def superadmin_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_SUPERADMIN_KEYS.split(",") if k.strip()}

    @property
    def admin_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_ADMIN_KEYS.split(",") if k.strip()}

    @property
    def operator_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_OPERATOR_KEYS.split(",") if k.strip()}

    @property
    def readonly_keys(self) -> set[str]:
        return {k.strip() for k in self.KUBEINTELLECT_READONLY_KEYS.split(",") if k.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.superadmin_keys or self.admin_keys or self.operator_keys or self.readonly_keys or self.DEMO_KEY_HMAC_SECRET)

    @model_validator(mode="after")
    def _validate_provider(self) -> Settings:
        valid = {"azure", "openai"}
        if self.LLM_PROVIDER not in valid:
            raise ValueError(
                f"LLM_PROVIDER must be 'openai' or 'azure', got {self.LLM_PROVIDER!r}.\n"
                f"  Fix: set LLM_PROVIDER=openai (or azure) in ~/.kubeintellect/.env"
            )
        if self.LLM_PROVIDER == "azure":
            missing = [
                k for k, v in {
                    "AZURE_OPENAI_API_KEY": self.AZURE_OPENAI_API_KEY,
                    "AZURE_OPENAI_ENDPOINT": self.AZURE_OPENAI_ENDPOINT,
                }.items() if not v
            ]
            if missing:
                logging.warning(
                    f"LLM_PROVIDER=azure but missing: {', '.join(missing)}. "
                    f"LLM calls will fail until these are set in ~/.kubeintellect/.env"
                )
        elif self.LLM_PROVIDER == "openai" and not self.OPENAI_API_KEY:
            logging.warning(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
                "Get your key at https://platform.openai.com/api-keys "
                "and add OPENAI_API_KEY=sk-... to ~/.kubeintellect/.env"
            )
        valid_modes = {"off", "lenient", "strict"}
        if self.SNAPSHOT_SUFFICIENCY_MODE not in valid_modes:
            raise ValueError(
                f"SNAPSHOT_SUFFICIENCY_MODE must be one of {valid_modes}, "
                f"got {self.SNAPSHOT_SUFFICIENCY_MODE!r}."
            )
        return self


def _load_settings() -> "Settings":
    """Load Settings, converting Pydantic validation errors into readable messages."""
    import sys as _sys
    try:
        return Settings()
    except Exception as exc:
        cfg_file = Path.home() / ".kubeintellect" / ".env"
        # Strip the raw Pydantic traceback and show something actionable
        raw = str(exc)
        print(f"\nConfiguration error: {raw.splitlines()[0]}", file=_sys.stderr)
        if "llm_provider" in raw.lower():
            print(
                f"  LLM_PROVIDER must be 'openai' or 'azure'.\n"
                f"  Edit {cfg_file} and set: LLM_PROVIDER=openai",
                file=_sys.stderr,
            )
        elif "pool" in raw.lower() or "postgres_pool" in raw.lower():
            print(
                f"  POSTGRES_POOL_MIN_CONN and POSTGRES_POOL_MAX_CONN must be positive integers.\n"
                f"  Edit {cfg_file} to fix these values.",
                file=_sys.stderr,
            )
        else:
            print(
                f"  Config file: {cfg_file}\n"
                f"  Run 'kubeintellect init' to reconfigure.",
                file=_sys.stderr,
            )
        raise


settings = _load_settings()
