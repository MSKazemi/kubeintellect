# Snap packaging

[`snapcraft.yaml`](snapcraft.yaml) builds the **`kubeintellect` snap** — the `kq`
terminal client, under strict confinement, for amd64 and arm64.

It packages the **client only**. The server stays a container image (GHCR /
Docker Hub) because it needs Postgres and LLM credentials; a snap would duplicate
that path badly. `kq` talks to whatever backend you point it at.

## Status

**Published.** `kubeintellect` is on the Snap Store at
<https://snapcraft.io/kubeintellect>, `latest/stable`:

```bash
sudo snap install kubeintellect
sudo snap connect kubeintellect:dot-kube    # read ~/.kube (kubeconfig)
sudo snap connect kubeintellect:dot-kube-q  # read/write ~/.kube-q (config, sessions)
sudo snap alias kubeintellect.kq kq         # optional: the short command name

kq --version
```

The two `snap connect` steps are not optional and not auto-connected — see
[Why strict confinement](#why-strict-confinement-and-what-that-costs) below. The
store declaration for the two `personal-files` plugs took four rejected revisions
and publisher vetting to obtain; revisions 1–4 (1.5.0) were all rejected for
requesting a super-privileged interface without one.

Building locally is still the way to test a change before it ships.

## Build and test locally

```bash
snapcraft                                  # builds in an LXD container (~5 min first run)
sudo snap install --dangerous ./kubeintellect_*.snap
sudo snap connect kubeintellect:dot-kube   # read ~/.kube (kubeconfig)
sudo snap connect kubeintellect:dot-kube-q # read/write ~/.kube-q (config, sessions)
sudo snap alias kubeintellect.kq kq        # optional: the short command name

kubeintellect --version
snap connections kubeintellect             # verify both plugs are connected
```

`--dangerous` is required because a locally built snap is unsigned. To remove it
again: `sudo snap remove --purge kubeintellect`.

> **If the build fails with a network error inside the container** and you also run
> Docker: Docker sets the iptables `FORWARD` policy to `DROP`, which blackholes
> LXD's bridge. Fix with
> `sudo iptables -I DOCKER-USER -i lxdbr0 -j ACCEPT && sudo iptables -I DOCKER-USER -o lxdbr0 -j ACCEPT`.

## Why strict confinement, and what that costs

Most Kubernetes CLI snaps (`kubectl`, `helm`, `k9s`) use **classic** confinement,
which turns the sandbox off entirely. This one doesn't, because it can afford not
to: `kq` is an HTTP client, and its filesystem needs are two known directories.

That choice has three visible consequences:

| | |
|---|---|
| **Two `snap connect` steps** | The `home` interface deliberately excludes top-level dot-directories, so `~/.kube` and `~/.kube-q` need explicit `personal-files` plugs. These are not auto-connected until the store grants a declaration. |
| **`HOME` is rewritten** | Strict snaps get `HOME=$SNAP_USER_DATA`, which would hide the user's real `~/.kube` and scatter `kq` state under `~/snap/`. The app definitions set `HOME=$SNAP_REAL_HOME` to undo that; the `personal-files` plugs are what actually gate access. |
| **`kubectl` is not visible** | `kube_q/core/kubeconfig.py` prefers `kubectl config get-contexts` and falls back to parsing `~/.kube/config` directly. Inside the sandbox the host's `kubectl` is unreachable, so the fallback path is always the one that runs. Context listing and Tab completion still work; this is why `dot-kube` is a hard requirement rather than a nicety. |
| **`$KUBECONFIG` outside `~/.kube`** | Not readable. Point `KUBECONFIG` at a file under `~/.kube`, or use `--dangerous`-installed classic builds for exotic layouts. |

## How the part is assembled

One `python` part, which is slightly unusual and deliberately so:

- **`source:` is `v4/packages/ki-protocol`**, not `packages/kube-q`. Sourcing the
  client directly would pull its Next.js demo UI (`web/`, ~600 MB of
  `node_modules`) through the pull step for no reason. `override-pull` copies just
  `kube_q/`, `pyproject.toml`, `README.md` and `LICENSE` next to it instead.
- **Both packages install in a single `pip` invocation** (`python-packages: ['.',
  './vendor-kube-q']`) so that `kube-q`'s `ki-protocol>=1.0.0` requirement resolves
  against the local copy. `ki-protocol` is not on PyPI yet; a two-step install
  would try to fetch it and fail.
- **No Python is bundled.** Strict snaps may use the base snap's interpreter;
  core24 provides 3.12, which matches `requires-python`.
- **The version is adopted** from `kube-q`'s `pyproject.toml` via `craftctl set
  version`, so the snap version tracks the client release with nothing to keep in
  sync by hand.

## Publishing

CI does the build on every PR that touches `snap/` or either packaged Python
project, and the [`Snap` workflow](../.github/workflows/snap.yml) publishes on
manual dispatch. It needs a `SNAPCRAFT_STORE_CREDENTIALS` repository secret:

```bash
snapcraft export-login --snaps kubeintellect \
    --acls package_access,package_push,package_update,package_release -
```

First-time store setup is **done** and is recorded here so the next snap does not
have to rediscover it:

1. `snapcraft register kubeintellect` — the name is registered.
2. A snap declaration for the two `personal-files` plugs was requested on the
   [snapcraft forum store-requests category](https://forum.snapcraft.io/c/store-requests/19)
   and granted after publisher vetting. `personal-files` is super-privileged: an
   upload without the declaration is auto-rejected, which is what happened to
   revisions 1–4. Expect days, not minutes.
3. Release to `edge` first, verify on a clean machine, then promote.

Check what the store is actually serving before claiming a version is out:

```bash
snap info kubeintellect | sed -n '/^channels:/,$p'
```
