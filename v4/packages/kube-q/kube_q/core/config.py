"""
config.py — Configuration loading and logging setup.

Priority order (highest wins):
  CLI flag  >  shell env var  >  ./.env  >  ~/.kube-q/.env  >  hardcoded default
"""

import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".kube-q"
LOG_FILE   = CONFIG_DIR / "kube-q.log"


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class Config:
    """All runtime configuration. CLI args override env vars which override defaults."""
    # Connection
    url:                    str        = "https://api.kubeintellect.com"
    api_key:                str | None = None

    # Timeouts (seconds)
    timeout:                float = 120.0   # query (stream + non-stream)
    health_timeout:         float =   5.0   # /healthz check
    namespace_timeout:      float =   3.0   # /v1/namespaces/:ns check
    startup_retry_timeout:  int   =   0     # total wait for API on startup (0 = no retry)
    startup_retry_interval: int   =   5     # seconds between startup retries

    # Behaviour
    stream:             bool = True
    output:             str  = "rich"   # "rich" | "plain"
    log_level:          str  = "INFO"
    skip_health_check:  bool = False

    # Model
    model: str = "kubeintellect-v2"

    # Display names
    user_name:  str = "You"
    agent_name: str = "kube-q"

    # Banner customisation
    logo:    str | None = None   # big logo shown at top of banner (use \n for newlines)
    tagline: str | None = None   # subtitle / copyright line, e.g. "© 2025 Acme Corp"

    # Cost estimation overrides (USD per 1K tokens)
    cost_per_1k_prompt:      float | None = None
    cost_per_1k_completion:  float | None = None

    # Backend selector: "kube-q" (default), "openai", or "azure"
    backend: str = "kube-q"

    # Direct OpenAI backend (when backend=openai)
    openai_api_key:  str | None = None
    openai_endpoint: str        = "https://api.openai.com"
    openai_model:    str        = "gpt-4o-mini"

    # Azure OpenAI backend (when backend=azure)
    azure_openai_api_key:     str | None = None
    azure_openai_endpoint:    str | None = None   # e.g. https://my-resource.openai.azure.com
    azure_openai_deployment:  str | None = None   # deployment name, NOT model name
    azure_openai_api_version: str        = "2024-06-01"

    # Kubernetes context (prepended to every query, similar to namespace)
    kube_context: str | None = None

    # Named profile (loaded from ~/.kube-q/profiles/<name>.env)
    profile: str | None = None


# ── .env file support ─────────────────────────────────────────────────────────

# Maps KUBE_Q_* env var names → (config field, type).
_ENV_MAP: dict[str, tuple[str, type]] = {
    "KUBE_Q_URL":                    ("url",                    str),
    "KUBE_Q_API_KEY":                ("api_key",                str),
    "KUBE_Q_TIMEOUT":                ("timeout",                float),
    "KUBE_Q_HEALTH_TIMEOUT":         ("health_timeout",         float),
    "KUBE_Q_NAMESPACE_TIMEOUT":      ("namespace_timeout",      float),
    "KUBE_Q_STARTUP_RETRY_TIMEOUT":  ("startup_retry_timeout",  int),
    "KUBE_Q_STARTUP_RETRY_INTERVAL": ("startup_retry_interval", int),
    "KUBE_Q_STREAM":                 ("stream",                 bool),
    "KUBE_Q_OUTPUT":                 ("output",                 str),
    "KUBE_Q_LOG_LEVEL":              ("log_level",              str),
    "KUBE_Q_SKIP_HEALTH_CHECK":      ("skip_health_check",      bool),
    "KUBE_Q_MODEL":                  ("model",                  str),
    "KUBE_Q_USER_NAME":              ("user_name",              str),
    "KUBE_Q_AGENT_NAME":             ("agent_name",             str),
    "KUBE_Q_COST_PER_1K_PROMPT":     ("cost_per_1k_prompt",     float),
    "KUBE_Q_COST_PER_1K_COMPLETION": ("cost_per_1k_completion", float),
    "KUBE_Q_LOGO":                   ("logo",                   str),
    "KUBE_Q_TAGLINE":                ("tagline",                str),
    "KUBE_Q_BACKEND":                ("backend",                str),
    "KUBE_Q_OPENAI_API_KEY":         ("openai_api_key",         str),
    "KUBE_Q_OPENAI_ENDPOINT":        ("openai_endpoint",        str),
    "KUBE_Q_OPENAI_MODEL":           ("openai_model",           str),
    "KUBE_Q_AZURE_OPENAI_API_KEY":   ("azure_openai_api_key",   str),
    "KUBE_Q_AZURE_OPENAI_ENDPOINT":  ("azure_openai_endpoint",  str),
    "KUBE_Q_AZURE_OPENAI_DEPLOYMENT":("azure_openai_deployment",str),
    "KUBE_Q_AZURE_OPENAI_API_VERSION":("azure_openai_api_version",str),
    "KUBE_Q_CONTEXT":                ("kube_context",           str),
    "KUBE_Q_PROFILE":                ("profile",                str),
}


