"""Cortex — the V4 reasoning runtime (ADR-001).

Explicit LangGraph nodes (triage → gather loop → synthesize → remember) with a
hand-rolled tool loop. No create_react_agent: streaming, plan state, and HITL
are controlled exactly. Enabled via CORTEX_V4_ENABLED; the V2 graph remains
the fallback until the LOFA L4 gate retires it.
"""
