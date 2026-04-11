# app/core/config.py

import os
from typing import Optional, Any # Added Dict for root_validator
from dotenv import load_dotenv
from pydantic import HttpUrl, Field, ValidationError, model_validator # model_validator for Pydantic v2 root validation
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    from app.utils.logger_config import setup_logging
    logger = setup_logging(app_name="kubeintellect")
except ImportError:
    # Fallback basic logger if the custom setup is not available
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
    logger = logging.getLogger("kubeintellect.config.fallback")
    logger.warning("Custom logger from 'app.utils.logger_config' not found. Using fallback basicConfig.")


load_dotenv()



class Settings(BaseSettings):

    # API Configuration
    API_V1_STR: str = Field(default="/v1", description="API version string.")

    # General LLM Provider Configuration
    LLM_PROVIDER: str = Field(default="azure", description="The primary LLM provider: 'azure', 'openai', or 'litellm'.")

    # --- Azure OpenAI General Configuration ---
    # These are critical if LLM_PROVIDER is 'azure'
    AZURE_OPENAI_API_KEY: Optional[str] = Field(default=None, description="Azure OpenAI API Key.")
    AZURE_OPENAI_ENDPOINT: str = Field(default=None, description="Azure OpenAI Endpoint URL (e.g., 'https://your-resource-name.openai.azure.com/').")
    AZURE_OPENAI_API_VERSION: str = Field(default="2024-02-01", description="Azure OpenAI API version.")

    # --- Primary LLM Configuration (using Azure) ---
    AZURE_PRIMARY_LLM_DEPLOYMENT_NAME: Optional[str] = Field(default=None, description="Azure deployment name for the primary LLM (e.g., gpt-3.5-turbo model).")
    AZURE_PRIMARY_LLM_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for the primary LLM.")
    AZURE_PRIMARY_LLM_TOP_P: float = Field(default=1.0, ge=0.0, le=1.0, description="Top P for the primary LLM.")
    AZURE_PRIMARY_LLM_MAX_TOKENS: int = Field(default=4096, gt=0, description="Max tokens for the primary LLM response.")

    # --- Supervisor LLM Configuration ---
    SUPERVISOR_LLM_MODEL: str = Field(default="gpt-4o", description="Generic model identifier for the supervisor LLM.")
    SUPERVISOR_AZURE_DEPLOYMENT_NAME: str = Field(default="gpt-4o", description="Azure deployment name for the supervisor LLM. Used if LLM_PROVIDER is 'azure'.")
    SUPERVISOR_LLM_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for the supervisor LLM.")
    SUPERVISOR_LLM_TOP_P: float = Field(default=1.0, ge=0.0, le=1.0, description="Top P for the supervisor LLM.")
    SUPERVISOR_LLM_MAX_TOKENS: int = Field(default=1024, gt=0, description="Max tokens for the supervisor LLM response.")

    # --- Code Generator LLM Configuration ---
    CODE_GEN_LLM_MAX_TOKENS: int = Field(default=4000, gt=0, description="Max tokens for the code generator LLM response. Must be large enough for full function bodies.")

    # --- OpenAI Direct Configuration ---
    OPENAI_API_KEY: Optional[str] = Field(default=None, description="OpenAI API Key. Required if LLM_PROVIDER is 'openai'.")
    PRIMARY_LLM_MODEL: str = Field(default="gpt-4o", description="OpenAI model for worker agents.")

    # --- Anthropic (Claude) Configuration ---
    # Install: uv add langchain-anthropic
    ANTHROPIC_API_KEY: Optional[str] = Field(default=None, description="Anthropic API Key. Required if LLM_PROVIDER is 'anthropic'.")
    ANTHROPIC_MODEL: str = Field(default="claude-opus-4-6", description="Anthropic model for the supervisor agent.")
    ANTHROPIC_WORKER_MODEL: str = Field(default="claude-haiku-4-5-20251001", description="Anthropic model for worker agents (use a smaller/cheaper model).")

    # --- Google Gemini Configuration ---
    # Install: uv add langchain-google-genai
    GOOGLE_API_KEY: Optional[str] = Field(default=None, description="Google AI API Key. Required if LLM_PROVIDER is 'google'.")
    GOOGLE_MODEL: str = Field(default="gemini-1.5-pro", description="Google Gemini model for the supervisor agent.")
    GOOGLE_WORKER_MODEL: str = Field(default="gemini-1.5-flash", description="Google Gemini model for worker agents.")

    # --- AWS Bedrock Configuration ---
    # Install: uv add langchain-aws  (uses boto3 credential chain — no API key needed)
    BEDROCK_REGION: str = Field(default="us-east-1", description="AWS region for Bedrock. Credentials come from the boto3 chain (env, ~/.aws, IAM role).")
    BEDROCK_MODEL: str = Field(default="anthropic.claude-3-5-sonnet-20241022-v2:0", description="Bedrock model ID for the supervisor agent.")
    BEDROCK_WORKER_MODEL: str = Field(default="anthropic.claude-3-haiku-20240307-v1:0", description="Bedrock model ID for worker agents.")

    # --- Ollama (local, direct) Configuration ---
    # No extra package needed — uses langchain-community.
    OLLAMA_BASE_URL: str = Field(default="http://localhost:11434", description="Ollama server URL. Used when LLM_PROVIDER is 'ollama'.")
    OLLAMA_MODEL: str = Field(default="llama3", description="Ollama model name (e.g. 'llama3', 'mistral', 'codellama').")

    # --- LiteLLM (universal proxy) Configuration ---
    # Any backend exposed via a LiteLLM proxy: https://docs.litellm.ai/docs/proxy/quick_start
    LITELLM_BASE_URL: str = Field(default="http://localhost:11434/v1", description="Base URL for the LiteLLM proxy (OpenAI-compatible). Used when LLM_PROVIDER is 'litellm'.")
    LITELLM_MODEL: str = Field(default="ollama/llama3", description="Model identifier passed to the LiteLLM proxy (e.g. 'ollama/llama3', 'ollama/mistral').")

    # --- NLU LLM Configuration (Optional) ---
    # If not set, might fall back to primary LLM or have specific logic.
    AZURE_NLU_LLM_DEPLOYMENT_NAME: Optional[str] = Field(default=None, description="Optional Azure deployment name for a specialized NLU LLM.")
    # NLU_LLM_TEMPERATURE: Optional[float] = Field(default=0.0, ge=0.0, le=2.0) # Example if NLU params are needed
    # NLU_LLM_TOP_P: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)       # Example if NLU params are needed
    # NLU_LLM_MAX_TOKENS: Optional[int] = Field(default=100, gt=0)              # Example if NLU params are needed

    # --- System Prompts ---
    KUBEINTELLECT_SYSTEM_PROMPT: str = Field(
        default=(
            "You are KubeIntellect, a helpful AI assistant specializing in Kubernetes operations. "
            "Your primary focus is on Kubernetes-related queries, commands, and operations.\n\n"
            "**Scope Guidelines:**\n"
            "- If the query is about Kubernetes operations, commands, or cluster management, provide detailed assistance.\n"
            "- If the query is related to Kubernetes but requires clarification, ask for the missing information.\n"
            "- If the query is completely unrelated to Kubernetes (e.g., general programming, unrelated system administration, personal questions), "
            "politely inform the user that this is outside the scope of KubeIntellect and that you specialize in Kubernetes operations.\n"
            "- Always be helpful and professional, even when declining out-of-scope requests."
        ),
        description="System prompt for the main KubeIntellect assistant."
    )
    KUBEINTELLECT_NLU_SYSTEM_PROMPT: str = Field(
        default=(
            "You are an expert Natural Language Understanding (NLU) engine for Kubernetes operations. "
            "Your task is to analyze the user's query and classify it into one of the predefined intents, "
            "extracting all relevant parameters for that intent. Respond ONLY with the requested JSON object."
        ),
        description="System prompt for the NLU classification task."
    )

    # --- Concurrency Configuration ---
    MAX_CONCURRENT_WORKFLOWS: int = Field(
        default=20,
        gt=0,
        description="Maximum number of in-flight workflow executions. Requests beyond this limit receive an immediate error rather than queuing indefinitely.",
    )

    # --- Memory Configuration ---
    CONVERSATION_CONTEXT_ENABLED: bool = Field(
        default=True,
        description=(
            "When True, the Conversation Context Service persists active namespace + resource "
            "across agent hops in PostgreSQL and injects them as a pinned SystemMessage before "
            "each supervisor call. Set to False for the memory ablation experiment (Condition B)."
        ),
    )
    SHORT_TERM_MEMORY_WINDOW: int = Field(
        default=3,
        gt=0,
        description="""
        Number of recent message turns to keep in short-term memory for LLM context.
        Higher values provide more context but use more tokens.
        """
    )
    CONVERSATION_SUMMARY_ENABLED: bool = Field(
        default=True,
        description="When True, conversations longer than CONVERSATION_SUMMARY_THRESHOLD messages will have their older history summarized and prepended as a SystemMessage so the supervisor retains context across the full conversation.",
    )
    CONVERSATION_SUMMARY_THRESHOLD: int = Field(
        default=10,
        gt=0,
        description="Total message count that triggers summarization. Conversations with more messages than this value will have their older portion (beyond SHORT_TERM_MEMORY_WINDOW) summarized. Set to a high number to disable effectively.",
    )

    # --- Kubernetes API Timeout ---
    K8S_API_TIMEOUT_SECONDS: int = Field(
        default=15,
        description="Timeout in seconds for all Kubernetes API list/read/watch calls. Tune per environment."
    )

    # --- Tool Output Summarization ---
    SUMMARIZE_TOOL_OUTPUTS: bool = Field(
        default=True,
        description=(
            "When True, heuristic token-budget summarization is applied to tool outputs before "
            "they enter agent message state. Structured fields (pod, namespace, status, etc.) "
            "always pass through; only free-text fields (logs, events, raw_output) are truncated. "
            "Set to False to bypass entirely for debugging — raw tool outputs will be passed as-is."
        ),
    )

    # --- SSH Tunnel Configuration (for Kubernetes API access) ---
    SSH_TUNNEL_ENABLED: bool = Field(default=False, description="Enable SSH tunnel for Kubernetes API access.")
    SSH_TUNNEL_LOCAL_PORT: int = Field(default=6443, description="Local port for the SSH tunnel.")
    SSH_TUNNEL_K8S_API_HOST: str = Field(default="127.0.0.1", description="Target Kubernetes API host from the bastion's perspective (e.g. '192.168.56.11' or '127.0.0.1' if K8s API is local to bastion).")
    SSH_TUNNEL_K8S_API_PORT: int = Field(default=6443, description="Target Kubernetes API port.")
    SSH_TUNNEL_SERVER_HOST: Optional[str] = Field(default=None, description="Hostname or IP of the SSH bastion/server.")
    SSH_TUNNEL_USER: Optional[str] = Field(default=None, description="SSH username for the bastion/server. Can often default to current user if key is specific.") # os.getlogin() could be a dynamic default if run locally.
    SSH_TUNNEL_KEY_PATH: Optional[str] = Field(default="~/.ssh/id_rsa", description="Path to SSH private key for bastion/server. Use a specific key if id_rsa is not desired.")
    SSH_TUNNEL_SETUP_WAIT: int = Field(default=7, gt=0, description="Time in seconds to wait for SSH tunnel setup.")
    SSH_TUNNEL_KEEP_ALIVE_INTERVAL: int = Field(default=30, gt=0, description="SSH keep-alive interval in seconds.")

    # --- Observability query endpoints (Prometheus + Loki) ---
    # Used by metrics_agent (PromQL) and logs_agent (LogQL) to query the observability stack.
    # Defaults point to kube-prometheus-stack and loki-stack services in the kubeintellect namespace.
    PROMETHEUS_URL: str = Field(
        default="http://prometheus-kube-prometheus-prometheus.kubeintellect.svc.cluster.local:9090",
        description="Prometheus HTTP API base URL. Used by query_prometheus tool.",
    )
    LOKI_URL: str = Field(
        default="http://loki.kubeintellect.svc.cluster.local:3100",
        description="Loki HTTP API base URL. Used by query_loki_logs tool.",
    )

    # --- Langfuse LLM Observability (self-hosted, optional) ---
    LANGFUSE_ENABLED: bool = Field(
        default=False,
        description="Enable Langfuse LLM tracing. Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST."
    )
    LANGFUSE_PUBLIC_KEY: Optional[str] = Field(default=None, description="Langfuse project public key.")
    LANGFUSE_SECRET_KEY: Optional[str] = Field(default=None, description="Langfuse project secret key.")
    LANGFUSE_HOST: str = Field(
        default="http://langfuse-web.kubeintellect.svc.cluster.local:3000",
        description="Langfuse server URL. In-cluster default points to the langfuse-web Service."
    )

    # --- LangSmith Tracing Configuration ---
    KUBEINTELLECT_LANGSMITH_TRACING_ENABLED: bool = Field(
        default=False,
        description="Master toggle to enable LangSmith tracing. If true, related LANGCHAIN_* variables will be set."
    )
    LANGCHAIN_API_KEY: Optional[str] = Field(default=None, description="LangSmith API Key. Required if KUBEINTELLECT_LANGSMITH_TRACING_ENABLED is true.")
    LANGCHAIN_PROJECT: Optional[str] = Field(default=None, description="LangSmith Project Name. Required if KUBEINTELLECT_LANGSMITH_TRACING_ENABLED is true.")
    LANGSMITH_ENDPOINT: HttpUrl = Field(
        default="https://api.smith.langchain.com", # Pydantic will cast string from .env to HttpUrl
        description="LangSmith API endpoint. Defaults to public LangSmith."
    )

    # Pydantic Model Configuration
    model_config = SettingsConfigDict(
        env_file=".env",                # Load from .env file
        env_file_encoding='utf-8',
        extra="ignore",                 # Ignore extra fields from environment variables or .env
        case_sensitive=False,           # Environment variable names are case-insensitive
        # env_prefix='KUBEINTELLECT_'   # Optional: prefix for all environment variables
    )

    # Debug mode — enables verbose request logging middleware (never enable in production)
    DEBUG: bool = Field(default=False, description="Enable debug middleware. Off by default; set DEBUG=true for local dev only.")
    UNSAFE_LOG_REQUEST_BODIES: bool = Field(
        default=False,
        description=(
            "Log full HTTP request bodies in the debug middleware. "
            "NEVER enable in production — request bodies contain user queries and may include sensitive data. "
            "Requires DEBUG=true to have any effect."
        ),
    )

    # Logging configuration
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log severity: DEBUG / INFO / WARNING / ERROR / CRITICAL.",
    )
    LOG_FORMAT: str = Field(
        default="text",
        description="Log output format: 'text' for human-readable, 'json' for structured JSON (recommended in production).",
    )

    # CORS — restrict to your frontend. Comma-separated list, e.g. "http://librechat.local,http://localhost:3080"
    ALLOWED_ORIGINS: str = Field(
        default="http://localhost:3080",
        description="Comma-separated list of allowed CORS origins."
    )

    POSTGRES_HOST: str = 'localhost'  # Override via POSTGRES_HOST env var; in-cluster default: postgres.kubeintellect.svc.cluster.local
    POSTGRES_DB: str = 'kubeintellectdb'
    POSTGRES_USER: str = 'kubeuser'
    POSTGRES_PASSWORD: str = 'password'
    POSTGRES_POOL_MIN_CONN: int = Field(default=1, gt=0, description="Minimum connections in the Postgres pool.")
    POSTGRES_POOL_MAX_CONN: int = Field(default=10, gt=0, description="Maximum connections in the Postgres pool.")

    @property
    def POSTGRES_DSN(self) -> str:
        """psycopg3-compatible DSN assembled from individual POSTGRES_* settings."""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}/{self.POSTGRES_DB}"

    # Tool Registry Configuration
    RUNTIME_TOOLS_PVC_PATH: str = Field(
        default="/mnt/runtime-tools",
        description="Path where runtime tools PVC is mounted"
    )
    TOOL_NAMING_PREFIX: str = Field(
        default="gen_",
        description="Prefix for runtime-generated tool names"
    )

    # --- GitHub PR Automation [SECRET + CONFIG] ---
    GITHUB_PR_ENABLED: bool = Field(
        default=False,
        description="Master toggle for GitHub PR creation on tool registration.",
    )
    GITHUB_TOKEN: Optional[str] = Field(
        default=None,
        description="GitHub Personal Access Token with repo scope. Required when GITHUB_PR_ENABLED=true.",
    )
    GITHUB_REPO: Optional[str] = Field(
        default=None,
        description="Target GitHub repository in owner/repo format (e.g. 'myorg/kubeintellect').",
    )
    GITHUB_PR_TARGET_BRANCH: str = Field(
        default="main",
        description="Base branch for generated PRs.",
    )
    GITHUB_PR_AUTO_CREATE: bool = Field(
        default=False,
        description="Auto-create PR immediately after tool registration. If false, use POST /tools/{tool_id}/create-pr.",
    )
    GITHUB_PR_LABELS: str = Field(
        default="ai-generated,tool,needs-review",
        description="Comma-separated labels to apply to generated PRs (labels must exist in the repo).",
    )

    @model_validator(mode='after')
    def _check_conditional_configs(self) -> 'Settings':
        valid_providers = {"azure", "openai", "anthropic", "google", "bedrock", "ollama", "litellm"}
        if self.LLM_PROVIDER not in valid_providers:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(valid_providers)}, got {self.LLM_PROVIDER!r}."
            )

        if self.LLM_PROVIDER == "azure":
            missing = [k for k, v in {
                "AZURE_OPENAI_API_KEY": self.AZURE_OPENAI_API_KEY,
                "AZURE_OPENAI_ENDPOINT": self.AZURE_OPENAI_ENDPOINT,
                "AZURE_PRIMARY_LLM_DEPLOYMENT_NAME": self.AZURE_PRIMARY_LLM_DEPLOYMENT_NAME,
            }.items() if not v]
            if missing:
                logger.warning(f"LLM_PROVIDER='azure' but missing: {', '.join(missing)}")

        elif self.LLM_PROVIDER == "openai":
            if not self.OPENAI_API_KEY:
                logger.warning("LLM_PROVIDER='openai' but OPENAI_API_KEY is not set.")

        elif self.LLM_PROVIDER == "anthropic":
            if not self.ANTHROPIC_API_KEY:
                logger.warning("LLM_PROVIDER='anthropic' but ANTHROPIC_API_KEY is not set.")

        elif self.LLM_PROVIDER == "google":
            if not self.GOOGLE_API_KEY:
                logger.warning("LLM_PROVIDER='google' but GOOGLE_API_KEY is not set.")

        elif self.LLM_PROVIDER == "bedrock":
            logger.info(
                f"LLM_PROVIDER='bedrock', region={self.BEDROCK_REGION}. "
                "Credentials are resolved via the boto3 chain (env vars / ~/.aws / IAM role)."
            )

        elif self.LLM_PROVIDER == "ollama":
            logger.info(f"LLM_PROVIDER='ollama', base_url={self.OLLAMA_BASE_URL}, model={self.OLLAMA_MODEL}")

        elif self.LLM_PROVIDER == "litellm":
            if not self.LITELLM_BASE_URL:
                logger.warning("LLM_PROVIDER='litellm' but LITELLM_BASE_URL is not set.")

        # GitHub PR Automation Configuration Check
        if self.GITHUB_PR_ENABLED:
            missing_gh = [k for k, v in {
                "GITHUB_TOKEN": self.GITHUB_TOKEN,
                "GITHUB_REPO": self.GITHUB_REPO,
            }.items() if not v]
            if missing_gh:
                logger.warning(
                    f"GITHUB_PR_ENABLED=true but missing: {', '.join(missing_gh)}. "
                    "PR creation will fail until these are set."
                )

        # SSH Tunnel Configuration Check
        if self.SSH_TUNNEL_ENABLED:
            missing_ssh_configs = []
            if not self.SSH_TUNNEL_SERVER_HOST:
                missing_ssh_configs.append("SSH_TUNNEL_SERVER_HOST")
            if not self.SSH_TUNNEL_USER:
                missing_ssh_configs.append("SSH_TUNNEL_USER")
            # SSH_TUNNEL_KEY_PATH has a default, so it might not need to be mandatory here unless default is invalid
            # if not self.SSH_TUNNEL_KEY_PATH:
            #     missing_ssh_configs.append("SSH_TUNNEL_KEY_PATH")

            if missing_ssh_configs:
                # Log as warning. Service using the tunnel should handle failure.
                # Or raise ValueError for critical failure.
                logger.warning(
                    f"SSH_TUNNEL_ENABLED is true, but essential SSH tunnel configurations are missing: {', '.join(missing_ssh_configs)}. Tunnel setup may fail."
                )
        return self

    def model_post_init(self, __context: Any) -> None:
        """
        Called after the model is initialized and validated.
        Use this to set up LangSmith environment variables based on settings.
        """
        # super().model_post_init(__context) # Not needed for BaseSettings unless it has custom logic.

        if self.KUBEINTELLECT_LANGSMITH_TRACING_ENABLED:
            missing_langsmith_configs = []
            if not self.LANGCHAIN_API_KEY:
                missing_langsmith_configs.append("LANGCHAIN_API_KEY")
            if not self.LANGCHAIN_PROJECT:
                missing_langsmith_configs.append("LANGCHAIN_PROJECT")

            if missing_langsmith_configs:
                logger.warning(
                    f"KUBEINTELLECT_LANGSMITH_TRACING_ENABLED is true, but required LangSmith configurations are missing: {', '.join(missing_langsmith_configs)}. Tracing will be disabled."
                )
                os.environ["LANGCHAIN_TRACING_V2"] = "false"
            else:
                os.environ["LANGCHAIN_TRACING_V2"] = "true"
                os.environ["LANGCHAIN_API_KEY"] = self.LANGCHAIN_API_KEY
                os.environ["LANGCHAIN_PROJECT"] = self.LANGCHAIN_PROJECT
                os.environ["LANGCHAIN_ENDPOINT"] = str(self.LANGSMITH_ENDPOINT) # Ensure it's a string
                logger.info(
                    f"LangSmith tracing enabled. Project: '{self.LANGCHAIN_PROJECT}', "
                    f"Endpoint: '{str(self.LANGSMITH_ENDPOINT)}'."
                )
        else:
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            logger.info("LangSmith tracing disabled via KUBEINTELLECT_LANGSMITH_TRACING_ENABLED setting.")


