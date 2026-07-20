"""Sensorium — continuous perception layer (V4 architecture §1).

Always-on watchers normalise cluster signals into Observation records and
feed them to the detector engine. Replaces the per-query snapshot as the
primary perception path; the snapshot remains as a chat-turn fallback.
"""
