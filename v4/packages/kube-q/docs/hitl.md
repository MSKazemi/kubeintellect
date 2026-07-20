# Human-in-the-Loop (HITL)

When the AI backend requests approval before executing a potentially destructive or irreversible action, kube-q pauses and waits for your explicit confirmation. Nothing runs until you say so.

---

## How it works

When a HITL action is pending, kube-q displays an approval panel and changes the prompt to `HITL>`:

```
╭─ Action requires approval ──────────────────────╮
│ The following action is pending approval:       │
│                                                 │
│   kubectl delete deployment nginx --namespace   │
│   production                                    │
│                                                 │
│ Type /approve to proceed or /deny to cancel.   │
╰─────────────────────────────────────────────────╯
HITL> 
```

The AI backend decides which actions require approval — typically anything that modifies cluster state (deletes, restarts, scaling, patching).

---

## Commands

| Command | Effect |
|---|---|
| `/approve` | Execute the pending action — the AI continues |
| `/deny` | Cancel the pending action — nothing is applied |

Once you approve or deny, the REPL returns to the normal prompt.

---

## Safety guarantees

- kube-q **never executes** a HITL action without an explicit `/approve`
- Closing the REPL (`Ctrl+D`, `/quit`) while a HITL action is pending is treated as a deny
- The action details (command, risk level, diff) are shown before you decide — what you see is exactly what will run

---

## In single-query mode

HITL is not supported in `kq --query` (non-interactive) mode. If the backend raises a HITL request during a non-interactive query, the action will be denied automatically. Use the interactive REPL for workflows that may require approval.
