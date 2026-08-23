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
or delete a stored record without the chain failing verification afterwards.
The replay endpoint recomputes the full chain on every read and reports the
verdict before streaming a single event.

**What the chain does not prove.** *Intact* and *complete* are two different
claims, and only the first is a property of the hash chain. The write path is
fire-and-forget (see below), so a recorder outage loses events that were never
stored — nothing about a hash can detect the absence of a row that was never
written. So the recorder records its own losses: when a batch cannot be
written, the count and the cause are carried forward and written into the
chain as a `recorder_gap` record the moment writes recover.

```json
{
  "type": "recorder_gap",
  "dropped": 7,
  "reason": "the decision_log table was missing",
  "message": "7 recorded event(s) were LOST at this point — …"
}
```

That marker is chained like any other row, so it cannot be removed without
breaking verification. `kq replay` exits **5** on an episode that contains one,
`kq export` does the same, and `kq postmortem` prints a **RECORD INCOMPLETE**
banner next to the ✅ chain verdict. Read a gap as: *nothing here was altered,
but this is not the whole sequence.*

A lost batch also never leaves a hole in the `seq` numbering. It used to: the
in-process chain head advanced before the insert was known to have succeeded,
so the next batch skipped the lost sequence numbers and verification reported
the episode as tampered with. A database blip is not tampering, and saying so
on the record devalues the verdict that matters.

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
  0  status           Triaging request…
  1  plan             plan — 3 step(s), 0 done
  2  tool_call        tool run_kubectl: kubectl get pods -n prod
  3  tool_result      result: NAME READY STATUS RESTARTS AGE …
  …
