"""Run a namespace-bound investigation's kubectl as a Kubernetes identity that can act only in that namespace.

Why this exists (ADR-007 Phase 6). Every other bound on an autonomous investigation — the namespace binding, the
resource block, the dry-run and repeat guards — is enforced by `kubectl_tool` reading the command string and predicting
how kubectl will parse it. Ten independent review passes (2026-11-14, `papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md`
§7e) kept finding strings that kubectl reads differently: a repeated `-n` reached another namespace, `get pods,secrets`
and `get --raw …/secrets` read Secrets. Each was fixed, and the next pass found another. A parser that guesses another
program's parser cannot be shown complete.

So the server stops relying on it for the authority it hands out. A session bound to namespace N runs kubectl with a
kubeconfig whose only credential is a short-lived token (TokenRequest, `kubectl create token`) for the ServiceAccount
`SESSION_IDENTITY_SERVICE_ACCOUNT` *in N*, bound by a RoleBinding *in N* to the ClusterRole
`SESSION_IDENTITY_CLUSTER_ROLE` (shipped by the Helm chart: reads without Secrets, the writes a fix needs, no workload
creation, no exec). The API server then decides, from RBAC, whatever string the model wrote: another namespace, a
Secret, a new Job, `--as`, all `Forbidden`. The string gates stay as the first line; this is the one that holds.

Fail closed. If the identity cannot be had — the ServiceAccount is missing and may not be provisioned, the ClusterRole
is not installed, the token request is refused — `IdentityUnavailable` is raised and the caller refuses to run kubectl.
It never falls back to the server's own, broader identity.

The token is never logged. It lives in a read-only (0400) file in a 0700 directory created for this process and removed
at exit, and `kubectl config view` (the only verb that could print it) redacts tokens and refuses `--raw`.

That file is the one kubectl is pointed at, so kubectl could write to it: the independent review of this phase
(2026-11-15) rewrote it with `kubectl config set …` and the session's next call ran with the server's own credentials.
`kubectl_tool` refuses every command that changes a kubeconfig; behind that, the file is read-only and its content is
compared with what was written before every reuse, and a file that changed is never used — the identity is re-issued.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import yaml

from app.core.config import settings

logger = logging.getLogger(__name__)

# A token is re-minted when less than this is left, so a kubectl call never starts with one about to expire.
_REFRESH_MARGIN_SECONDS = 120.0
_NAMESPACE_RE = re.compile(
    r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
)  # used with fullmatch: `$` would accept "ns\n"
# The cache key of the read-only identity: not a legal namespace name, so it cannot collide with one.
READONLY_KEY = "<readonly>"
_IN_CLUSTER_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
# Only these keys of the base kubeconfig's cluster entry are carried over: where the API server is and how to trust it.
_CLUSTER_KEYS = (
    "server",
    "certificate-authority-data",
    "insecure-skip-tls-verify",
    "tls-server-name",
    "proxy-url",
)
_MANAGED_LABELS = {
    "app.kubernetes.io/managed-by": "kubeintellect",
    "kubeintellect.io/purpose": "session-identity",
}


class IdentityUnavailable(Exception):
    """No namespace-scoped identity could be had for this session; kubectl must not run."""


_lock = threading.Lock()
_cache: dict[
    tuple[str, str], tuple[str, float, str]
] = {}  # (base kubeconfig, namespace) -> (kubeconfig path, expiry, sha256 of the content written)
# (base kubeconfig, namespace) whose ServiceAccount + RoleBinding this process has applied. With provisioning on, the
# binding is applied once per process even when the ServiceAccount already exists, so a RoleBinding of that name to
# another role is reported (the API server refuses to change a roleRef), never used.
_provisioned: set[tuple[str, str]] = set()
_dir: str | None = None


def base_kubeconfig() -> str:
    """The server's own kubeconfig (empty in-cluster), which mints the session tokens and never runs a session."""
    return (
        os.path.expanduser(settings.KUBECONFIG_PATH) if settings.KUBECONFIG_PATH else ""
    )


