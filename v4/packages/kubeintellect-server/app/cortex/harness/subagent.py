"""The frozen ADR-101 investigation-subagent contract (v5 specs/00 §3, specs/01).

A gather subagent runs in an isolated context, may call ONLY the four ACI read verbs, never
mutates, and must return a structured summary bounded to ~2k tokens. These are pure guards the
gather node applies around whatever runner P2 chooses; keeping them here makes "subagents
isolate reads, single writer mutates" structural rather than a prompt exhortation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.tools.aci import ACI_READ_VERB_ALLOWLIST

# ~2k-token summary budget. Chars-per-token ≈ 4 (English/code heuristic); kept explicit and
# conservative so the bound is deterministic without a tokenizer dependency.
SUMMARY_MAX_TOKENS = 2000
_CHARS_PER_TOKEN = 4
SUMMARY_MAX_CHARS = SUMMARY_MAX_TOKENS * _CHARS_PER_TOKEN


class SubagentContractError(RuntimeError):
    """A subagent was configured with a tool outside the read-only allowlist."""


@dataclass(frozen=True)
class SubagentContract:
    """What a dispatched investigation subagent is allowed to do."""
    objective: str
    allowed_verbs: frozenset[str] = field(default_factory=lambda: ACI_READ_VERB_ALLOWLIST)
    max_summary_tokens: int = SUMMARY_MAX_TOKENS
    may_mutate: bool = False  # always False for v0 investigation subagents

    def __post_init__(self) -> None:
        enforce_read_only_allowlist(self.allowed_verbs)
        if self.may_mutate:
            raise SubagentContractError("investigation subagents must not mutate (ADR-101)")


@dataclass(frozen=True)
class SubagentResult:
    """The bounded structured summary a subagent returns to the lead agent."""
    objective: str
    summary: str
    truncated: bool
    verbs_used: tuple[str, ...]


def enforce_read_only_allowlist(verbs: frozenset[str]) -> None:
    """Raise unless every requested verb is one of the four ACI read verbs."""
    extra = set(verbs) - set(ACI_READ_VERB_ALLOWLIST)
    if extra:
        raise SubagentContractError(
            f"verbs {sorted(extra)} are outside the read-only allowlist "
            f"{sorted(ACI_READ_VERB_ALLOWLIST)}"
        )


def bound_summary(text: str, max_tokens: int = SUMMARY_MAX_TOKENS) -> tuple[str, bool]:
    """Cap a subagent summary to the token budget. Returns (text, truncated).

    Never splits mid-line where avoidable; appends an explicit truncation marker so the lead
    agent is never silently handed a clipped summary.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars // 2:  # only prefer a line break if it isn't wastefully early
        cut = cut[:nl]
    return cut.rstrip() + "\n…[summary truncated to fit the 2k-token subagent budget]", True


def finalize_result(contract: SubagentContract, raw_summary: str, verbs_used: list[str]) -> SubagentResult:
    """Apply the contract's bounds to a raw subagent summary (the gather-node tail)."""
    enforce_read_only_allowlist(frozenset(verbs_used))
    summary, truncated = bound_summary(raw_summary, contract.max_summary_tokens)
    return SubagentResult(
        objective=contract.objective,
        summary=summary,
        truncated=truncated,
        verbs_used=tuple(verbs_used),
    )