42 records · ✓ chain intact
```

The `type` column is the row's recorded `kind`, and the summary is produced by
`ki_protocol.record.summarise_record` — the same function the postmortem builder uses, so
the two views of one episode describe it identically. Every recorded kind has a summary;
`kq replay findings:<cluster-id>` renders a detector-firing episode the same way
(`detector crashloop fired on prod/pod/api-0`).

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Replay succeeded, chain intact |
| `1` | Episode not found (or the request failed) |
| `2` | Usage error |
| `3` | Replay succeeded but the **chain is broken** — records may have been tampered with |
| `4` | Replay rendered but the integrity verdict never arrived — **unverified**, which is not the same as intact |
| `5` | Chain intact, but the episode is **incomplete** — the recorder lost events and said so (`recorder_gap`) |

Exit codes `3`, `4` and `5` are the ones to alert on in scripts, and they are
three different problems: `3` the record may have been altered, `4` nothing
checked it, `5` the record is genuine but has holes in it.

### `GET /v1/episodes/{episode_id}/replay`

The HTTP endpoint streams Server-Sent Events. The **first frame is always a
meta record carrying the chain verdict**, then each recorded payload in `seq`
order, then `[DONE]` — each payload carrying its row's `kind` as `type`, whether or not the
stored payload repeated it:

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

Before **every mutating `kubectl` command** (anything the tool classifies as a write —
the same test the approval gate uses, so the two cannot disagree; dry-runs excluded),
the tool layer captures the current YAML of the targeted
objects and records it as a `rollback_point` event in the flight recorder:

```json
{
  "type": "rollback_point",
  "rollback_id": "rb-1a2b3c4d5e6f",
  "command": "kubectl scale deployment/web --replicas=0 -n prod",
  "pre_state": ["apiVersion: apps/v1\nkind: Deployment\n…"],
  "restorable": true,
  "capture_notes": []
}
```

- IDs are `rb-` followed by 12 hex characters.
- The captured YAML is **redacted** (same secret-stripping as
  [reflexion](reflexion.md)) and capped at 4000 characters.
- **`restorable` says whether the capture can actually be applied.** See the next section —
  it is the difference between a restore point and a photograph of one.
- Recovery, *when `restorable` is true*, is manual but mechanical: pipe the captured state into
  `kubectl apply -f -` (or recreate the object, for deletes). Through KubeIntellect the
  stdin form is the only one accepted — see
  [security](security.md#4-hitl-human-in-the-loop-gate).
- Captures are listed in the [morning digest](autonomy.md#the-morning-digest) with their
  restorability, and visible in `kq replay` output.

Capture is best-effort: if the pre-state read fails, the mutation still runs —
the rollback point is a safety net, not a gate.

### Restorable vs recorded

A capture is stored in a database, so it is redacted and capped first, and **either
transformation can leave something that is no longer the object**. Measured against real
kubectl output (`bitnami/kubectl:latest`, 2026-08-20):

| Captured object | What is stored | What kubectl says about it |
|---|---|---|
| a **Secret** | the key names survive (`password:`, `token:`); every value is replaced with `<redacted>` | accepted — and it **overwrites the live credentials with the placeholder** |
| a **ConfigMap** with token-shaped values | every value replaced with `<redacted-token>` | accepted — `configmap/app-config`. It **applies cleanly and overwrites the real values** |
| anything over 4000 characters (this project's own chart `values.yaml` is 7.4 KB) | cut mid-line, ending `[...]` | `error parsing STDIN: … did not find expected key` |

The first two rows are the ones to understand: a redacted capture can be perfectly valid YAML,
so "it applied without error" is not evidence that it restored anything.

> **Changed 2026-08-20.** The Secret row used to read *"the line `kind: Secret` contains a
> redaction keyword and is dropped"* — true, and an accident. The redactor classified each line
> on its own and dropped any line containing a keyword, which in YAML deletes the **label** and
> keeps the **value** on the next line. The same rule that removed `kind: Secret` left
> `value: hunter2` from a Deployment's `env:` block sitting in the database. Redaction is now
> line-aware: structural fields stay, values go. A Secret capture is still `restorable: false`
> — for the honest reason that its data is gone, not because the manifest was mangled.

Redaction is not the flaw and is not optional — the alternative is credentials sitting in
Postgres. So each capture is compared with what kubectl produced and the record states which
of the two it is:

- `restorable: true` — byte-identical to the live object; the recovery above works.
- `restorable: false` — redacted or truncated. Keep it as **evidence of what the object looked
  like**; do not pipe it into `kubectl apply`. `capture_notes` says what changed
  (`secret db-creds: redacted: 2 value(s) replaced`, `redacted: 1 line(s) dropped`,
  `truncated at 4000 chars (object is 8007 chars)`). The count comes from
  `redact.count_redactions`, which owns the full marker vocabulary — until 2026-08-20 this note
  counted three of the six markers by hand, so a capture whose only redaction was a PEM private
  key or a Secret's `data:` block said, in full, `redacted`.
- field absent — recorded before this was tracked; the digest reports it as *unknown*, never
  as armed.

---

## Configuration & limitations

| Flag | Default | Effect |
|---|---|---|
| `FLIGHT_RECORDER_ENABLED` | `True` | Master switch. Off = no recording, replay returns 404 for new sessions. |

**Postgres required.** In SQLite mode (`USE_SQLITE=true`) recording is
disabled — there is no `decision_log` in the SQLite schema. The server logs
`flight_recorder: SQLite mode — recording disabled` at startup. If the table
is missing on Postgres, run `kubeintellect db-init`. The
[morning digest](autonomy.md#the-morning-digest) reports this state explicitly
rather than as a quiet watch — an empty digest and an unrecorded one are
different answers.

**Fire-and-forget guarantee.** `record()` never blocks and never raises:
events go onto an in-process queue and a background task batches them into
Postgres. A recorder outage (DB down, table missing, queue full) **degrades
auditability, never availability** — user responses and autonomous
investigations are unaffected. The trade-off is honest: during an outage,
events are dropped, not buffered to disk — and the drop is itself recorded, as
a `recorder_gap` row in the chain (see [Tamper evidence](#tamper-evidence)), so
a replay states what it lost instead of reading as a complete record.

Redaction of recorded payloads follows the same flag as reflexion:
`REFLEXION_REDACT_SECRETS` (default `True`).

---

## Related

- [Autonomous Operations](autonomy.md) — every watchtower investigation is recorded here.
- [Memory Hierarchy](memory.md) — episodes summarize what the recorder logs in full.
- [Security](security.md) — roles and HITL gates that the recorded events document.
