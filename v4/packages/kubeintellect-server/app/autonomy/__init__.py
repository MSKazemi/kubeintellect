"""Autonomy ladder + watchtower (ADR-003).

A0 observe → A1 investigate+report → A2 propose (HITL) → A3 auto-fix
(allowlisted patterns only, rollback-armed). Configured per cluster with
per-namespace overrides; the requesting identity's role remains the hard
ceiling on what any level may do.
"""
