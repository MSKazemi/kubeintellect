# Token Tracking

kube-q tracks token usage per response and per session, so you always know what a conversation costs.

---

## Response footer

After every response that includes usage data, kube-q shows a footer:

```
kube-q  (1.2s · 460 tokens)
```

This is the elapsed response time and the total tokens for that exchange (prompt + completion). If the server doesn't emit a `usage` block, the footer is omitted silently — no errors.

---

## Session summary

Use `/tokens` or `/cost` inside the REPL for a full breakdown:

```
┌─ Token Usage ─────────────────────────┐
│ This session:                         │
│   Prompt:     1,240 tokens            │
│   Completion: 3,890 tokens            │
│   Total:      5,130 tokens            │
│   Requests:   8                       │
│   Est. cost:  $0.0312                 │
│                                       │
│ Last response:                        │
│   120 in → 340 out ($0.0024)          │
└───────────────────────────────────────┘
```

Cost estimates are labeled "Est." — they are approximations based on per-model rate tables, not exact billing figures.

---

## Session list

`kq --list` shows a **Tokens** column for every session:

```
ID        Title                     Messages  Tokens   Last used
────────  ────────────────────────  ────────  ──────   ─────────
abc123    Debug failing pods        12        8,430    2 hours ago
def456    Scale deployment prod     5         2,100    yesterday
```

---

## Built-in rate table

| Model | Prompt | Completion |
|---|---|---|
| `kubeintellect-v2` | $0.003 / 1K | $0.006 / 1K |
| `gpt-4o` | $0.005 / 1K | $0.015 / 1K |
| `gpt-4o-mini` | $0.00015 / 1K | $0.0006 / 1K |
| `claude-sonnet-4-6` | $0.003 / 1K | $0.015 / 1K |

---

## Custom rates

Override rates for any model via environment variables or `.env`:

```bash
KUBE_Q_COST_PER_1K_PROMPT=0.002
KUBE_Q_COST_PER_1K_COMPLETION=0.008
```

This is useful when running a self-hosted model or when the backend uses non-standard pricing.
