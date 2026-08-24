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
breaking verification. It covers **every** way an event fails to reach the
table, including the recorder never having started: if the pod loses the
startup race with Postgres, each `record()` is carried as a pending gap and the
whole outage is written into the chain once the pool comes up (see [When the
recorder is not running](#when-the-recorder-is-not-running)). `kq replay` exits **5** on an episode that contains one,
`kq export` does the same, and `kq postmortem` prints a **RECORD INCOMPLETE**
banner next to the ✅ chain verdict. Read a gap as: *nothing here was altered,
but this is not the whole sequence.*

A third banner covers what the *document* could not fetch, as opposed to what the *records*
lost. A postmortem enriches the deterministic timeline from two best-effort sources — root cause
and outcome from the L1 episode store, and the optional LLM narrative — and both used to return
"nothing" whether they had nothing to add or had failed. A revoked grant on `episodes` therefore
produced a postmortem with no **Root cause** section, byte-identical to one where the
investigation genuinely never concluded, under a ✅ *audit chain verified intact* banner. When an
enrichment fails, the document now says so next to the chain verdict:

```text
> ✅ Audit chain verified intact — every event below is tamper-evident.
> ⚠️ **POSTMORTEM INCOMPLETE** — could not read: root cause / outcome (episode store: permission
  denied for relation episodes). A section missing below is NOT evidence that it was empty.
```

An episode with no row, and a narrative that is simply switched off, are not failures and print
nothing — the banner is only worth reading if it never fires when nothing went wrong.

A lost batch also never leaves a hole in the `seq` numbering. It used to: the
in-process chain head advanced before the insert was known to have succeeded,
so the next batch skipped the lost sequence numbers and verification reported
the episode as tampered with. A database blip is not tampering, and saying so
on the record devalues the verdict that matters.

Token frames (one per LLM token) are deliberately not recorded — they would
bloat the table by orders of magnitude with no audit value. The final answer
text is recorded as a normal event.

### The link check alone cannot see a truncation

A hash chain catches an edit, a reorder and an interior delete, because each one breaks a
link. Deleting an episode's **newest** rows breaks nothing — what remains is a shorter,
perfectly valid chain. Measured 2026-08-24: a 9-event episode with its last 3 events deleted
still verified, and the postmortem printed *"✅ Audit chain verified intact"* over it.

Each episode's chain is therefore anchored in `decision_log_head`, written alongside the
events on every successful flush, exactly as `memory_chain_head` anchors the memory audit
chain. A chain shorter than its anchor contradicts it. Three consequences:

- **`verify_chain()` is the link check only.** Anything that shows a human a tamper verdict
  calls `verify_episode()`, which is the link check *and* the anchor.
- **An append after a truncation continues past the head**, not past the surviving tail — a
  re-anchoring append would close over the hole and destroy the only evidence it existed.
- **An episode with an anchor but no surviving rows is not an empty episode.** The postmortem
  says every event has been removed, and `GET /v1/episodes/{id}/replay` answers **409**, not
  the `404` that would launder a total truncation into "this never existed".

It remains tamper-**evidence**, not prevention: an attacker with full database write can forge
the anchor too. What it removes is the free tamper — the one that needs no second edit. A head
that cannot be read, or an episode older than the anchor, is never reported as tampering.


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
| `1` | Episode not found, the decision log is **unavailable**, or the request failed |
| `2` | Usage error |
| `3` | Replay succeeded but the **chain is broken** — records may have been tampered with |
| `4` | Replay rendered but the integrity verdict never arrived — **unverified**, which is not the same as intact |
| `5` | Chain intact, but the episode is **incomplete** — the recorder lost events and said so (`recorder_gap`) |

Exit codes `3`, `4` and `5` are the ones to alert on in scripts, and they are
three different problems: `3` the record may have been altered, `4` nothing
checked it, `5` the record is genuine but has holes in it.

Exit `1` covers two states that read very differently on screen. *"No recorded
episode 'X'"* is a claim about that episode. *"The decision log is unavailable"*
is a claim about the recorder, and explicitly says it is **not** a statement that
the episode has no records — the audit trail was never consulted:

```text
$ kq replay 3f9c…
The decision log is unavailable — the flight recorder is not running — no decision log to read
This is NOT a statement that episode '3f9c…' has no records. Check that the flight
recorder is enabled and its database is reachable: kubeintellect status
```

`kq export` makes the same distinction: exit `4` means the episode has no events,
while an unreadable recorder exits `1` and exports nothing.

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

Returns `404` when no records exist for the episode, and `503` when the log could
not be read at all (recorder not running, or its database unreachable). Keeping
those apart is the whole point: a `404` asserts the episode does not exist, which
is the most expensive wrong answer an audit surface can give. Unlike
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

### When the recorder is not running

Recording needs Postgres, and the API pod is routinely scheduled before Postgres
accepts connections. That is not fatal: the pool is retried every 30 seconds,
and reconnecting completes startup properly (queue + drain task), so a rollout
race costs no restart and no permanently unrecorded process.

Ask the process which state it is in — an episode with no rows and a recorder
that never ran give the same `404` shape only to someone who already suspected
it:

```bash
curl -s localhost:8000/healthz | jq .recorder
# {"enabled": true,  "state": "ready",       "reason": "",  "lost_while_down": 0}
# {"enabled": false, "state": "unavailable", "reason": "Connect call failed ...", "lost_while_down": 77}
```

| `state` | Meaning |
|---|---|
| `ready` | Decisions are being recorded. |
| `flag` | `FLIGHT_RECORDER_ENABLED=false`. Configuration, not a fault. |
| `sqlite` | `USE_SQLITE=true`; recording needs Postgres. Configuration, not a fault. |
| `unavailable` | Postgres refused the connection. **Nothing is being recorded**; a reconnect loop is retrying and the loss is being carried. |

`lost_while_down` counts events that were never persisted because the recorder
was not running. Those same events become `recorder_gap` rows on the first
successful flush, so the replay of an affected episode states its own hole.
Configuration-off states carry nothing — there is no chain to be honest about —
and skipped kinds (token frames) are not losses.

**Fire-and-forget guarantee.** `record()` never blocks and never raises:
events go onto an in-process queue and a background task batches them into
Postgres. A recorder outage (DB down, table missing, queue full) **degrades
auditability, never availability** — user responses and autonomous
investigations are unaffected. The trade-off is honest: during an outage,
events are dropped, not buffered to disk — and the drop is itself recorded, as
a `recorder_gap` row in the chain (see [Tamper evidence](#tamper-evidence)), so
a replay states what it lost instead of reading as a complete record.

Redaction of recorded payloads follows the same flag as reflexion:
`REFLEXION_REDACT_SECRETS` (default `True`). With the flag on, every string in the payload is
redacted **wherever it sits** — a value, a list element, a string used as a **dict key**, or a
member of a **set**. Past the walk's depth bound of 6 levels the remaining subtree is replaced by
`<redacted-unscannable-depth>` and not emitted.

!!! warning "A depth bound is also a redaction bound (fixed 2026-08-24)"

    The bound exists so a self-referential payload cannot hang the drain task. Until 2026-08-24
    it hit that bound and returned the unscanned subtree **verbatim**, so the same line that
    stopped a cycle also stopped the redaction — in a module whose output is persisted
    permanently, that is failing in the one direction it must not. Dict keys and sets were never
    walked at all; a set then reached the column as `str(the_set)` via `json.dumps(default=str)`.

    No call site produced any of those three shapes — all six build payloads of depth ≤ 3 out of
    fixed field names — so this was a latent fail-open, not an observed leak. It is listed here
    because a hygiene gate whose reach depends on the shape of the caller's dict is not a gate,
    which is the same reason the one-level walk was fixed in 2026-08-20.

    Keys use the narrower `redact_identifier`. The full redactor drops any line that *looks*
    secret, and `redact_secrets("token")` and `redact_secrets("password")` both return
    `# <redacted-line>` — redacting keys with it would rename two ordinary field names to the
    same string and silently merge two fields of a tamper-evident record into one. When two
    genuinely secret keys do redact to the same marker they are suffixed (`…#2`) rather than
    merged, because losing a field in order to hide something in it is the wrong trade.

---

## Related

- [Autonomous Operations](autonomy.md) — every watchtower investigation is recorded here.
- [Memory Hierarchy](memory.md) — episodes summarize what the recorder logs in full.
- [Security](security.md) — roles and HITL gates that the recorded events document.
