# Demo Recordings

Live demos are published at **[kubeintellect.com/demos](https://kubeintellect.com/demos)**.

This directory holds the source `.cast` files (asciinema terminal recordings) used to produce them.

## Recording a demo

Requires a running Kind cluster (`make kind-kubeintellect-clean-deploy`).

```bash
make record-demo                    # default: deploy scenario
make record-demo SCENARIO=debug     # CrashLoopBackOff / OOMKilled diagnosis
make record-demo SCENARIO=security  # RBAC / privileged container audit
make record-demo SCENARIO=scale     # scale + rollout
make record-demo SCENARIO=hitl      # HITL tool-generation approval flow
```

After recording, trim and save:

```bash
make trim-demo    # interactive: cut dead time from the start, cap idle gaps
make play-demo    # preview the trimmed recording
make upload-demo  # publish to asciinema.org (returns a shareable URL)
```

## Scenarios

| Scenario | `SCENARIO=` | What it shows |
|---|---|---|
| Deploy | `deploy` | Deploying an app, checking rollout status |
| Debug | `debug` | Natural language root cause for CrashLoopBackOff / OOMKilled |
| Security | `security` | RBAC audit: who has cluster-admin? privileged containers? |
| Scale | `scale` | Scaling deployments with dry-run diff before apply |
| HITL | `hitl` | CodeGenerator writes a custom tool, user reviews and approves |

## Adding a recording to the README

1. Record: `make record-demo SCENARIO=<name>` — saves `demo-<name>-raw.cast`
2. Trim: `make trim-demo` — saves to `docs/demos/kubeintellect-demo-<name>.cast`
3. Upload: `make upload-demo` — prints a shareable URL
4. Replace the placeholder comment in `README.md` → Demo section:

```markdown
[![asciicast](https://asciinema.org/a/RECORDING_ID.svg)](https://asciinema.org/a/RECORDING_ID)
```
