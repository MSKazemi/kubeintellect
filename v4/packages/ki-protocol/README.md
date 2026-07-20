# ki-protocol

Single source of truth for the KubeIntellect SSE wire protocol (version 1.0).

Two views of the same protocol:

- `ki_protocol.wire` — the **emission models** used by the server. Flat shape:
  `{"type": "...", <fields>, "session_id": "...", "ts": ...}`. This is exactly
  what goes over the wire inside the `ki_event` side-channel.
- `ki_protocol.events` — the **typed client view**: an envelope/`data`
  discriminated union plus `parse_event()`, which normalizes the flat wire
  shape into the nested form. Used by kube-q and any SDK consumer.

Wire-format changes must update both views in the same commit — that is the
point of this package (before V4, the two definitions lived in separate repos
and drifted).
