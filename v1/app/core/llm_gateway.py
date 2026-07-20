"""
LLM Gateway — single factory for all LLM instances in KubeIntellect.

Public streaming-error API:
  classify_llm_error(exc) → LLMStreamError   normalise provider exceptions to a small taxonomy

Supported providers (set via LLM_PROVIDER env var):
  azure      → AzureChatOpenAI                           (default)
  openai     → ChatOpenAI via api.openai.com
  anthropic  → ChatAnthropic (Claude)                    pip: langchain-anthropic
  google     → ChatGoogleGenerativeAI (Gemini)           pip: langchain-google-genai
  bedrock    → ChatBedrock (AWS)                         pip: langchain-aws
  ollama     → ChatOllama (local, no proxy needed)       bundled in langchain-community
  litellm    → ChatOpenAI → LiteLLM proxy (any backend)

Public API:
  get_supervisor_llm()                   LLM for the supervisor agent
  get_worker_llm()                       LLM for worker agents (may differ in azure)
  get_code_gen_llm()                     LLM for code generator (high token limit)
  get_llm_with_params(temp, max_tokens)  ad-hoc instance for title gen, etc.
"""

from dataclasses import dataclass
from typing import Any, List

from langchain_openai import AzureChatOpenAI, ChatOpenAI

from app.core.config import settings
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# Type alias — concrete type depends on the provider chosen at runtime
LLMInstance = Any


_langfuse_client = None  # initialized once on first use


def get_langfuse_callbacks() -> List[Any]:
    """Return a Langfuse CallbackHandler list if tracing is enabled, else empty list.

    Langfuse v4: session_id / user_id / trace_name are NOT set here — they are
    propagated via `langfuse.propagate_attributes()` at the call site (e.g. in
    workflow.py before calling app.astream()) so that all spans within a single
    workflow run share the same trace context.
    """
    global _langfuse_client
    if not settings.LANGFUSE_ENABLED:
        return []
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        logger.warning("LANGFUSE_ENABLED=true but PUBLIC_KEY or SECRET_KEY is missing — tracing disabled.")
        return []
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
        if _langfuse_client is None:
            _langfuse_client = Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                host=settings.LANGFUSE_HOST,
            )
            logger.info(f"Langfuse tracing enabled. Host: {settings.LANGFUSE_HOST}")
        return [CallbackHandler()]
    except ImportError:
        logger.warning("LANGFUSE_ENABLED=true but 'langfuse' package is not installed. Run: uv add langfuse")
        return []


# Keep private alias for backward compatibility with any internal callers
_get_langfuse_callbacks = get_langfuse_callbacks


def get_langfuse_client() -> Any:
    """Return the shared Langfuse client singleton, or None if tracing is disabled.

    Use this for direct SDK operations: scoring, trace I/O, flush, etc.
    The client is initialised lazily on the first call to get_langfuse_callbacks().
    """
    if not settings.LANGFUSE_ENABLED:
        return None
    get_langfuse_callbacks()   # ensures _langfuse_client is initialised
    return _langfuse_client


# ── internal builders ──────────────────────────────────────────────────────────