def clear_cache() -> None:
    """Test helper: forget every minted token (the files are removed with the directory at exit)."""
    with _lock:
        _cache.clear()
        _provisioned.clear()


def forget(namespace: str) -> None:
    """Drop a namespace's token, e.g. after the API server answered Unauthorized; the next call mints a new one."""
    with _lock:
        for key in [k for k in _cache if k[1] == namespace]:
            del _cache[key]


def _digest(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _pod_namespace() -> str:
    """The namespace of the pod this server runs in ('' outside a pod)."""
    try:
        with open(
            "/var/run/secrets/kubernetes.io/serviceaccount/namespace", encoding="utf-8"
        ) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _private_dir() -> str:
    global _dir
    if _dir is None or not os.path.isdir(_dir):
        _dir = tempfile.mkdtemp(
            prefix="ki-session-identity-"
        )  # mkdtemp creates it 0700
        atexit.register(shutil.rmtree, _dir, True)
    return _dir


def _run(
    cmd: list[str], base_kubeconfig: str, stdin: str | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "KUBECONFIG": base_kubeconfig}
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=settings.KUBECTL_TIMEOUT_SECONDS,
        env=env,
        shell=False,
    )


def _reason(proc: subprocess.CompletedProcess) -> str:
    from app.utils.redact import redact_secrets

    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    return redact_secrets(text[0] if text else f"exit {proc.returncode}", max_chars=240)


def _base_view(base_kubeconfig: str) -> tuple[dict, str | None]:
    """Where the API server is and how to trust it, plus the default namespace, from the server's own kubeconfig (or
    the pod's in-cluster config). Nothing else is read out of it: never its credentials."""
    in_cluster = os.environ.get("KUBERNETES_SERVICE_HOST")
    if base_kubeconfig:
        proc = _run(
            ["kubectl", "config", "view", "--minify", "--flatten", "-o", "json"],
            base_kubeconfig,
        )
        if proc.returncode == 0:
            try:
                view = json.loads(proc.stdout or "{}") or {}
            except json.JSONDecodeError:
                view = {}
            clusters = view.get("clusters") or []
            if (
                clusters
                and isinstance(clusters[0].get("cluster"), dict)
                and clusters[0]["cluster"].get("server")
            ):
                src = clusters[0]["cluster"]
                contexts = view.get("contexts") or [{}]
                default_ns = ((contexts[0] or {}).get("context") or {}).get("namespace")
                return {k: src[k] for k in _CLUSTER_KEYS if k in src}, default_ns
    if in_cluster:
        # The chart sets KUBECONFIG_PATH="" so kubectl uses the pod's own ServiceAccount; the API server address and CA
        # are the documented in-cluster ones.
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        host = f"[{in_cluster}]" if ":" in in_cluster else in_cluster
        # An unscoped read means what it meant with the pod's own config: the pod's namespace, not `default`.
        return {
            "server": f"https://{host}:{port}",
            "certificate-authority": _IN_CLUSTER_CA,
        }, (_pod_namespace() or None)
    raise IdentityUnavailable(
        "the server's kubeconfig names no cluster to issue a session identity for"
    )


