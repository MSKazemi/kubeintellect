"""
costs.py — Token usage counting and cost estimation for kube-q.

Cost data is stored locally; never exact, always labeled "Est."
Model rates can be overridden via KUBE_Q_COST_PER_1K_PROMPT /
KUBE_Q_COST_PER_1K_COMPLETION env vars for custom backends.
"""

DEFAULT_COST_PER_1K: dict[str, dict[str, float]] = {
    "kubeintellect-v2":  {"prompt": 0.003,   "completion": 0.006},
    "gpt-4o":            {"prompt": 0.005,   "completion": 0.015},
    "gpt-4o-mini":       {"prompt": 0.00015, "completion": 0.0006},
    "claude-sonnet-4-6": {"prompt": 0.003,   "completion": 0.015},
    # Alibaba DashScope / Qwen (intl endpoint, USD per 1K tokens)
    "qwen-max":          {"prompt": 0.0016,  "completion": 0.0064},
    "qwen-plus":         {"prompt": 0.0004,  "completion": 0.0012},
    "qwen-turbo":        {"prompt": 0.00005, "completion": 0.0002},
}


def estimate_cost(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    override_prompt: float | None = None,
    override_completion: float | None = None,
) -> float | None:
    """Return estimated cost in USD, or None if model is unknown and no override provided.

    Override values (from env vars) take precedence over the model lookup table.
    Prefix matching: "kubeintellect-v2-20260401" matches "kubeintellect-v2".
    """
    base_rates: dict[str, float] | None = None
    if model:
        if model in DEFAULT_COST_PER_1K:
            base_rates = DEFAULT_COST_PER_1K[model]
        else:
            # Longest-prefix match so "gpt-4o-mini-..." prefers "gpt-4o-mini" over "gpt-4o"
            best_key = max(
                (k for k in DEFAULT_COST_PER_1K if model.startswith(k)),
                key=len,
                default=None,
            )
            if best_key is not None:
                base_rates = DEFAULT_COST_PER_1K[best_key]

    prompt_rate = (
        override_prompt
        if override_prompt is not None
        else (base_rates["prompt"] if base_rates else None)
    )
    completion_rate = (
        override_completion
        if override_completion is not None
        else (base_rates["completion"] if base_rates else None)
    )

    if prompt_rate is None or completion_rate is None:
        return None

    return (prompt_tokens / 1000) * prompt_rate + (completion_tokens / 1000) * completion_rate


def format_tokens(prompt: int, completion: int) -> str:
    """Return e.g. '120 in → 340 out (460 total)'."""
    total = prompt + completion
    return f"{prompt:,} in → {completion:,} out ({total:,} total)"


def format_cost(cost: float | None) -> str:
    """Return e.g. '$0.0024' or 'cost unknown'."""
    if cost is None:
        return "cost unknown"
    if cost == 0.0:
        return "$0.0000"
    if cost < 0.0001:
        return f"${cost:.6f}"
    return f"${cost:.4f}"
