"""K8s-ACI v0 — bounded, normalized, never-silent read-only verbs (v5 specs/01, ADR-101).

Additive over V4: these verbs wrap ``run_kubectl`` (never a new subprocess) so they inherit
its injection guard, protected-namespace block, secret redaction, and flight-recorder rows.
The read-verb allowlist is exported unconditionally so the P2 harness subagent runner can
constrain read-only investigation subagents to exactly these verbs, independent of the flag.
"""

from __future__ import annotations

from app.tools.aci.read_verbs import diff_change, inspect, logs, search

# The four R0 (read-only) verbs a harness investigation subagent may call. Exported
# regardless of KI_V5_ACI_READ_VERBS_ENABLED (spec R-aci-reg-02).
ACI_READ_VERB_ALLOWLIST = frozenset({"inspect", "search", "logs", "diff_change"})

ACI_READ_VERBS = [inspect, search, logs, diff_change]

__all__ = [
    "ACI_READ_VERBS",
    "ACI_READ_VERB_ALLOWLIST",
    "diff_change",
    "inspect",
    "logs",
    "search",
]
