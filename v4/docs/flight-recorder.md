---
description: >-
  The KubeIntellect flight recorder — an append-only, hash-chained decision
  log of every event in every session, with tamper-evident replay via
  `kq replay` and pre-mutation rollback points.
---

# Flight Recorder & Replay

Every non-token event the server emits — status updates, tool calls, tool
output, approval prompts, findings, plan transitions — is recorded as one row
in the `decision_log` table. The table is **append-only** and **hash-chained
per episode**, so you can replay any past session and know whether the record
has been modified since it was written.

An *episode* is a session: `episode_id == session_id`. Interactive chat
sessions, autonomous watchtower investigations (`auto-*`), and the detector
findings stream (`findings:<cluster-id>`) all land in the same log.

---

## Tamper evidence

Each row's hash covers the previous row's hash plus the canonical JSON of the
row itself:

```
hash = sha256(prev_hash + JSON({episode_id, seq, kind, payload}))
genesis prev_hash = ""
```

Because every hash includes the previous one, **modifying any row breaks the
hash of every later row in that episode** — there is no way to edit, insert,
or delete a record without the chain failing verification afterwards. The
replay endpoint recomputes the full chain on every read and reports the
verdict before streaming a single event.

Token frames (one per LLM token) are deliberately not recorded — they would
bloat the table by orders of magnitude with no audit value. The final answer
text is recorded as a normal event.

---

## Replaying an episode

### `kq replay`

```bash
kq replay <session-id>
```

The session ID is shown by `/id` in the `kq` REPL; autonomous investigations
use `auto-<finding-id>` (visible in `kq digest`). Output is the full event
sequence in order, followed by the integrity verdict:

```text
Episode 3f9c…
  #  type             summary
  0  status           phase=analyzing  message=Triaging request…
  1  tool_call        tool=run_kubectl  command=kubectl get pods -n prod
  …
42 records · ✓ chain intact
```

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Replay succeeded, chain intact |
| `1` | Episode not found (or the request failed) |
| `2` | Usage error |
| `3` | Replay succeeded but the **chain is broken** — records may have been tampered with |

Exit code `3` is the one to alert on in scripts: the data streamed, but it is
no longer trustworthy.

### `GET /v1/episodes/{episode_id}/replay`

The HTTP endpoint streams Server-Sent Events. The **first frame is always a
meta record carrying the chain verdict**, then each recorded payload in `seq`
order, then `[DONE]`:

```text
data: {"type": "replay_meta", "episode_id": "3f9c…", "records": 42, "chain_valid": true}

data: {"type": "status", "phase": "analyzing", …}

…

data: [DONE]
```

Returns `404` when no records exist for the episode. Unlike
`/v1/events/replay/{session_id}` (in-memory, lost on restart), this replay is
durable — it survives server restarts and redeployments.

---

## Rollback points

Before **every mutating `kubectl` command** (any medium- or high-risk verb;
dry-runs excluded), the tool layer captures the current YAML of the targeted
objects and records it as a `rollback_point` event in the flight recorder:

```json
{
  "type": "rollback_point",
  "rollback_id": "rb-1a2b3c4d5e6f",
  "command": "kubectl scale deployment/web --replicas=0 -n prod",
  "pre_state": ["apiVersion: apps/v1\nkind: Deployment\n…"]
}
```

- IDs are `rb-` followed by 12 hex characters.
- The captured YAML is **redacted** (same secret-stripping as
  [reflexion](reflexion.md)) and capped in size.
- Recovery is manual but mechanical: `kubectl apply -f` the captured state
  (or recreate the object, for deletes).
- Armed rollback points are listed in the [morning digest](autonomy.md#the-morning-digest)
  and visible in `kq replay` output.

Capture is best-effort: if the pre-state read fails, the mutation still runs —
the rollback point is a safety net, not a gate.

---

## Configuration & limitations

| Flag | Default | Effect |
|---|---|---|
| `FLIGHT_RECORDER_ENABLED` | `True` | Master switch. Off = no recording, replay returns 404 for new sessions. |

**Postgres required.** In SQLite mode (`USE_SQLITE=true`) recording is
disabled — there is no `decision_log` in the SQLite schema. The server logs
`flight_recorder: SQLite mode — recording disabled` at startup. If the table
is missing on Postgres, run `kubeintellect db-init`.

**Fire-and-forget guarantee.** `record()` never blocks and never raises:
events go onto an in-process queue and a background task batches them into
Postgres. A recorder outage (DB down, table missing, queue full) **degrades
auditability, never availability** — user responses and autonomous
investigations are unaffected. The trade-off is honest: during an outage,
events are dropped, not buffered to disk.

Redaction of recorded payloads follows the same flag as reflexion:
`REFLEXION_REDACT_SECRETS` (default `True`).

---

## Related

- [Autonomous Operations](autonomy.md) — every watchtower investigation is recorded here.
- [Memory Hierarchy](memory.md) — episodes summarize what the recorder logs in full.
- [Security](security.md) — roles and HITL gates that the recorded events document.