def _load_dotenv_file(path: Path) -> None:
    """Minimal .env parser — sets env vars not already in the environment.

    Supports KEY=VALUE, KEY="VALUE", KEY='VALUE', and inline # comments.
    Shell-set variables always win (no override).
    """
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split("#")[0].strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def _apply_env(cfg: Config) -> None:
    """Override cfg fields from environment variables."""
    for env_key, (field, typ) in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if typ is bool:
            setattr(cfg, field, val.lower() not in ("0", "false", "no", "off"))
        else:
            try:
                setattr(cfg, field, typ(val))
            except (ValueError, TypeError):
                print(
                    f"Warning: invalid value for {env_key}={val!r} "
                    f"(expected {typ.__name__}) — ignored",
                    file=sys.stderr,
                )


_VALID_OUTPUTS = ("rich", "plain")
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_VALID_BACKENDS = ("kube-q", "openai", "azure")

PROFILES_DIR = CONFIG_DIR / "profiles"


def _env_var_for_field(field_name: str) -> str | None:
    """Return the KUBE_Q_* env var name that maps to *field_name*, if any."""
    for env_key, (name, _typ) in _ENV_MAP.items():
        if name == field_name:
            return env_key
    return None


def validate_config(cfg: Config) -> list[str]:
    """Return a list of actionable error messages for invalid config values.

    Empty list means the config is valid. Each message includes the offending
    value, the matching env var (so the user knows what to edit), and what a
    valid value looks like.
    """
    errors: list[str] = []

    def _hint(field_name: str) -> str:
        env = _env_var_for_field(field_name)
        return f"  Fix: set {env} in ~/.kube-q/.env or pass a valid value." if env else ""

    parsed = urlparse(cfg.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        errors.append(
            f"Invalid URL: {cfg.url!r} — must start with http:// or https:// and include a host.\n"
            f"  Example: KUBE_Q_URL=https://api.kubeintellect.com\n"
            f"{_hint('url')}"
        )

    numeric_fields: tuple[tuple[str, bool], ...] = (
        # (field_name, allow_zero)
        ("timeout",                False),
        ("health_timeout",         False),
        ("namespace_timeout",      False),
        ("startup_retry_timeout",  True),
        ("startup_retry_interval", False),
    )
    for name, allow_zero in numeric_fields:
        val = getattr(cfg, name)
        ok = (val >= 0) if allow_zero else (val > 0)
        if not ok:
            min_label = ">= 0" if allow_zero else "> 0"
            errors.append(
                f"Invalid {name}: {val!r} — must be a number {min_label}.\n"
                f"{_hint(name)}"
            )

    if cfg.output not in _VALID_OUTPUTS:
        errors.append(
            f"Invalid output: {cfg.output!r} — must be one of "
            f"{', '.join(_VALID_OUTPUTS)}.\n"
            f"{_hint('output')}"
        )

    if cfg.log_level.upper() not in _VALID_LOG_LEVELS:
        errors.append(
            f"Invalid log_level: {cfg.log_level!r} — must be one of "
            f"{', '.join(_VALID_LOG_LEVELS)}.\n"
            f"{_hint('log_level')}"
        )

    for name in ("cost_per_1k_prompt", "cost_per_1k_completion"):
        val = getattr(cfg, name)
        if val is not None and val < 0:
            errors.append(
                f"Invalid {name}: {val!r} — must be >= 0.\n"
                f"{_hint(name)}"
            )

    for name in ("user_name", "agent_name", "model"):
        val = getattr(cfg, name)
        if not isinstance(val, str) or not val.strip():
            errors.append(
                f"Invalid {name}: {val!r} — must be a non-empty string.\n"
                f"{_hint(name)}"
            )

    if cfg.backend not in _VALID_BACKENDS:
        errors.append(
            f"Invalid backend: {cfg.backend!r} — must be one of "
            f"{', '.join(_VALID_BACKENDS)}.\n"
            f"{_hint('backend')}"
        )

    if cfg.backend == "openai" and not cfg.openai_api_key:
        errors.append(
            "backend=openai requires an API key.\n"
            "  Fix: set KUBE_Q_OPENAI_API_KEY in ~/.kube-q/.env or pass --openai-api-key."
        )
    if cfg.backend == "azure":
        missing = []
        if not cfg.azure_openai_api_key:
            missing.append("KUBE_Q_AZURE_OPENAI_API_KEY")
        if not cfg.azure_openai_endpoint:
            missing.append("KUBE_Q_AZURE_OPENAI_ENDPOINT")
        if not cfg.azure_openai_deployment:
            missing.append("KUBE_Q_AZURE_OPENAI_DEPLOYMENT")
        if missing:
            errors.append(
                "backend=azure requires " + ", ".join(missing) + ".\n"
                "  Fix: set them in ~/.kube-q/.env or pass the matching --azure-* flags."
            )

    # Sanity-check that every field we inspected actually exists on the
    # dataclass — protects against typos if someone renames a field.
    known = {f.name for f in fields(cfg)}
    for name, _ in numeric_fields:
        assert name in known, f"validate_config references unknown field {name!r}"

    return errors


def load_config(strict: bool = True) -> Config:
    """Load configuration from .env files and environment variables.

    .env files loaded (lower to higher priority):
      ~/.kube-q/.env    — persistent user-level defaults
      ./.env            — project-local or per-cluster overrides

    When *strict* is True (default), invalid values cause the process to exit
    with a formatted error list. Tests pass strict=False to get the raw Config
    back regardless of validity.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Load .env files before reading env vars.
    # Shell-exported variables are never overwritten.
    # Precedence (lowest → highest, later overrides earlier):
    #   ~/.kube-q/.env
    #   ~/.kube-q/profiles/<profile>.env  (if KUBE_Q_PROFILE is set)
    #   ./.env
    #   shell env (always wins)
    _load_dotenv_file(CONFIG_DIR / ".env")   # user-level defaults

    profile_name = os.environ.get("KUBE_Q_PROFILE")
    if profile_name:
        _load_dotenv_file(PROFILES_DIR / f"{profile_name}.env")

    _load_dotenv_file(Path(".env"))          # local        (higher priority)

    cfg = Config()
    _apply_env(cfg)

    if strict:
        errors = validate_config(cfg)
        if errors:
            print("kube-q: configuration error(s):", file=sys.stderr)
            for err in errors:
                print(f"\n  • {err}", file=sys.stderr)
            print(
                "\n  Edit ~/.kube-q/.env or ./.env, "
                "then retry.  See `kq config show` for current values.",
                file=sys.stderr,
            )
            sys.exit(2)

    return cfg


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO", debug: bool = False) -> None:
    """Configure the kube_q logger.

    Always writes to ~/.kube-q/kube-q.log (rotating, 5 MB × 3).
    When *debug* is True the level is forced to DEBUG and a second handler
    writes brief lines to stderr so the user sees live HTTP chatter.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    effective_level = logging.DEBUG if debug else getattr(logging, log_level.upper(), logging.INFO)

    logger = logging.getLogger("kube_q")
    logger.setLevel(effective_level)

    # Avoid duplicate handlers when called more than once (e.g., in tests)
    if logger.handlers:
        return

    file_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # ── Rotating file handler ─────────────────────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(effective_level)
        fh.setFormatter(file_fmt)
        logger.addHandler(fh)
    except OSError as exc:
        print(f"Warning: cannot open log file {LOG_FILE}: {exc}", file=sys.stderr)

    # ── Stderr handler (debug mode only) ──────────────────────────────────────
    if debug:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("  \033[2mDEBUG %(name)s: %(message)s\033[0m"))
        logger.addHandler(sh)