try:
    settings = Settings()
    logger.info("Configuration loaded successfully.")

    # Log key configuration details (be careful with secrets in production logs)
    logger.debug(f"API Version: {settings.API_V1_STR}")
    logger.info(f"LLM Provider: {settings.LLM_PROVIDER}")

    if settings.LLM_PROVIDER == "azure":
        if settings.AZURE_OPENAI_ENDPOINT: # Check if endpoint is set before logging
            logger.info(f"Azure OpenAI Endpoint: {settings.AZURE_OPENAI_ENDPOINT}")
        else:
            logger.warning("Azure OpenAI Endpoint is not configured.")

        if settings.AZURE_PRIMARY_LLM_DEPLOYMENT_NAME:
            logger.info(f"Azure Primary LLM Deployment: {settings.AZURE_PRIMARY_LLM_DEPLOYMENT_NAME}")
        else:
            logger.warning("Azure Primary LLM Deployment Name is not configured.")

        if settings.AZURE_NLU_LLM_DEPLOYMENT_NAME:
            logger.info(f"Azure NLU LLM Deployment: {settings.AZURE_NLU_LLM_DEPLOYMENT_NAME}")
        if settings.SUPERVISOR_AZURE_DEPLOYMENT_NAME: # This one has a default, so should always be present
            logger.info(f"Azure Supervisor LLM Deployment: {settings.SUPERVISOR_AZURE_DEPLOYMENT_NAME}")

    if settings.SSH_TUNNEL_ENABLED:
        logger.info(
            f"SSH Tunneling enabled: Local Port {settings.SSH_TUNNEL_LOCAL_PORT} -> "
            f"K8s API {settings.SSH_TUNNEL_K8S_API_HOST}:{settings.SSH_TUNNEL_K8S_API_PORT} "
            f"via {settings.SSH_TUNNEL_USER or '[User not set]'}@{settings.SSH_TUNNEL_SERVER_HOST or '[Server host not set]'}"
        )
        # Specific warnings for SSH are now handled by the model_validator,
        # but an additional info log here confirms the enabled status.

except ValidationError as e:
    # Log detailed validation errors
    error_messages = []
    for error in e.errors():
        field_path = " -> ".join(str(loc) for loc in error['loc']) if error['loc'] else "General"
        message = error['msg']
        error_messages.append(f"Field '{field_path}': {message}")
    detailed_errors = "\n".join(error_messages)
    logger.error(f"Critical configuration error: Failed to load settings due to validation issues.\nDetails:\n{detailed_errors}")
    # Re-raise to prevent application from starting with invalid config
    raise ValueError(f"Critical configuration error. Please check logs. Summary: {e}") from e

except Exception as e:
    logger.error(f"An unexpected error occurred during configuration loading: {e}", exc_info=True)
    # Re-raise for critical failure
    raise ValueError(f"Critical unexpected configuration error: {e}") from e


# Example usage (typically in other modules after importing settings):
# from app.core.config import settings
# print(f"Using Azure Key: {settings.AZURE_OPENAI_API_KEY[:5]}...") # Be careful logging secrets