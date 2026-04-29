# KubeIntellect Evaluation Results

Structured evaluation runs against a live Kind cluster with injected faults.

## Runs

| Run ID | Date | Scenarios | Pass rate | Avg judge | Branch |
|--------|------|-----------|-----------|-----------|--------|
| [v3-full_20260429_081102](v3-full_20260429_081102-report.md) | 2026-04-29 | 41 | 70.7% (29/41) | 31.3/40 | exp/v3 |

## Lessons Learned

- [V3 DeepAgents — lessons learned](v3-lessons.md): root-cause analysis of failures, fixes applied, and residual issues from the first full 42-scenario evaluation run on the DeepAgents architecture.

## Files

- `*-results.csv` — per-scenario raw scores (signal + judge), latency, tool calls, error status
- `*-report.md` — auto-generated per-scenario report with answer previews, error patterns, latency outliers
- `*-lessons.md` — hand-written lessons learned document with root causes, fixes, and improvement priorities
