# The architecture animation

A 37.5-second silent animation of what KubeIntellect is made of and how data moves through it:
seven layers, twenty-six components, four phases.

```bash
python3 animate.py     # -> out/architecture.mp4 (1080p, ~1.7 MB) + out/architecture.png
```

Two files matter. **`spec.py` is the architecture** — layers, components, and the flows between
them. **`animate.py` only draws it.** Palette, fonts and the mark are imported from
`../video/render.py`, so this and the narrated demo cannot drift apart visually.

## Why it is a spec file and not a drawing

A diagram is the easiest artifact to lie with, because nothing executes it. The repository
already had the worked example: `website/public/images/architecture.svg`, a Mermaid export from
**2026-03-29** whose nodes are `Task Router`, `Orchestrator`, `Final Aggregator` and
`Code Generator`. That is the **V1** design — frozen under ADR-001, cited by the paper — and it
contains no sensorium, no detector engine, no flight recorder, no memory hierarchy, no autonomy
ladder and no approval gate. It describes a different system, and it is referenced by no page on
the site. It was not reconciled into `spec.py`; it was set aside as describing V1.

So every component here carries the module it stands for, and the box is labelled with that
path. If you doubt a box, open the file.

## The part that is easy to get wrong

**Most of V4 is flag-gated, and a large share of those flags are off by default.** Four of the
twenty-six components are off in a stock install:

| Component | Flag | Why it is off |
|---|---|---|
| Anticipatory detection | `PREDICTIVE_DETECTION_ENABLED` | range-PromQL + OLS ETA; capped at A1 |
| Cortex V4 | `CORTEX_V4_ENABLED` | flip criterion is `cluster_resolved` parity with V2 |
| API-key roles | `REQUIRE_AUTH` | **with no keys set, every unauthenticated caller is `admin`** |
| Memory V5 slices | `MEMORY_SECURITY_HARDENING` | P1–P8 are additive and default-off |

They are drawn **dashed, dimmed, and chipped with the flag name**, and a standing caption names
all four. A diagram that drew them like everything else would be claiming a system nobody runs —
which is exactly the defect the video audit caught on 2026-08-28, where the narration said
"read-only by default" about a server whose `REQUIRE_AUTH` is `False`.

`AUTONOMY_LEVEL` is a third state: on, but at `A1`, so it is chipped with its value rather than
drawn as off.

## What stops it drifting

`v4/tests/test_the_architecture_drawing_matches_the_code.py` (19 tests) reads `spec.py` and
checks it against the code, not against a document:

- every `module` is a path that exists, and is specific enough to be worth opening;
- every `flag` is a real `Settings` field;
- every claimed default equals `Settings.model_fields[flag].default` — the **declared** default,
  not `settings.FLAG`, which is whatever the developer's `.env` happens to say and is how a
  drawing drifts with nobody editing it;
- a component with no flag must claim to be always-on;
- the off-by-default set is non-empty, and contains those four;
- no flow endpoint is a component that does not exist, and **the only edge into `kubectl`
  labelled as a write comes from the gate** — if the drawing ever shows a write path around the
  approval gate, that test fails;
- phase 3 starts at no human-facing component, because its whole point is that nobody asked;
- the V1 component names may not reappear.

It caught two errors while being written: the approval gate and the API-key roles were both
labelled with module paths that do not exist (`agent/nodes/tool_executor.py`,
`core/security.py`). The real ones are `tools/kubectl_tool.py` and `api/v1/auth.py`.

## Layout

Seven bands, top to bottom, in the order a request meets them: interface, perception, cognition,
governance, memory, record, substrate. Edges are routed orthogonally and every horizontal run
travels in a **gutter** between bands, with the label on it — the first version drew straight
lines, which put `gate -> kubectl` through `L1 episodes` and every label on top of a module path.
Jumps of more than one band use the routing corridor on the right rather than crossing whatever
is in between.

## Phases

| # | Title | What lights up |
|---|---|---|
| 1 | A human asks | query -> snapshot -> playbooks -> recall -> reason -> answer |
| 2 | It wants to change something | every mutating command stops at the gate, and is recorded |
| 3 | Nobody asked | `--watch` -> observation -> finding -> A1 investigation |
| 4 | Afterwards | recorder -> postmortem + digest; episodes -> consolidation -> KG |

## Artifacts

`out/` is excluded from both gits (`.dualgit/exclude.*.txt`) exactly like the video's — the
sources are tracked and the mp4 is regenerated. Nothing here has been published: putting the
animation on the website or in the README is outward-facing and waits for the owner.