def _token_expiry(token: str, requested: int) -> float:
    """The token's own `exp` claim (read, not verified — the API server verifies it), else the requested lifetime."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        exp = float(claims["exp"])
        return time.time() + min(exp - time.time(), float(requested))
    except Exception:  # noqa: BLE001 - an unreadable claim only means the requested lifetime is assumed
        return time.time() + requested


def _provision(namespace: str, base_kubeconfig: str) -> None:
    """Create the session ServiceAccount and its RoleBinding in `namespace` (idempotent `kubectl apply`)."""
    sa, role = (
        settings.SESSION_IDENTITY_SERVICE_ACCOUNT,
        settings.SESSION_IDENTITY_CLUSTER_ROLE,
    )
    # A RoleBinding to a ClusterRole that does not exist is accepted and grants nothing, so every call would be
    # Forbidden with no hint why. Checked first, so the operator gets the cause.
    check = _run(["kubectl", "get", "clusterrole", role, "-o", "name"], base_kubeconfig)
    if check.returncode != 0:
        raise IdentityUnavailable(
            f"the ClusterRole {role!r} that session identities are bound to is not installed "
            f"(Helm: sessionIdentity.enabled=true): {_reason(check)}"
        )
    meta = {"namespace": namespace, "labels": dict(_MANAGED_LABELS)}
    manifest = yaml.safe_dump_all(
        [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": sa, **meta},
                "automountServiceAccountToken": False,
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": sa, **meta},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": role,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": sa, "namespace": namespace}
                ],
            },
        ],
        sort_keys=False,
    )
    proc = _run(["kubectl", "apply", "-f", "-"], base_kubeconfig, stdin=manifest)
    if proc.returncode != 0:
        raise IdentityUnavailable(
            f"could not provision the session identity in {namespace!r}: {_reason(proc)}"
        )
    logger.info(
        "session_identity: provisioned ServiceAccount %s and RoleBinding in %s",
        sa,
        namespace,
    )


def _mint(
    sa: str, namespace: str, base_kubeconfig: str, *, provision: bool
) -> tuple[str, float]:
    seconds = settings.SESSION_IDENTITY_TOKEN_SECONDS
    cmd = ["kubectl", "create", "token", sa, "-n", namespace, f"--duration={seconds}s"]
    if provision:
        with _lock:
            done = (base_kubeconfig, namespace) in _provisioned
        if not done:
            _provision(namespace, base_kubeconfig)
            with _lock:
                _provisioned.add((base_kubeconfig, namespace))
    proc = _run(cmd, base_kubeconfig)
    if proc.returncode != 0 and "notfound" in (proc.stderr or "").lower().replace(
        " ", ""
    ):
        if not provision:
            raise IdentityUnavailable(
                f"ServiceAccount {sa!r} does not exist in {namespace!r}"
                + (
                    " and SESSION_IDENTITY_PROVISION is off"
                    if sa == settings.SESSION_IDENTITY_SERVICE_ACCOUNT
                    else ""
                )
            )
        _provision(namespace, base_kubeconfig)
        with _lock:
            _provisioned.add((base_kubeconfig, namespace))
        proc = _run(cmd, base_kubeconfig)
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token or any(c.isspace() for c in token):
        raise IdentityUnavailable(
            f"the token request for {sa!r} in {namespace!r} failed: {_reason(proc)}"
        )
    return token, _token_expiry(token, seconds)


def _valid_namespace(namespace: str) -> bool:
    return bool(_NAMESPACE_RE.fullmatch(namespace or "")) and len(namespace) <= 63


def _issue(
    key: tuple[str, str],
    name: str,
    sa: str,
    sa_namespace: str,
    *,
    provision: bool,
    context_namespace: str | None,
) -> str:
    """Mint (or reuse) a token for `sa` in `sa_namespace` and write the kubeconfig that holds only it."""
    base_kubeconfig = key[0]
    with _lock:
        hit = _cache.get(key)
    if hit and hit[1] - time.time() > _REFRESH_MARGIN_SECONDS:
        current = _digest(hit[0])
        if current == hit[2]:
            return hit[0]
        if current is not None:
            logger.warning(
                "session_identity: the kubeconfig of %s changed after it was written; not used, re-issued",
                name,
            )
        with _lock:
            _cache.pop(key, None)
    try:
        cluster, default_ns = _base_view(base_kubeconfig)
        token, expires = _mint(sa, sa_namespace, base_kubeconfig, provision=provision)
        path, digest = _write_kubeconfig(
            name, cluster, token, context_namespace, default_ns
        )
    except subprocess.TimeoutExpired as exc:
        raise IdentityUnavailable(
            f"the API server did not answer in time ({exc.timeout}s)"
        ) from None
    except FileNotFoundError:
        raise IdentityUnavailable("kubectl is not installed") from None
    except OSError as exc:
        # PermissionError, a full disk, an unexecutable kubectl: still no identity, so still no kubectl — but reported
        # as such, not as a crash of the investigation (review finding 6).
        raise IdentityUnavailable(
            f"could not issue the session identity: {exc.__class__.__name__}: {exc}"
        ) from None
    with _lock:
        _cache[key] = (path, expires, digest)
    logger.info(
        "session_identity: issued a %ss token for %s in %s",
        settings.SESSION_IDENTITY_TOKEN_SECONDS,
        sa,
        sa_namespace,
    )
    return path


def _write_kubeconfig(
    name: str,
    cluster: dict,
    token: str,
    context_namespace: str | None,
    default_ns: str | None,
) -> tuple[str, str]:
    """Write the kubeconfig holding only `token` read-only (0400) and return its path and content digest."""
    context: dict = {"cluster": "cluster", "user": "session"}
    ns = context_namespace if context_namespace is not None else default_ns
    if ns:
        context["namespace"] = ns
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "cluster", "cluster": cluster}],
        "users": [{"name": "session", "user": {"token": token}}],
        "contexts": [{"name": "session", "context": context}],
        "current-context": "session",
    }
    folder = _private_dir()
    path = os.path.join(folder, f"{name}.kubeconfig")
    content = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.")
    try:
        os.fchmod(
            fd, 0o400
        )  # read-only to its owner too: kubectl cannot rewrite it in place
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(
            tmp, path
        )  # a rename, so a read-only file of the same name is replaced, not written
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path, hashlib.sha256(content).hexdigest()


def kubeconfig_for(namespace: str, base_kubeconfig: str) -> str:
    """Path of a kubeconfig whose only credential is a short-lived token for the session identity in `namespace`.

    Raises `IdentityUnavailable` instead of ever returning the server's own identity.
    """
    if not _valid_namespace(namespace):
        raise IdentityUnavailable(f"{namespace!r} is not a namespace name")
    if namespace.lower() in settings.kubectl_blocked_namespaces:
        # A protected namespace is never handed to an investigation, and no identity is created there.
        raise IdentityUnavailable(f"{namespace!r} is a protected namespace")
    return _issue(
        (base_kubeconfig, namespace),
        f"ns-{namespace}",
        settings.SESSION_IDENTITY_SERVICE_ACCOUNT,
        namespace,
        provision=settings.SESSION_IDENTITY_PROVISION,
        context_namespace=namespace,
    )


def _home_namespace() -> str:
    """The namespace the read-only ServiceAccount lives in: configured, else the server pod's own."""
    if settings.READONLY_IDENTITY_NAMESPACE:
        return settings.READONLY_IDENTITY_NAMESPACE
    return _pod_namespace()


def readonly_kubeconfig(base_kubeconfig: str) -> str:
    """Path of a kubeconfig whose only credential is a short-lived token for the read-only identity.

    The `readonly` role may not write and may not read Secrets (ten review passes found string forms that did). The
    ServiceAccount `READONLY_IDENTITY_SERVICE_ACCOUNT` is bound cluster-wide to the chart's `cluster-ro` role, which
    grants reads only and no Secrets, so the API server holds both. Never provisioned by the server: the chart creates
    it, because binding it is a cluster-wide grant an operator should see in the chart. The default namespace is the
    server's own default, so an unscoped read means what it meant before.
    """
    home = _home_namespace()
    if not _valid_namespace(home):
        raise IdentityUnavailable(
            "READONLY_IDENTITY_NAMESPACE is unset and the server is not running in a pod"
        )
    return _issue(
        (base_kubeconfig, READONLY_KEY),
        "readonly",
        settings.READONLY_IDENTITY_SERVICE_ACCOUNT,
        home,
        provision=False,
        context_namespace=None,
    )