def _build_azure(deployment: str, temperature: float, max_tokens: int) -> AzureChatOpenAI:
    return AzureChatOpenAI(
        azure_endpoint=str(settings.AZURE_OPENAI_ENDPOINT),
        api_key=settings.AZURE_OPENAI_API_KEY,
        azure_deployment=deployment,
        openai_api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


def _build_openai(model: str, temperature: float, max_tokens: int) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.OPENAI_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


def _build_anthropic(model: str, temperature: float, max_tokens: int) -> LLMInstance:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError(
            "LLM_PROVIDER='anthropic' requires the langchain-anthropic package. "
            "Install it with: uv add langchain-anthropic"
        )
    return ChatAnthropic(
        model=model,
        api_key=settings.ANTHROPIC_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


def _build_google(model: str, temperature: float, max_tokens: int) -> LLMInstance:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError(
            "LLM_PROVIDER='google' requires the langchain-google-genai package. "
            "Install it with: uv add langchain-google-genai"
        )
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=settings.GOOGLE_API_KEY,
        temperature=temperature,
        max_output_tokens=max_tokens,
        streaming=True,
    )


def _build_bedrock(model: str, temperature: float, max_tokens: int) -> LLMInstance:
    try:
        from langchain_aws import ChatBedrock
    except ImportError:
        raise ImportError(
            "LLM_PROVIDER='bedrock' requires the langchain-aws package. "
            "Install it with: uv add langchain-aws"
        )
    return ChatBedrock(
        model_id=model,
        region_name=settings.BEDROCK_REGION,
        streaming=True,
        model_kwargs={"temperature": temperature, "max_tokens": max_tokens},
    )


def _build_ollama(model: str, temperature: float) -> LLMInstance:
    """Direct Ollama integration — no LiteLLM proxy required."""
    from langchain_community.chat_models import ChatOllama
    return ChatOllama(
        model=model,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=temperature,
    )


def _build_litellm(model: str, temperature: float, max_tokens: int) -> ChatOpenAI:
    """Points ChatOpenAI at a LiteLLM proxy that exposes an OpenAI-compatible API."""
    return ChatOpenAI(
        model=model,
        base_url=settings.LITELLM_BASE_URL,
        api_key="litellm",  # proxy typically does not enforce a real key
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


def _unsupported(provider: str) -> None:
    raise ValueError(
        f"Unsupported LLM_PROVIDER: {provider!r}. "
        "Valid values: azure, openai, anthropic, google, bedrock, ollama, litellm."
    )


# ── shared dispatch helpers ────────────────────────────────────────────────────

def _build_supervisor(provider: str, temperature: float, max_tokens: int) -> LLMInstance:
    if provider == "azure":
        return _build_azure(settings.SUPERVISOR_AZURE_DEPLOYMENT_NAME, temperature, max_tokens)
    if provider == "openai":
        return _build_openai(settings.SUPERVISOR_LLM_MODEL, temperature, max_tokens)
    if provider == "anthropic":
        return _build_anthropic(settings.ANTHROPIC_MODEL, temperature, max_tokens)
    if provider == "google":
        return _build_google(settings.GOOGLE_MODEL, temperature, max_tokens)
    if provider == "bedrock":
        return _build_bedrock(settings.BEDROCK_MODEL, temperature, max_tokens)
    if provider == "ollama":
        return _build_ollama(settings.OLLAMA_MODEL, temperature)
    if provider == "litellm":
        return _build_litellm(settings.LITELLM_MODEL, temperature, max_tokens)
    _unsupported(provider)


def _build_worker(provider: str, temperature: float, max_tokens: int) -> LLMInstance:
    if provider == "azure":
        return _build_azure(settings.AZURE_PRIMARY_LLM_DEPLOYMENT_NAME, temperature, max_tokens)
    if provider == "openai":
        return _build_openai(settings.PRIMARY_LLM_MODEL, temperature, max_tokens)
    if provider == "anthropic":
        return _build_anthropic(settings.ANTHROPIC_WORKER_MODEL, temperature, max_tokens)
    if provider == "google":
        return _build_google(settings.GOOGLE_WORKER_MODEL, temperature, max_tokens)
    if provider == "bedrock":
        return _build_bedrock(settings.BEDROCK_WORKER_MODEL, temperature, max_tokens)
    if provider == "ollama":
        return _build_ollama(settings.OLLAMA_MODEL, temperature)
    if provider == "litellm":
        return _build_litellm(settings.LITELLM_MODEL, temperature, max_tokens)
    _unsupported(provider)


# ── public API ─────────────────────────────────────────────────────────────────

def get_supervisor_llm() -> LLMInstance:
    """Return the LLM instance used by the supervisor agent."""
    provider = settings.LLM_PROVIDER
    logger.debug(f"Building supervisor LLM: provider={provider}")
    llm = _build_supervisor(
        provider,
        settings.SUPERVISOR_LLM_TEMPERATURE,
        settings.SUPERVISOR_LLM_MAX_TOKENS,
    )
    callbacks = _get_langfuse_callbacks()
    return llm.with_config(callbacks=callbacks) if callbacks else llm


def get_worker_llm() -> LLMInstance:
    """Return the LLM instance used by worker agents.

    Azure allows a separate cheaper deployment for workers vs the supervisor.
    For all other providers the worker model is a distinct config key
    (e.g. ANTHROPIC_WORKER_MODEL) so you can run a smaller model for routine tasks.
    """
    provider = settings.LLM_PROVIDER
    logger.debug(f"Building worker LLM: provider={provider}")
    llm = _build_worker(
        provider,
        settings.AZURE_PRIMARY_LLM_TEMPERATURE,
        settings.AZURE_PRIMARY_LLM_MAX_TOKENS,
    )
    callbacks = _get_langfuse_callbacks()
    return llm.with_config(callbacks=callbacks) if callbacks else llm


def get_code_gen_llm() -> LLMInstance:
    """Return the LLM instance used by the code generator agent.

    Uses the supervisor model with a high token limit so generated function
    bodies are never truncated mid-output (which breaks ```python``` extraction).
    """
    provider = settings.LLM_PROVIDER
    logger.debug(f"Building code-gen LLM: provider={provider}, max_tokens={settings.CODE_GEN_LLM_MAX_TOKENS}")
    llm = _build_supervisor(provider, 0.0, settings.CODE_GEN_LLM_MAX_TOKENS)
    callbacks = _get_langfuse_callbacks()
    return llm.with_config(callbacks=callbacks) if callbacks else llm


def get_llm_with_params(
    temperature: float = 0.2,
    max_tokens: int = 16,
) -> LLMInstance:
    """Return a one-off LLM instance with caller-specified sampling parameters.

    Uses the supervisor model. Intended for short one-shot tasks like
    title generation or quick classification.
    """
    provider = settings.LLM_PROVIDER
    logger.debug(f"Building ad-hoc LLM: provider={provider}, temp={temperature}, max_tokens={max_tokens}")
    llm = _build_supervisor(provider, temperature, max_tokens)
    callbacks = _get_langfuse_callbacks()
    return llm.with_config(callbacks=callbacks) if callbacks else llm


# ── streaming error taxonomy ───────────────────────────────────────────────────

@dataclass
class LLMStreamError:
    """Normalised LLM streaming error returned by classify_llm_error().

    error_type — one of: rate_limit | context_length | auth | network | unknown
    user_message — curated string safe to display to users (no provider internals)
    original — the raw exception for logging / re-raise
    """
    error_type: str
    user_message: str
    original: Exception


_USER_MESSAGES: dict = {
    "rate_limit": "Rate limit reached — please wait a moment and retry.",
    "context_length": (
        "Your query is too long — try asking about a specific pod, namespace, or resource."
    ),
    "auth": "LLM authentication failed — check your API key configuration.",
    "network": "Connection to the LLM was interrupted — please retry your question.",
    "unknown": (
        "An error occurred while processing your request. Please try again."
    ),
}


def classify_llm_error(exc: Exception) -> LLMStreamError:
    """Classify a provider exception (possibly wrapped by LangChain) into an LLMStreamError.

    Unwraps __cause__ / __context__ up to 4 levels to surface the original provider
    exception before type-based classification.  Falls back to message-content matching.
    Provider-specific imports stay here — workflow.py stays provider-agnostic.
    """
    candidate = exc
    for _ in range(4):
        cause = getattr(candidate, "__cause__", None) or getattr(candidate, "__context__", None)
        if cause is None:
            break
        candidate = cause

    error_type = _classify_by_type(candidate)
    if error_type == "unknown" and candidate is not exc:
        error_type = _classify_by_message(exc)

    return LLMStreamError(
        error_type=error_type,
        user_message=_USER_MESSAGES[error_type],
        original=exc,
    )


def _classify_by_type(exc: Exception) -> str:
    """Classify by exception class name — avoids hard provider SDK imports."""
    cls_name = type(exc).__name__

    # OpenAI / Azure OpenAI exception names
    if cls_name == "RateLimitError":
        return "rate_limit"
    if cls_name in ("ContextLengthExceededError",) or (
        cls_name in ("BadRequestError", "InvalidRequestError")
        and _context_length_in_message(exc)
    ):
        return "context_length"
    if cls_name == "AuthenticationError":
        return "auth"
    if cls_name in ("APIConnectionError", "APITimeoutError"):
        return "network"

    # Anthropic
    if cls_name == "OverloadedError":
        return "rate_limit"

    # HTTP status codes carried on the exception
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "auth"

    # Network-level (httpx, asyncio, stdlib)
    if cls_name in (
        "ConnectError", "ConnectTimeout", "ReadTimeout",
        "RemoteProtocolError", "ConnectionError", "TimeoutError",
    ):
        return "network"
    if "timeout" in cls_name.lower() or "connection" in cls_name.lower():
        return "network"

    return _classify_by_message(exc)


def _classify_by_message(exc: Exception) -> str:
    msg = str(exc).lower()
    if "rate limit" in msg or "rate_limit" in msg or "429" in msg or "too many requests" in msg:
        return "rate_limit"
    if (
        "context length" in msg or "context_length" in msg
        or "maximum context" in msg
        or ("token" in msg and "limit" in msg)
    ):
        return "context_length"
    if "authentication" in msg or "api key" in msg or "unauthorized" in msg or "401" in msg:
        return "auth"
    if "connection" in msg or "timeout" in msg or "network" in msg or "unreachable" in msg:
        return "network"
    return "unknown"


def _context_length_in_message(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "context_length_exceeded" in msg
        or "maximum context" in msg
        or ("token" in msg and "limit" in msg)
    )
