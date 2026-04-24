"""KubeIntellect CLI — manage and run the server on your local machine.

Commands:
  kubeintellect init          Interactive setup wizard; writes ~/.kubeintellect/.env
  kubeintellect serve         Start the API server (default: http://localhost:8000)
  kubeintellect db-init       Initialize the database schema
  kubeintellect status        Show current configuration and connectivity status
  kubeintellect kind-setup    Create a local Kind cluster for testing
  kubeintellect service <action>  Manage the background systemd service
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from collections import namedtuple
from pathlib import Path


_CONFIG_DIR = Path.home() / ".kubeintellect"
_CONFIG_FILE = _CONFIG_DIR / ".env"


# ── ANSI colours (degrade gracefully on non-TTY) ──────────────────────────────

def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


_ok   = lambda t: _c("32", t)   # green
_warn = lambda t: _c("33", t)   # yellow
_err  = lambda t: _c("31", t)   # red
_dim  = lambda t: _c("2",  t)   # dim/grey
_bold = lambda t: _c("1",  t)   # bold


# ── Config validation ─────────────────────────────────────────────────────────

_Issue = namedtuple("_Issue", ["field", "level", "message", "fix"])

# Keys whose values are masked in displayed output
_MASK_KEYS = frozenset({
    "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "POSTGRES_PASSWORD",
    "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS",
    "KUBEINTELLECT_READONLY_KEYS", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    "DATABASE_URL",
})


def _mask(key: str, value: str) -> str:
    if key not in _MASK_KEYS or not value:
        return value
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def _validate_config(cfg: dict) -> list[_Issue]:
    """Check a config dict for problems. Never raises — returns a list of issues."""
    issues: list[_Issue] = []

    # LLM provider ─────────────────────────────────────────────────────────────
    provider = cfg.get("LLM_PROVIDER", "azure").strip().lower()
    if provider not in ("azure", "openai"):
        issues.append(_Issue(
            "LLM_PROVIDER", "error",
            f"Invalid value {provider!r} — must be 'openai' or 'azure'.",
            "Edit ~/.kubeintellect/.env:\n"
            "         LLM_PROVIDER=openai    # or: LLM_PROVIDER=azure",
        ))
    elif provider == "openai":
        if not cfg.get("OPENAI_API_KEY", "").strip():
            issues.append(_Issue(
                "OPENAI_API_KEY", "error",
                "LLM_PROVIDER=openai but OPENAI_API_KEY is not set.",
                "Get your key at: https://platform.openai.com/api-keys\n"
                "         Then add to ~/.kubeintellect/.env:\n"
                "           OPENAI_API_KEY=sk-proj-...",
            ))
    elif provider == "azure":
        if not cfg.get("AZURE_OPENAI_API_KEY", "").strip():
            issues.append(_Issue(
                "AZURE_OPENAI_API_KEY", "error",
                "LLM_PROVIDER=azure but AZURE_OPENAI_API_KEY is not set.",
                "Azure Portal → your OpenAI resource → Keys and Endpoint → KEY 1\n"
                "         Then add to ~/.kubeintellect/.env:\n"
                "           AZURE_OPENAI_API_KEY=<your-key>",
            ))
        ep = cfg.get("AZURE_OPENAI_ENDPOINT", "").strip()
        if not ep:
            issues.append(_Issue(
                "AZURE_OPENAI_ENDPOINT", "error",
                "LLM_PROVIDER=azure but AZURE_OPENAI_ENDPOINT is not set.",
                "Azure Portal → your OpenAI resource → Keys and Endpoint → Endpoint\n"
                "         Example:\n"
                "           AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/",
            ))
        elif not ep.startswith("https://"):
            issues.append(_Issue(
                "AZURE_OPENAI_ENDPOINT", "error",
                f"AZURE_OPENAI_ENDPOINT must start with https://, got: {ep!r}",
                "Correct format:\n"
                "         AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/",
            ))

    # DATABASE_URL format ───────────────────────────────────────────────────────
    db_url = cfg.get("DATABASE_URL", "").strip()
    if db_url and not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        issues.append(_Issue(
            "DATABASE_URL", "error",
            "DATABASE_URL does not look like a valid PostgreSQL DSN.",
            "Must start with postgresql:// or postgres://\n"
            "         Example:\n"
            "           DATABASE_URL=postgresql://user:password@localhost:5432/dbname",
        ))

    # Observability URLs (optional but must be well-formed if set) ─────────────
    for key, example in (
        ("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090"),
        ("LOKI_URL",       "http://loki.monitoring.svc.cluster.local:3100"),
        ("LANGFUSE_HOST",  "http://langfuse-web.monitoring.svc.cluster.local:3000"),
    ):
        url = cfg.get(key, "").strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            issues.append(_Issue(
                key, "warn",
                f"{key} is set but does not look like a valid URL: {url!r}",
                f"Must start with http:// or https://\n"
                f"         Example: {key}={example}",
            ))

    # Kubeconfig file ───────────────────────────────────────────────────────────
    kube_path = cfg.get("KUBECONFIG_PATH", "~/.kube/config").strip()
    if not Path(kube_path).expanduser().exists():
        issues.append(_Issue(
            "KUBECONFIG_PATH", "warn",
            f"Kubeconfig not found at {Path(kube_path).expanduser()}",
            "If you don't have a cluster yet:\n"
            "         kubeintellect kind-setup       # creates a local Kind cluster\n"
            "         kubectl config view --minify   # verify your current context",
        ))

    # Auth disabled ─────────────────────────────────────────────────────────────
    if not cfg.get("KUBEINTELLECT_ADMIN_KEYS", "").strip():
        issues.append(_Issue(
            "KUBEINTELLECT_ADMIN_KEYS", "warn",
            "No admin API key configured — server runs in open-access mode.",
            "To enable authentication add to ~/.kubeintellect/.env:\n"
            "         KUBEINTELLECT_ADMIN_KEYS=ki-admin-<your-key>\n"
            "         Run 'kubeintellect init' to generate a key automatically.",
        ))

    return issues


def _print_issues(issues: list[_Issue]) -> None:
    """Print config issues with coloured severity labels and fix hints."""
    if not issues:
        return
    for issue in issues:
        label = _err("  [error]") if issue.level == "error" else _warn("   [warn]")
        print(f"{label}  {_bold(issue.field)}: {issue.message}")
        for line in issue.fix.splitlines():
            print(f"           {_dim(line)}")
    print()


def _print_config_summary(cfg: dict) -> None:
    """Print a categorised, masked summary of an existing config file."""
    sections = [
        ("LLM", [
            "LLM_PROVIDER",
            "OPENAI_API_KEY", "OPENAI_COORDINATOR_MODEL", "OPENAI_SUBAGENT_MODEL",
            "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
            "AZURE_COORDINATOR_DEPLOYMENT", "AZURE_SUBAGENT_DEPLOYMENT",
        ]),
        ("Database", [
            "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
            "POSTGRES_USER", "POSTGRES_PASSWORD", "USE_SQLITE",
        ]),
        ("Kubernetes", ["KUBECONFIG_PATH"]),
        ("Auth", [
            "KUBEINTELLECT_ADMIN_KEYS",
            "KUBEINTELLECT_OPERATOR_KEYS",
            "KUBEINTELLECT_READONLY_KEYS",
        ]),
        ("Observability", [
            "PROMETHEUS_URL", "LOKI_URL",
            "LANGFUSE_ENABLED", "LANGFUSE_HOST",
        ]),
    ]
    print(_bold(f"\n  Existing configuration found: {_CONFIG_FILE}"))
    for section_name, keys in sections:
        present = [(k, cfg[k]) for k in keys if cfg.get(k)]
        if not present:
            continue
        divider = "─" * max(1, 28 - len(section_name))
        print(f"\n  {_dim('─── ' + section_name + ' ' + divider)}")
        for k, v in present:
            print(f"    {k:<40} {_dim(_mask(k, v))}")
    print()


# ── Kind cluster + sample workloads ──────────────────────────────────────────

_SAMPLE_MANIFEST = """
apiVersion: v1
kind: Namespace
metadata:
  name: demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: demo
spec:
  replicas: 2
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:alpine
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: nginx
  namespace: demo
spec:
  selector:
    app: nginx
  ports:
  - port: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: httpbin
  namespace: demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: httpbin
  template:
    metadata:
      labels:
        app: httpbin
    spec:
      containers:
      - name: httpbin
        image: kennethreitz/httpbin
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: httpbin
  namespace: demo
spec:
  selector:
    app: httpbin
  ports:
  - port: 80
"""


_DEMO_RCA_MANIFEST = """
apiVersion: v1
kind: Namespace
metadata:
  name: demo-rca
---
# Scenario 1: CrashLoopBackOff — container exits with error immediately
apiVersion: apps/v1
kind: Deployment
metadata:
  name: crash-loop
  namespace: demo-rca
  labels:
    scenario: crashloop
spec:
  replicas: 1
  selector:
    matchLabels:
      app: crash-loop
  template:
    metadata:
      labels:
        app: crash-loop
        scenario: crashloop
    spec:
      containers:
      - name: crash-loop
        image: busybox:latest
        command: ["/bin/sh", "-c", "echo 'FATAL: database connection refused'; exit 1"]
---
# Scenario 2: OOMKilled — memory limit too low for the workload
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oom-killer
  namespace: demo-rca
  labels:
    scenario: oomkilled
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oom-killer
  template:
    metadata:
      labels:
        app: oom-killer
        scenario: oomkilled
    spec:
      containers:
      - name: oom-killer
        image: busybox:latest
        command: ["/bin/sh", "-c", "dd if=/dev/zero bs=1M count=200 | tail"]
        resources:
          limits:
            memory: "10Mi"
---
# Scenario 3: ImagePullBackOff — image tag does not exist on Docker Hub
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad-image
  namespace: demo-rca
  labels:
    scenario: imagepull
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bad-image
  template:
    metadata:
      labels:
        app: bad-image
        scenario: imagepull
    spec:
      containers:
      - name: bad-image
        image: nginx:version-does-not-exist-99999
---
# Scenario 4: Pending — requests more CPU/RAM than any node has
apiVersion: apps/v1
kind: Deployment
metadata:
  name: resource-hog
  namespace: demo-rca
  labels:
    scenario: pending
spec:
  replicas: 1
  selector:
    matchLabels:
      app: resource-hog
  template:
    metadata:
      labels:
        app: resource-hog
        scenario: pending
    spec:
      containers:
      - name: resource-hog
        image: nginx:alpine
        resources:
          requests:
            cpu: "100"
            memory: "100Gi"
---
# Scenario 5: No endpoints — service selector does not match any pods
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
  namespace: demo-rca
  labels:
    scenario: noendpoints
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api-server
  template:
    metadata:
      labels:
        app: api-server
        scenario: noendpoints
    spec:
      containers:
      - name: api-server
        image: nginx:alpine
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: api-server
  namespace: demo-rca
spec:
  selector:
    app: api-server-v2   # intentionally wrong — no pods match
  ports:
  - port: 80
"""


def _get_kind_node_ip() -> str:
    try:
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o",
             "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _setup_observability() -> None:
    """Install Prometheus, Grafana, and Loki on the Kind cluster. Best-effort — never raises."""
    _ensure_tool("helm", _install_helm)

    print("\n  Setting up observability stack (Prometheus + Grafana + Loki)...")

    for name, url in [
        ("prometheus-community", "https://prometheus-community.github.io/helm-charts"),
        ("grafana",              "https://grafana.github.io/helm-charts"),
    ]:
        subprocess.run(["helm", "repo", "add", name, url],
                       capture_output=True, check=False)
    subprocess.run(["helm", "repo", "update"], capture_output=True, check=False)

    subprocess.run(["kubectl", "create", "namespace", "monitoring"],
                   capture_output=True, check=False)

    # Prometheus + Grafana — exposed via NodePort so the host can reach them
    print("  Installing Prometheus + Grafana (2-3 min) ...")
    prom_ok = subprocess.run([
        "helm", "upgrade", "--install", "kube-prometheus-stack",
        "prometheus-community/kube-prometheus-stack",
        "--namespace", "monitoring",
        "--set", "alertmanager.enabled=false",
        "--set", "prometheus.service.type=NodePort",
        "--set", "prometheus.service.nodePort=30090",
        "--set", "grafana.service.type=NodePort",
        "--set", "grafana.service.nodePort=30080",
        "--set", "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false",
        "--wait", "--timeout", "5m",
    ], check=False).returncode == 0

    if prom_ok:
        print(f"  {_ok('✓')}  Prometheus + Grafana installed.")
    else:
        print(_warn("  ⚠  Prometheus/Grafana install failed — KubeIntellect still works without it."))
        print(_dim("     To retry: helm upgrade --install kube-prometheus-stack "
                   "prometheus-community/kube-prometheus-stack --namespace monitoring"))
        print(_dim("     Docs: https://github.com/prometheus-community/helm-charts"))

    # Loki (single binary, no auth) + Promtail for log collection
    print("  Installing Loki (log aggregation) ...")
    loki_ok = subprocess.run([
        "helm", "upgrade", "--install", "loki",
        "grafana/loki",
        "--namespace", "monitoring",
        "--set", "loki.auth_enabled=false",
        "--set", "loki.commonConfig.replication_factor=1",
        "--set", "loki.storage.type=filesystem",
        "--set", "loki.useTestSchema=true",
        "--set", "singleBinary.replicas=1",
        "--set", "read.replicas=0",
        "--set", "write.replicas=0",
        "--set", "backend.replicas=0",
        "--wait", "--timeout", "5m",
    ], check=False).returncode == 0

    if loki_ok:
        # Patch loki service to NodePort so the host can reach it
        subprocess.run([
            "kubectl", "patch", "svc", "loki", "-n", "monitoring",
            "-p", ('{"spec":{"type":"NodePort","ports":[{"port":3100,"targetPort":3100,'
                   '"nodePort":30100,"protocol":"TCP","name":"http-metrics"}]}}'),
        ], capture_output=True, check=False)
        print(f"  {_ok('✓')}  Loki installed.")
    else:
        print(_warn("  ⚠  Loki install failed — KubeIntellect still works without it."))
        print(_dim("     To retry: helm upgrade --install loki grafana/loki --namespace monitoring"))
        print(_dim("     Docs: https://grafana.com/docs/loki/latest/setup/install/helm/"))

    print("  Installing Grafana Alloy (log shipper) ...")
    # Add Alloy repo if not already present
    subprocess.run(["helm", "repo", "add", "grafana", "https://grafana.github.io/helm-charts"],
                   capture_output=True, check=False)
    subprocess.run(["helm", "repo", "update"], capture_output=True, check=False)
    alloy_ok = subprocess.run([
        "helm", "upgrade", "--install", "alloy",
        "grafana/alloy",
        "--namespace", "monitoring",
        "--set", "alloy.configMap.content=logging { level = \"info\" format = \"logfmt\" }\nloki.write \"default\" { endpoint { url = \"http://loki:3100/loki/api/v1/push\" } }",
        "--wait", "--timeout", "3m",
    ], check=False).returncode == 0

    if alloy_ok:
        print(f"  {_ok('✓')}  Grafana Alloy installed (shipping logs → Loki).")
    else:
        print(_warn("  ⚠  Alloy install failed — logs won't be collected, but Loki queries still work."))
        print(_dim("     To retry: helm upgrade --install alloy grafana/alloy --namespace monitoring"))

    node_ip = _get_kind_node_ip()
    if not node_ip:
        print(_warn("  ⚠  Could not detect Kind node IP — set PROMETHEUS_URL / LOKI_URL manually."))
        return

    prom_url    = f"http://{node_ip}:30090"
    loki_url    = f"http://{node_ip}:30100"
    grafana_url = f"http://{node_ip}:30080"

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    additions = ""
    if prom_ok and "PROMETHEUS_URL=" not in existing:
        additions += f"PROMETHEUS_URL={prom_url}\n"
    if loki_ok and "LOKI_URL=" not in existing:
        additions += f"LOKI_URL={loki_url}\n"
    if prom_ok and "GRAFANA_URL=" not in existing:
        additions += f"GRAFANA_URL={grafana_url}\n"
    if additions:
        with _CONFIG_FILE.open("a") as f:
            f.write(additions)

    if prom_ok:
        print(f"  {_ok('✓')}  Prometheus: {prom_url}")
        print(f"  {_ok('✓')}  Grafana:    {grafana_url}  {_dim('(user: admin / pass: prom-operator)')}")
    if loki_ok:
        print(f"  {_ok('✓')}  Loki:       {loki_url}")


def _setup_demo_rca() -> None:
    """Deploy intentionally broken workloads for RCA practice. Best-effort — never raises."""
    print("\n  Creating RCA demo scenarios in namespace 'demo-rca' ...")
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(_DEMO_RCA_MANIFEST)
            manifest_path = f.name
        subprocess.run(["kubectl", "apply", "-f", manifest_path],
                       check=False, capture_output=True)
        Path(manifest_path).unlink(missing_ok=True)

        print(f"  {_ok('✓')}  5 RCA scenarios deployed to namespace 'demo-rca':\n")
        rows = [
            ("crash-loop",   "CrashLoopBackOff", "container exits with non-zero code"),
            ("oom-killer",   "OOMKilled",         "memory limit too low"),
            ("bad-image",    "ImagePullBackOff",  "image tag does not exist"),
            ("resource-hog", "Pending",           "requests 100 CPU / 100 Gi"),
            ("api-server",   "No endpoints",      "service selector does not match pods"),
        ]
        for name, state, reason in rows:
            print(f"    {name:<16} {_warn(state):<22} {_dim(reason)}")
        print()
        print(f"  {_dim('Try asking KubeIntellect:')}")
        print(f"  {_dim('  → \"what pods are broken in the demo-rca namespace?\"')}")
        print(f"  {_dim('  → \"why is crash-loop crashing and how do I fix it?\"')}")
        print(f"  {_dim('  → \"why is resource-hog pending?\"')}")
        print(f"  {_dim('  → \"why does the api-server service have no endpoints?\"')}")
    except Exception as exc:
        print(_warn(f"  ⚠  Could not create RCA demo scenarios: {exc}"))
        print(_dim("     Run 'kubeintellect kind-setup' later to retry."))


def _setup_kind_with_samples() -> None:
    _ensure_tool("kind", _install_kind)
    _ensure_tool("kubectl", _install_kubectl)

    print("\n  Creating 1-node Kind cluster 'kubeintellect' ...")
    result = subprocess.run(
        ["kind", "create", "cluster", "--name", "kubeintellect"],
        check=False,
    )
    if result.returncode != 0:
        print(_err("  Failed to create Kind cluster — run 'kubeintellect kind-setup' manually."))
        return
    print(f"  {_ok('✓')}  Cluster created.")

    # Update kubeconfig path in our config
    kube_path = str(Path.home() / ".kube" / "config")
    existing_text = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    lines = [l for l in existing_text.splitlines(keepends=True)
             if not l.startswith("KUBECONFIG_PATH=")]
    lines.append(f"KUBECONFIG_PATH={kube_path}\n")
    _CONFIG_FILE.write_text("".join(lines))

    # Deploy sample workloads
    print("  Deploying sample workloads (nginx × 2, httpbin × 1) in namespace 'demo' ...")
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(_SAMPLE_MANIFEST)
        manifest_path = f.name
    subprocess.run(["kubectl", "apply", "-f", manifest_path], check=False, capture_output=True)
    Path(manifest_path).unlink(missing_ok=True)
    print(f"  {_ok('✓')}  Sample pods deployed. Try asking: 'list pods in demo namespace'")


# ── systemd user service ──────────────────────────────────────────────────────

_SERVICE_NAME = "kubeintellect"
_SERVICE_DIR  = Path.home() / ".config" / "systemd" / "user"
_SERVICE_FILE = _SERVICE_DIR / f"{_SERVICE_NAME}.service"


def _systemd_available() -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
    ).returncode in (0, 1)  # 0=running, 1=degraded — both mean systemd is present


def _service_installed() -> bool:
    return _SERVICE_FILE.exists()


def _install_service() -> None:
    kubeintellect_bin = subprocess.run(
        ["which", "kubeintellect"], capture_output=True, text=True,
    ).stdout.strip() or str(Path(sys.executable).parent / "kubeintellect")

    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_FILE.write_text(f"""\
[Unit]
Description=KubeIntellect AI Server
After=network.target

[Service]
ExecStart={kubeintellect_bin} serve
Restart=on-failure
RestartSec=5
EnvironmentFile={_CONFIG_FILE}

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", _SERVICE_NAME],
                   check=False, capture_output=True)


def _uninstall_service() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", _SERVICE_NAME],
                   check=False, capture_output=True)
    _SERVICE_FILE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)


def cmd_service(args: argparse.Namespace) -> None:
    """Manage the kubeintellect background service."""
    action = args.action
    if action == "install":
        _install_service()
        print(_ok("✓  Service installed — server will start automatically on login."))
    elif action == "uninstall":
        _uninstall_service()
        print(_ok("✓  Service removed."))
    elif action == "start":
        subprocess.run(["systemctl", "--user", "start", _SERVICE_NAME])
    elif action == "stop":
        subprocess.run(["systemctl", "--user", "stop", _SERVICE_NAME])
    elif action == "status":
        subprocess.run(["systemctl", "--user", "status", _SERVICE_NAME])
    elif action == "logs":
        subprocess.run(["journalctl", "--user", "-u", _SERVICE_NAME, "-f", "--no-pager"])


# ── start server in background + hand off to kq ──────────────────────────────

def _open_kq() -> None:
    import socket, time
    for i in range(45):
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=1):
                break
        except OSError:
            if i == 0:
                print("  Waiting for server", end="", flush=True)
            print(".", end="", flush=True)
            time.sleep(1)
    else:
        print(f"\n  {_warn('Server did not start in time.')}")
        print("  Check logs: kubeintellect service logs")
        return
    print(f"\n  {_ok('✓')}  Server is ready at http://localhost:8000\n")
    kq_bin = Path(sys.executable).parent / "kq"
    if kq_bin.exists():
        os.execv(str(kq_bin), [str(kq_bin)])
    else:
        print(f"  {_warn('kq not found.')} Run: pip install kube-q")


def _start_server_and_open_kq() -> None:
    _ensure_database()
    log_file = _CONFIG_DIR / "server.log"
    print(f"\n  Starting server in background (logs → {log_file}) ...")
    with log_file.open("a") as lf:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"],
            stdout=lf, stderr=lf,
        )
    _open_kq()


# ── init — where to find each value ──────────────────────────────────────────

_HELP = {
    "OPENAI_API_KEY":               "platform.openai.com → API keys",
    "AZURE_OPENAI_API_KEY":         "Azure Portal → your OpenAI resource → Keys and Endpoint → KEY 1",
    "AZURE_OPENAI_ENDPOINT":        "Azure Portal → your OpenAI resource → Keys and Endpoint → Endpoint",
    "AZURE_COORDINATOR_DEPLOYMENT": "Azure AI Foundry → Deployments — your gpt-4o deployment name",
    "AZURE_SUBAGENT_DEPLOYMENT":    "Azure AI Foundry → Deployments — your gpt-4o-mini deployment name",
    "OPENAI_COORDINATOR_MODEL":     "platform.openai.com/docs/models (e.g. gpt-4o, gpt-4.1)",
    "OPENAI_SUBAGENT_MODEL":        "platform.openai.com/docs/models (e.g. gpt-4o-mini, gpt-4.1-mini)",
    "DATABASE_URL":                 "format: postgresql://user:password@host:5432/dbname",
    "POSTGRES_PASSWORD":            "any secure password — used only for the local postgres container",
    "PROMETHEUS_URL":               "e.g. http://kube-prometheus-stack-prometheus.monitoring:9090",
    "LOKI_URL":                     "e.g. http://loki.monitoring.svc.cluster.local:3100",
    "LANGFUSE_HOST":                "your Langfuse URL — langfuse.com or self-hosted instance",
    "KUBECONFIG_PATH":              "usually ~/.kube/config — check: kubectl config view --minify",
}


def cmd_init(_args: argparse.Namespace) -> None:
    """Interactively create or update ~/.kubeintellect/.env."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str] = {}

    # Load in priority order: env vars → local .env → existing config ──────────
    # Lower-priority sources fill in only what's not already set
    _WATCHED_KEYS = (
        "LLM_PROVIDER",
        "OPENAI_API_KEY", "OPENAI_COORDINATOR_MODEL", "OPENAI_SUBAGENT_MODEL",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
        "AZURE_COORDINATOR_DEPLOYMENT", "AZURE_SUBAGENT_DEPLOYMENT",
        "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PASSWORD",
        "KUBECONFIG_PATH",
        "PROMETHEUS_URL", "LOKI_URL", "LANGFUSE_HOST", "LANGFUSE_ENABLED",
        "KUBEINTELLECT_ADMIN_KEYS", "KUBEINTELLECT_OPERATOR_KEYS", "KUBEINTELLECT_READONLY_KEYS",
    )
    # 1. existing kubeintellect config (highest priority)
    if _CONFIG_FILE.exists():
        _load_dotenv_dict(_CONFIG_FILE, existing)
    # 2. local .env in cwd
    _local_env = Path(".env")
    if _local_env.exists():
        _local: dict[str, str] = {}
        _load_dotenv_dict(_local_env, _local)
        for k in _WATCHED_KEYS:
            if k not in existing and _local.get(k):
                existing[k] = _local[k]
    # 3. shell environment variables
    for k in _WATCHED_KEYS:
        if k not in existing and os.environ.get(k):
            existing[k] = os.environ[k]

    if _CONFIG_FILE.exists():
        _print_config_summary(existing)
        issues = _validate_config(existing)
        if issues:
            print(_warn("  Existing configuration has issues — the wizard will help you fix them.\n"))
            _print_issues(issues)
        else:
            print(_ok("  ✓  Configuration looks healthy. Press Enter to keep current values.\n"))
    else:
        print(_bold("\n  KubeIntellect — first-time setup\n"))
        if any(existing.get(k) for k in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")):
            print(_dim("  Found API keys in your environment — press Enter to use them.\n"))
        else:
            print("  This wizard creates ~/.kubeintellect/.env with your settings.")
            print("  Press Ctrl+C at any time to cancel without saving.\n")

    def _ask(prompt: str, key: str, default: str = "", help_text: str = "") -> str:
        current = existing.get(key, default)
        display = f" [{_dim(_mask(key, current))}]" if current else ""
        hint = f"\n    {_dim('→  ' + help_text)}" if help_text else ""
        value = input(f"  {prompt}{display}{hint}\n  > ").strip()
        return value or current

    # kubectl check — install automatically if missing ────────────────────────
    if subprocess.run(["which", "kubectl"], capture_output=True).returncode != 0:
        print("  kubectl not found — installing automatically...")
        _ensure_tool("kubectl", _install_kubectl)

    # LLM provider ─────────────────────────────────────────────────────────────
    print(f"\n  {_bold('LLM Provider')}")
    print("    1  OpenAI        (api.openai.com)")
    print("    2  Azure OpenAI  (your own Azure deployment)")
    current_provider = existing.get("LLM_PROVIDER", "openai")
    current_choice = "2" if current_provider == "azure" else "1"
    choice = input(f"\n  Choose [1/2] [{current_choice}]: ").strip() or current_choice
    provider = "openai" if choice != "2" else "azure"
    lines: list[str] = [f"LLM_PROVIDER={provider}\n"]

    if provider == "openai":
        print()
        api_key = _ask("OPENAI_API_KEY:", "OPENAI_API_KEY", help_text=_HELP["OPENAI_API_KEY"])
        lines += [
            f"OPENAI_API_KEY={api_key}\n",
            f"OPENAI_COORDINATOR_MODEL={existing.get('OPENAI_COORDINATOR_MODEL', 'gpt-4o')}\n",
            f"OPENAI_SUBAGENT_MODEL={existing.get('OPENAI_SUBAGENT_MODEL', 'gpt-4o-mini')}\n",
        ]
    else:
        print()
        api_key = _ask("AZURE_OPENAI_API_KEY:", "AZURE_OPENAI_API_KEY",
                       help_text=_HELP["AZURE_OPENAI_API_KEY"])
        endpoint = _ask("AZURE_OPENAI_ENDPOINT (https://...):", "AZURE_OPENAI_ENDPOINT",
                        help_text=_HELP["AZURE_OPENAI_ENDPOINT"])
        lines += [
            f"AZURE_OPENAI_API_KEY={api_key}\n",
            f"AZURE_OPENAI_ENDPOINT={endpoint}\n",
            f"AZURE_COORDINATOR_DEPLOYMENT={existing.get('AZURE_COORDINATOR_DEPLOYMENT', 'gpt-4o')}\n",
            f"AZURE_SUBAGENT_DEPLOYMENT={existing.get('AZURE_SUBAGENT_DEPLOYMENT', 'gpt-4o-mini')}\n",
        ]

    # Kubernetes cluster — detect before deciding access level ───────────────
    kube_path = Path("~/.kube/config").expanduser()
    kind_created = False
    if not kube_path.exists():
        print(_warn("\n  No Kubernetes cluster found (~/.kube/config missing)."))
        ans = input("  Create a local Kind cluster with sample workloads? [Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            _setup_kind_with_samples()
            kind_created = True

            ans = input("  Install observability stack (Prometheus, Grafana, Loki)? [Y/n]: ").strip().lower()
            if ans not in ("n", "no"):
                _setup_observability()

            ans = input("  Create RCA demo scenarios (broken pods to practice root-cause analysis)? [Y/n]: ").strip().lower()
            if ans not in ("n", "no"):
                _setup_demo_rca()

    # Access level — admin for Kind/test, ask for existing clusters ───────────
    if kind_created or not kube_path.exists():
        # Local test cluster — full access is safe
        access_level = "admin"
    elif existing.get("KUBEINTELLECT_ADMIN_KEYS"):
        access_level = "admin"
    elif existing.get("KUBEINTELLECT_OPERATOR_KEYS"):
        access_level = "operator"
    elif existing.get("KUBEINTELLECT_READONLY_KEYS"):
        access_level = "readonly"
    else:
        print(f"\n  {_bold('Access level for this cluster:')}")
        print("    1  admin     — full access, all operations (dev/test clusters)")
        print("    2  operator  — create, scale, apply; no deletes or drains")
        print("    3  readonly  — queries only, no changes  " + _dim("← recommended for production"))
        lvl = input("\n  Choose [1/2/3] [3]: ").strip() or "3"
        access_level = {"1": "admin", "2": "operator"}.get(lvl, "readonly")

    existing_key = (
        existing.get("KUBEINTELLECT_ADMIN_KEYS") or
        existing.get("KUBEINTELLECT_OPERATOR_KEYS") or
        existing.get("KUBEINTELLECT_READONLY_KEYS") or ""
    )
    prefix = {"admin": "ki-admin", "operator": "ki-op", "readonly": "ki-ro"}[access_level]
    user_key = existing_key or f"{prefix}-{secrets.token_hex(10)}"
    env_var  = {"admin": "KUBEINTELLECT_ADMIN_KEYS",
                "operator": "KUBEINTELLECT_OPERATOR_KEYS",
                "readonly": "KUBEINTELLECT_READONLY_KEYS"}[access_level]
    lines.append(f"{env_var}={user_key}\n")

    # Kubeconfig — use default silently, only keep existing override ───────────
    kube = existing.get("KUBECONFIG_PATH", "~/.kube/config")
    lines.append(f"KUBECONFIG_PATH={kube}\n")

    # Collect all values into a single dict, then write a fully-commented env file
    final: dict[str, str] = {}
    # Parse what the wizard built so far
    for raw in lines:
        raw = raw.strip()
        if raw and "=" in raw and not raw.startswith("#"):
            k, _, v = raw.partition("=")
            final[k.strip()] = v.strip()
    # Carry over database + observability from existing config
    for k in ("DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
               "POSTGRES_USER", "POSTGRES_PASSWORD", "USE_SQLITE",
               "PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL",
               "LANGFUSE_ENABLED", "LANGFUSE_HOST",
               "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        if existing.get(k) and k not in final:
            final[k] = existing[k]

    def _v(key: str, default: str = "") -> str:
        return final.get(key, default)

    def _line(key: str, default: str = "", comment: str = "") -> str:
        """Return a KEY=value line (active) or # KEY=hint line (commented out).
        Comments go on a separate line above — never inline — so they are not
        parsed as part of the value."""
        val = _v(key, default)
        c = f"# {comment}\n" if comment else ""
        if val:
            return f"{c}{key}={val}\n"
        hint = default or comment or ""
        return f"# {key}={hint}\n"

    env_content = f"""\
# KubeIntellect configuration
# ──────────────────────────────────────────────────────────────────────────────
# Docs & support:  https://kubeintellect.com
# GitHub:          https://github.com/mskazemi/kubeintellect
# Contact:         mohsen.seyedkazemi@gmail.com
# ──────────────────────────────────────────────────────────────────────────────
# Edit values directly or use:  kubeintellect set KEY=VALUE
# Re-run wizard:                kubeintellect init
# Check status:                 kubeintellect status

# ── LLM Provider ──────────────────────────────────────────────────────────────
# Choose your AI provider: openai or azure
{_line("LLM_PROVIDER", "openai")}
# OpenAI — get key at https://platform.openai.com/api-keys
{_line("OPENAI_API_KEY", "sk-proj-...")}
# Main reasoning model (e.g. gpt-4o, gpt-4.1)
{_line("OPENAI_COORDINATOR_MODEL", "gpt-4o")}
# Faster/cheaper model for subagents (e.g. gpt-4o-mini, gpt-4.1-mini)
{_line("OPENAI_SUBAGENT_MODEL", "gpt-4o-mini")}
# Azure OpenAI — get key at Azure Portal → your resource → Keys and Endpoint
{_line("AZURE_OPENAI_API_KEY", "your-key")}
{_line("AZURE_OPENAI_ENDPOINT", "https://your-resource.openai.azure.com/")}
# Deployment names from Azure AI Foundry
{_line("AZURE_COORDINATOR_DEPLOYMENT", "gpt-4o")}
{_line("AZURE_SUBAGENT_DEPLOYMENT", "gpt-4o-mini")}

# ── Database ──────────────────────────────────────────────────────────────────
# USE_SQLITE=true — default for local/testing, no extra setup needed
# Set to false (or remove) to use PostgreSQL instead
{_line("USE_SQLITE", "true")}
# PostgreSQL — only needed when USE_SQLITE is not set
{_line("DATABASE_URL", "postgresql://user:pass@localhost:5432/kubeintellect")}
{_line("POSTGRES_HOST", "localhost")}
{_line("POSTGRES_PORT", "5432")}
{_line("POSTGRES_DB", "kubeintellect")}
{_line("POSTGRES_USER", "kubeintellect")}
{_line("POSTGRES_PASSWORD", "")}

# ── Kubernetes ────────────────────────────────────────────────────────────────
# Path to your kubeconfig file (default: ~/.kube/config)
{_line("KUBECONFIG_PATH", "~/.kube/config")}

# ── Authentication ────────────────────────────────────────────────────────────
# Comma-separated API keys per role. Leave unset for open access (dev only).
# Generate a key:  openssl rand -hex 20
# Full access — create, delete, scale, drain
{_line("KUBEINTELLECT_ADMIN_KEYS", "ki-admin-...")}
# Write access — create, scale, apply; no deletes or drains
{_line("KUBEINTELLECT_OPERATOR_KEYS", "ki-op-...")}
# Read-only — queries and describe only, no changes
{_line("KUBEINTELLECT_READONLY_KEYS", "ki-ro-...")}

# ── Observability (optional) ──────────────────────────────────────────────────
# Set automatically by 'kubeintellect init' when observability stack is installed
{_line("PROMETHEUS_URL", "http://172.18.0.2:30090")}
{_line("LOKI_URL", "http://172.18.0.2:30100")}
{_line("GRAFANA_URL", "http://172.18.0.2:30080")}

# ── Langfuse LLM tracing (optional) ──────────────────────────────────────────
# Sign up at https://cloud.langfuse.com or self-host
{_line("LANGFUSE_ENABLED", "false")}
{_line("LANGFUSE_HOST", "https://cloud.langfuse.com")}
{_line("LANGFUSE_PUBLIC_KEY", "pk-lf-...")}
{_line("LANGFUSE_SECRET_KEY", "sk-lf-...")}
"""
    _CONFIG_FILE.write_text(env_content)

    # Configure kube-q with the user key + local URL ───────────────────────────
    _kube_q_dir = Path.home() / ".kube-q"
    _kube_q_env = _kube_q_dir / ".env"
    _kube_q_dir.mkdir(parents=True, exist_ok=True)
    _kube_q_existing = _kube_q_env.read_text() if _kube_q_env.exists() else ""
    _kube_q_lines = list(_kube_q_existing.splitlines(keepends=True))
    _kube_q_lines = [l for l in _kube_q_lines if not l.startswith(("KUBE_Q_URL=", "KUBE_Q_API_KEY="))]
    _kube_q_lines += [f"KUBE_Q_URL=http://localhost:8000\n", f"KUBE_Q_API_KEY={user_key}\n"]
    _kube_q_env.write_text("".join(_kube_q_lines))

    _level_label = {"admin": _err("admin  (full access)"),
                    "operator": _warn("operator  (no deletes/drains)"),
                    "readonly": _ok("readonly  (queries only)")}[access_level]
    print(f"\n  {_ok('✓')}  kubeintellect  {_CONFIG_FILE}")
    print(f"  {_ok('✓')}  kube-q         {_kube_q_env}")
    print(f"  {_ok('✓')}  Access level:  {_level_label}")
    print(f"  {_ok('✓')}  API key:       {_bold(user_key)}")

    # Post-write validation ────────────────────────────────────────────────────
    written: dict[str, str] = {}
    _load_dotenv_dict(_CONFIG_FILE, written)
    issues = _validate_config(written)
    if issues:
        print(_warn("\n  Issues detected in the saved configuration:\n"))
        _print_issues(issues)
        print(_dim(f"  Edit {_CONFIG_FILE} or re-run 'kubeintellect init' to fix them.\n"))
    else:
        print(f"  {_ok('✓')}  All required settings are present.\n")

    print(f"""
  {_bold('── Setup complete ───────────────────────────────────────────────────────')}
  API key:     {_bold(user_key)}
  Config file: {_CONFIG_FILE}
  {_bold('─────────────────────────────────────────────────────────────────────────')}
""")

    # Resolve database mode now so the systemd service starts without prompting ──
    _ensure_database()

    # Offer systemd service so kq works on every new terminal automatically ────
    if _systemd_available():
        if _service_installed():
            print(f"  {_ok('✓')}  Background service already installed — server starts automatically on login.")
        else:
            ans = input("  Install as background service? (server starts automatically on login) [Y/n]: ").strip().lower()
            if ans not in ("n", "no"):
                _install_service()
                print(f"  {_ok('✓')}  Service installed. After this, just open a terminal and run: kq\n")
                _open_kq()
                return

    # Fallback: start server in background for this session only ───────────────
    ans = input("  Start server and open kq now? [Y/n]: ").strip().lower()
    if ans not in ("n", "no"):
        _start_server_and_open_kq()


def _print_compose_help() -> None:
    print(f"""
  {_bold('── Docker Compose quick start ───────────────────────────────────────────')}

  Core only (KubeIntellect + postgres):
    docker compose up -d

  With Prometheus + Grafana + Loki:
    docker compose --profile monitoring up -d
    # Then set in ~/.kubeintellect/.env:
    #   PROMETHEUS_URL=http://localhost:9090
    #   LOKI_URL=http://localhost:3100

  With Langfuse LLM tracing:
    docker compose --profile tracing up -d
    # Visit http://localhost:3001 → create account → copy API keys
    # Then set in ~/.kubeintellect/.env:
    #   LANGFUSE_ENABLED=true
    #   LANGFUSE_HOST=http://localhost:3001
    #   LANGFUSE_PUBLIC_KEY=pk-lf-...
    #   LANGFUSE_SECRET_KEY=sk-lf-...

  Everything:
    docker compose --profile monitoring --profile tracing up -d

  {_bold('─────────────────────────────────────────────────────────────────────────')}
""")


def _print_manual_help(admin_key: str) -> None:
    kubectl_ok = subprocess.run(["which", "kubectl"], capture_output=True).returncode == 0
    print(f"\n  {_bold('── Next steps ───────────────────────────────────────────────────────────')}\n")
    if not kubectl_ok:
        print("  0. Install kubectl:")
        print("       # Linux:")
        print('       curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"')
        print("       chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl")
        print("       # macOS: brew install kubectl\n")
    print("  1. Initialize the database schema:")
    print("       kubeintellect db-init\n")
    print("  2. Start the server:")
    print("       kubeintellect serve\n")
    print("  3. Connect with kube-q:")
    print("       pipx install kube-q   # or: pip install kube-q")
    print(f"       KUBE_Q_API_KEY={admin_key} kq\n")
    print(f"  {_bold('─────────────────────────────────────────────────────────────────────────')}\n")


# ── database auto-detection ───────────────────────────────────────────────────

def _postgres_reachable() -> bool:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    try:
        import socket
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _docker_available() -> bool:
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=5
    ).returncode == 0


def _start_postgres_container() -> None:
    pg_pass = os.environ.get("POSTGRES_PASSWORD", secrets.token_hex(16))
    os.environ["POSTGRES_PASSWORD"] = pg_pass
    subprocess.run([
        "docker", "run", "-d", "--name", "kubeintellect-postgres",
        "--restart", "unless-stopped",
        "-e", f"POSTGRES_USER={os.environ.get('POSTGRES_USER', 'kubeintellect')}",
        "-e", f"POSTGRES_PASSWORD={pg_pass}",
        "-e", f"POSTGRES_DB={os.environ.get('POSTGRES_DB', 'kubeintellect')}",
        "-p", f"{os.environ.get('POSTGRES_PORT', '5432')}:5432",
        "postgres:16",
    ], check=True)
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    if "POSTGRES_PASSWORD=" not in existing:
        with _CONFIG_FILE.open("a") as f:
            f.write(f"POSTGRES_PASSWORD={pg_pass}\n")
    print("  Waiting for postgres to be ready...")
    import time
    for _ in range(15):
        if _postgres_reachable():
            break
        time.sleep(1)


def _ensure_database() -> None:
    """Detect database mode and set USE_SQLITE env var if postgres is unavailable."""
    if os.environ.get("USE_SQLITE", "").lower() == "true":
        return

    if os.environ.get("DATABASE_URL"):
        return

    if _postgres_reachable():
        return

    interactive = sys.stdin.isatty()

    if _docker_available():
        result = subprocess.run(
            ["docker", "start", "kubeintellect-postgres"],
            capture_output=True,
        )
        if result.returncode == 0:
            print(_ok("  ✓  Started existing postgres container."))
            return

        if interactive:
            print(_warn("\n  Postgres is not running."))
            print("  Options:")
            print("    1  Use SQLite  (default — no setup needed, good for testing)")
            print("    2  Start a postgres container via Docker")
            choice = input("  Choose [1/2] (default: 1): ").strip() or "1"
            if choice == "2":
                print("  Starting postgres container...")
                try:
                    _start_postgres_container()
                    print(_ok("  ✓  Postgres started."))
                    print(_dim("  Run 'kubeintellect db-init' if this is a fresh install.\n"))
                    return
                except Exception as exc:
                    print(_err(f"  Could not start postgres container: {exc}"), file=sys.stderr)
                    print(_dim("  Falling back to SQLite.\n"))
    else:
        if interactive:
            print(_warn("  Postgres not reachable and Docker not available — using SQLite."))

    os.environ["USE_SQLITE"] = "true"
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    if "USE_SQLITE=" not in existing:
        with _CONFIG_FILE.open("a") as f:
            f.write("USE_SQLITE=true\n")
    if interactive:
        print(_ok("  ✓  SQLite mode enabled.") + f" Data stored at ~/.kubeintellect/kubeintellect.db\n")


# ── serve ─────────────────────────────────────────────────────────────────────

def cmd_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI server via uvicorn."""
    if not _CONFIG_FILE.exists():
        print(_warn(f"\n  No config file found at {_CONFIG_FILE}"))
        print(f"  Run {_bold('kubeintellect init')} to create one.")
        print(_dim("  Continuing with environment variables and defaults...\n"))
    else:
        _load_dotenv(_CONFIG_FILE)
        cfg: dict[str, str] = {}
        _load_dotenv_dict(_CONFIG_FILE, cfg)
        issues = _validate_config(cfg)
        if issues:
            errors = [i for i in issues if i.level == "error"]
            print(f"\n  {_bold('Configuration issues')} (server will still attempt to start):\n")
            _print_issues(issues)
            if errors:
                print(_warn("  The server may not function correctly until these are resolved."))
                print(_dim(f"  Edit {_CONFIG_FILE} or run: kubeintellect init\n"))

    _ensure_database()

    try:
        import uvicorn  # type: ignore[import-untyped]
    except ImportError:
        print(_err("  uvicorn not found") + " — install with: pip install kubeintellect", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Starting KubeIntellect on http://{args.host}:{args.port}")
    print(_dim("  Press Ctrl+C to stop.\n"))
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


# ── db-init ───────────────────────────────────────────────────────────────────

def _db_error_hint(exc: Exception) -> str:
    """Return an actionable fix hint for a common database error."""
    msg = str(exc).lower()
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "kubeintellect")
    db   = os.environ.get("POSTGRES_DB", "kubeintellect")

    if "password authentication failed" in msg:
        return (
            f"The password in your config does not match the postgres user '{user}'.\n"
            f"  Check POSTGRES_PASSWORD in {_CONFIG_FILE}\n"
            "  Then re-run: kubeintellect db-init"
        )
    if "connection refused" in msg or "connection failed" in msg or "nodename nor servname" in msg:
        return (
            f"Cannot connect to postgres at {host}:{port} — is it running?\n"
            "  Start with Docker:\n"
            f"    docker run -d --name ki-pg \\\n"
            f"      -e POSTGRES_USER={user} \\\n"
            f"      -e POSTGRES_PASSWORD=<your-password> \\\n"
            f"      -e POSTGRES_DB={db} \\\n"
            f"      -p {port}:5432 postgres:16\n"
            "  Or let the server auto-detect: kubeintellect serve"
        )
    if "does not exist" in msg and "database" in msg:
        return (
            f"Database '{db}' does not exist.\n"
            "  Create it first:\n"
            f"    createdb -h {host} -U {user} {db}\n"
            "  Then re-run: kubeintellect db-init"
        )
    if "role" in msg and "does not exist" in msg:
        return (
            f"Postgres user/role '{user}' does not exist.\n"
            f"  Check POSTGRES_USER in {_CONFIG_FILE} or create the role:\n"
            f"    createuser -h {host} -s {user}"
        )
    if "ssl" in msg:
        return (
            "SSL/TLS connection error.\n"
            "  If your postgres requires SSL, add ?sslmode=require to DATABASE_URL.\n"
            f"  Example: DATABASE_URL=postgresql://{user}:password@{host}:{port}/{db}?sslmode=require"
        )
    return (
        f"Check your database configuration in {_CONFIG_FILE}\n"
        "  Run 'kubeintellect status' to verify connectivity."
    )


def cmd_db_init(_args: argparse.Namespace) -> None:
    """Run the database schema against the configured PostgreSQL instance."""
    if _CONFIG_FILE.exists():
        _load_dotenv(_CONFIG_FILE)
    else:
        print(_warn(f"\n  No config file at {_CONFIG_FILE} — using environment variables.\n"))

    if os.environ.get("USE_SQLITE", "").lower() == "true":
        print("  SQLite mode — schema is created automatically on first start.")
        print("  Nothing to do. Run: kubeintellect serve")
        return

    try:
        import importlib.resources as pkg_resources
        sql_text = pkg_resources.files("app.db").joinpath("schema.sql").read_text()
    except Exception:
        schema_path = Path(__file__).parent / "db" / "schema.sql"
        if not schema_path.exists():
            print(_err("  schema.sql not found."), file=sys.stderr)
            print("  Reinstall KubeIntellect: pip install --upgrade kubeintellect", file=sys.stderr)
            sys.exit(1)
        sql_text = schema_path.read_text()

    dsn = _build_dsn()
    print(f"  Connecting to: {_redact_dsn(dsn)}")

    try:
        import psycopg  # type: ignore[import-untyped]
    except ImportError:
        print(_err("  psycopg not found") + " — install with: pip install 'kubeintellect'", file=sys.stderr)
        sys.exit(1)

    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql_text)
        print(_ok("  ✓  Database schema initialized successfully."))
        print(_dim("  Next: kubeintellect serve"))
    except Exception as exc:
        print(_err(f"\n  Database error: {exc}\n"), file=sys.stderr)
        print(f"  {_bold('How to fix:')}\n  {_db_error_hint(exc)}\n", file=sys.stderr)
        sys.exit(1)


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    """Show current configuration and connectivity status."""
    if _CONFIG_FILE.exists():
        _load_dotenv(_CONFIG_FILE)
    if Path(".env").exists():
        _load_dotenv(Path(".env"))

    ok_   = _ok("✓")
    fail_ = _err("✗")
    warn_ = _warn("-")

    print(f"\n  {_bold('KubeIntellect status')}\n")

    # Config file
    cfg_status = ok_ if _CONFIG_FILE.exists() else fail_
    cfg_note = "" if _CONFIG_FILE.exists() else f"  {_dim('→ run: kubeintellect init')}"
    print(f"  Config:    {cfg_status}  {_CONFIG_FILE}{cfg_note}")

    # LLM
    provider = os.environ.get("LLM_PROVIDER", "azure")
    if provider == "openai":
        model = os.environ.get("OPENAI_COORDINATOR_MODEL", "gpt-4o")
        has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        llm_status = ok_ if has_key else fail_
        note = "" if has_key else f"  {_dim('OPENAI_API_KEY missing — platform.openai.com/api-keys')}"
        print(f"  LLM:       {llm_status}  openai / {model}{note}")
    else:
        model = os.environ.get("AZURE_COORDINATOR_DEPLOYMENT", "gpt-4o")
        has_key = bool(os.environ.get("AZURE_OPENAI_API_KEY", "").strip())
        has_ep  = bool(os.environ.get("AZURE_OPENAI_ENDPOINT",  "").strip())
        llm_status = ok_ if (has_key and has_ep) else fail_
        missing = []
        if not has_key: missing.append("AZURE_OPENAI_API_KEY")
        if not has_ep:  missing.append("AZURE_OPENAI_ENDPOINT")
        note = f"  {_dim('missing: ' + ', '.join(missing))}" if missing else ""
        print(f"  LLM:       {llm_status}  azure / {model}{note}")

    # Database
    use_sqlite = os.environ.get("USE_SQLITE", "").lower() == "true"
    if use_sqlite:
        sqlite_path = os.path.expanduser(os.environ.get("SQLITE_PATH", "~/.kubeintellect/kubeintellect.db"))
        sqlite_exists = Path(sqlite_path).exists()
        db_status = ok_ if sqlite_exists else warn_
        db_note = "" if sqlite_exists else f"  {_dim('(will be created on first start)')}"
        print(f"  DB:        {db_status}  sqlite  {sqlite_path}{db_note}")
    else:
        dsn = _build_dsn()
        db_reachable = _check_db(dsn)
        db_host = os.environ.get("POSTGRES_HOST", "localhost")
        db_name = os.environ.get("POSTGRES_DB",   "kubeintellect")
        db_status = ok_ if db_reachable else fail_
        db_note = (
            "" if db_reachable
            else f"  {_dim('unreachable — add USE_SQLITE=true to ' + str(_CONFIG_FILE) + ' for SQLite')}"
        )
        print(f"  DB:        {db_status}  postgres  {db_host}/{db_name}{db_note}")

    # kubectl
    kubectl_found = subprocess.run(["which", "kubectl"], capture_output=True).returncode == 0
    kubectl_status = ok_ if kubectl_found else fail_
    kubectl_note = "" if kubectl_found else f"  {_dim('→ run: kubeintellect kind-setup')}"
    print(f"  kubectl:   {kubectl_status}  {'found' if kubectl_found else 'not found'}{kubectl_note}")

    # Kubeconfig
    kube_path = os.path.expanduser(os.environ.get("KUBECONFIG_PATH", "~/.kube/config"))
    kube_exists = Path(kube_path).exists()
    kube_status = ok_ if kube_exists else fail_
    kube_context = _get_kube_context(kube_path) if kube_exists else ""
    kube_note = (
        f"  {_dim('context: ' + kube_context)}" if kube_context
        else (f"  {_dim('file not found — set KUBECONFIG_PATH in ' + str(_CONFIG_FILE))}" if not kube_exists else "")
    )
    print(f"  Kube:      {kube_status}  {kube_path}{kube_note}")

    # Auth — show each key so users can copy it for kq / KUBE_Q_API_KEY
    admin_keys = os.environ.get("KUBEINTELLECT_ADMIN_KEYS", "").strip()
    op_keys    = os.environ.get("KUBEINTELLECT_OPERATOR_KEYS", "").strip()
    ro_keys    = os.environ.get("KUBEINTELLECT_READONLY_KEYS", "").strip()
    if any([admin_keys, op_keys, ro_keys]):
        print(f"  Auth:      {ok_}  enabled")
        for label, keys_str in (("admin   ", admin_keys), ("operator", op_keys), ("readonly", ro_keys)):
            if not keys_str:
                continue
            for key in keys_str.split(","):
                key = key.strip()
                if key:
                    print(f"    {_dim(label)}  {_bold(key)}")
        print(f"  {_dim('  → set KUBE_Q_API_KEY=<key> or pass --api-key <key> to kq')}")
    else:
        print(f"  Auth:      {warn_}  {_warn('open access')} {_dim('(no API keys set)')}")

    # Prometheus
    prom_url = os.environ.get("PROMETHEUS_URL", "").strip()
    if prom_url:
        prom_up = _http_ok(prom_url + "/-/healthy")
        print(f"  Prometheus:{ok_ if prom_up else fail_}  {prom_url}  "
              f"{_dim('reachable') if prom_up else _warn('unreachable')}")
    else:
        print(f"  Prometheus:{warn_}  {_dim('not configured')}")

    # Loki
    loki_url = os.environ.get("LOKI_URL", "").strip()
    if loki_url:
        loki_up = _http_ok(loki_url + "/ready")
        print(f"  Loki:      {ok_ if loki_up else fail_}  {loki_url}  "
              f"{_dim('reachable') if loki_up else _warn('unreachable')}")
    else:
        print(f"  Loki:      {warn_}  {_dim('not configured')}")

    # Grafana
    grafana_url = os.environ.get("GRAFANA_URL", "").strip()
    if grafana_url:
        grafana_up = _http_ok(grafana_url + "/api/health")
        print(f"  Grafana:   {ok_ if grafana_up else fail_}  {grafana_url}  "
              f"{_dim('reachable') if grafana_up else _warn('unreachable')}")
    else:
        print(f"  Grafana:   {warn_}  {_dim('not configured')}")

    # Langfuse
    langfuse_enabled = os.environ.get("LANGFUSE_ENABLED", "false").lower() == "true"
    if langfuse_enabled:
        lf_host = os.environ.get("LANGFUSE_HOST", "")
        lf_up = _http_ok(lf_host + "/api/public/health") if lf_host else False
        print(f"  Langfuse:  {ok_ if lf_up else fail_}  {lf_host}  "
              f"{_dim('reachable') if lf_up else _warn('unreachable')}")
    else:
        print(f"  Langfuse:  {warn_}  {_dim('disabled')}")

    # kube-q CLI — check venv-local bin first, then system PATH
    _kq_bin = Path(sys.executable).parent / "kq"
    kq_found = _kq_bin.exists() or subprocess.run(["which", "kq"], capture_output=True).returncode == 0
    if kq_found:
        print(f"  kube-q:    {ok_}  found")
    else:
        print(f"  kube-q:    {warn_}  {_dim('not installed → pipx install kube-q')}")

    # Config issue summary
    if _CONFIG_FILE.exists():
        cfg: dict[str, str] = {}
        _load_dotenv_dict(_CONFIG_FILE, cfg)
        issues = _validate_config(cfg)
        if issues:
            print(f"\n  {_warn('Configuration issues:')}\n")
            _print_issues(issues)
        else:
            print(f"\n  {_ok('✓')}  No configuration issues found.")
    print()


# ── set ───────────────────────────────────────────────────────────────────────

def cmd_set(args: argparse.Namespace) -> None:
    """Set one or more config values in ~/.kubeintellect/.env."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    lines = list(existing.splitlines(keepends=True))

    changed: list[str] = []
    for pair in args.assignments:
        if "=" not in pair:
            print(_err(f"  Invalid argument {pair!r} — expected KEY=VALUE"), file=sys.stderr)
            sys.exit(1)
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            print(_err(f"  Empty key in {pair!r}"), file=sys.stderr)
            sys.exit(1)
        # Replace existing line or append
        new_line = f"{key}={value}\n"
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        changed.append(f"  {_ok('✓')}  {key} = {_mask(key, value)}")

    _CONFIG_FILE.write_text("".join(lines))
    for msg in changed:
        print(msg)

    # Reload service if running so changes take effect immediately
    svc_active = subprocess.run(
        ["systemctl", "--user", "is-active", _SERVICE_NAME],
        capture_output=True, text=True,
    ).stdout.strip() == "active"
    if svc_active:
        subprocess.run(["systemctl", "--user", "restart", _SERVICE_NAME],
                       capture_output=True, check=False)
        print(_dim("  → service restarted to apply changes"))


# ── tool installers ───────────────────────────────────────────────────────────

def _ensure_tool(name: str, installer: "callable") -> None:
    if subprocess.run(["which", name], capture_output=True).returncode == 0:
        return
    print(f"  '{name}' not found — installing...")
    try:
        installer()
        print(f"  {_ok('✓')}  '{name}' installed.")
    except Exception as exc:
        print(_err(f"  Failed to install '{name}': {exc}"), file=sys.stderr)
        sys.exit(1)


def _install_kind() -> None:
    import urllib.request
    import platform
    arch = "amd64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    system = platform.system().lower()
    url = f"https://kind.sigs.k8s.io/dl/v0.23.0/kind-{system}-{arch}"
    urllib.request.urlretrieve(url, "/tmp/kind")
    subprocess.run(["chmod", "+x", "/tmp/kind"], check=True)
    subprocess.run(["sudo", "mv", "/tmp/kind", "/usr/local/bin/kind"], check=True)


def _install_kubectl() -> None:
    import urllib.request
    import platform
    arch = "amd64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    system = platform.system().lower()
    stable = urllib.request.urlopen("https://dl.k8s.io/release/stable.txt").read().decode().strip()
    url = f"https://dl.k8s.io/release/{stable}/bin/{system}/{arch}/kubectl"
    urllib.request.urlretrieve(url, "/tmp/kubectl")
    subprocess.run(["chmod", "+x", "/tmp/kubectl"], check=True)
    subprocess.run(["sudo", "mv", "/tmp/kubectl", "/usr/local/bin/kubectl"], check=True)


def _install_helm() -> None:
    import urllib.request
    script_url = "https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3"
    urllib.request.urlretrieve(script_url, "/tmp/get-helm.sh")
    subprocess.run(["chmod", "+x", "/tmp/get-helm.sh"], check=True)
    subprocess.run(["/tmp/get-helm.sh"], check=True)


# ── kind-setup ────────────────────────────────────────────────────────────────

def _configure_cluster_dns() -> None:
    dns_ip = _get_kube_dns_ip()
    if not dns_ip:
        print(_warn("  Warning: could not detect kube-dns IP — skipping cluster DNS setup."))
        return

    conf_dir  = Path("/etc/systemd/resolved.conf.d")
    conf_file = conf_dir / "kind-dns.conf"

    if conf_file.exists() and dns_ip in conf_file.read_text():
        print(f"  {_ok('✓')}  Cluster DNS already configured ({dns_ip}).")
        return

    conf_content = f"[Resolve]\nDNS={dns_ip}\nDomains=~cluster.local ~svc.cluster.local\n"
    print(f"  Configuring cluster DNS ({dns_ip}) so svc.cluster.local resolves from this host...")
    try:
        subprocess.run(["sudo", "mkdir", "-p", str(conf_dir)], check=True)
        tmp = Path("/tmp/kind-dns.conf")
        tmp.write_text(conf_content)
        subprocess.run(["sudo", "cp", str(tmp), str(conf_file)], check=True)
        subprocess.run(["sudo", "systemctl", "restart", "systemd-resolved"], check=True)
        print(f"  {_ok('✓')}  Cluster DNS configured — svc.cluster.local now resolves from this host.")
    except Exception as exc:
        print(_warn(f"  Warning: could not configure cluster DNS: {exc}"), file=sys.stderr)
        print(f"  To do it manually: sudo tee {conf_file} <<EOF\n{conf_content}EOF")
        print("  sudo systemctl restart systemd-resolved")


def _get_kube_dns_ip() -> str:
    try:
        result = subprocess.run(
            ["kubectl", "get", "svc", "kube-dns", "-n", "kube-system",
             "-o", "jsonpath={.spec.clusterIP}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def cmd_kind_setup(args: argparse.Namespace) -> None:
    """Create a local Kind cluster for testing KubeIntellect without a real cluster."""
    cluster_name = args.cluster_name

    _ensure_tool("kind",    _install_kind)
    _ensure_tool("kubectl", _install_kubectl)
    _ensure_tool("helm",    _install_helm)

    result = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True)
    existing = result.stdout.strip().splitlines()
    if cluster_name in existing:
        print(f"  {_ok('✓')}  Kind cluster '{cluster_name}' already exists — skipping creation.")
    else:
        print(f"  Creating Kind cluster '{cluster_name}'...")
        result = subprocess.run(
            ["kind", "create", "cluster", "--name", cluster_name],
            check=False,
        )
        if result.returncode != 0:
            print(_err("  Error: failed to create Kind cluster."), file=sys.stderr)
            sys.exit(1)
        print(f"  {_ok('✓')}  Cluster '{cluster_name}' created.")

    if not args.skip_ingress:
        print("\n  Installing nginx ingress controller...")
        ingress_url = (
            "https://raw.githubusercontent.com/kubernetes/ingress-nginx"
            "/main/deploy/static/provider/kind/deploy.yaml"
        )
        result = subprocess.run(["kubectl", "apply", "-f", ingress_url], check=False)
        if result.returncode != 0:
            print(_warn("  Warning: nginx ingress install failed — install it manually later."), file=sys.stderr)
        else:
            print(f"  {_ok('✓')}  nginx ingress installed.")

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing_config = _CONFIG_FILE.read_text() if _CONFIG_FILE.exists() else ""
    kube_path = str(Path.home() / ".kube" / "config")
    if "KUBECONFIG_PATH=" not in existing_config:
        with _CONFIG_FILE.open("a") as f:
            f.write(f"KUBECONFIG_PATH={kube_path}\n")
        print(f"  {_ok('✓')}  Updated {_CONFIG_FILE}: KUBECONFIG_PATH={kube_path}")

    _configure_cluster_dns()

    print(f"""
  {_bold('── Kind cluster ready ───────────────────────────────────────────────────')}
  Cluster:    {cluster_name}
  Kubeconfig: {kube_path}

  Next:
    kubeintellect status   # verify everything is ready
    kubeintellect serve    # start the API server
  {_bold('─────────────────────────────────────────────────────────────────────────')}
""")


# ── helpers ───────────────────────────────────────────────────────────────────

def _http_ok(url: str, timeout: float = 3.0) -> bool:
    """Return True if url returns a 2xx or 3xx response within timeout."""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 400
    except Exception:
        return False


def _load_dotenv_dict(path: Path, target: dict) -> None:
    """Load .env into a dict (does not touch os.environ)."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            target[key] = value


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — sets env vars that are not already set."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _build_dsn() -> str:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        return db_url
    host     = os.environ.get("POSTGRES_HOST",     "localhost")
    port     = os.environ.get("POSTGRES_PORT",     "5432")
    db       = os.environ.get("POSTGRES_DB",       "kubeintellectdb")
    user     = os.environ.get("POSTGRES_USER",     "kubeuser")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _redact_dsn(dsn: str) -> str:
    import re
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", dsn)


def _check_db(dsn: str) -> bool:
    try:
        import psycopg  # type: ignore[import-untyped]
        with psycopg.connect(dsn, connect_timeout=3, autocommit=True):
            return True
    except Exception:
        return False


def _get_kube_context(kube_path: str) -> str:
    try:
        result = subprocess.run(
            ["kubectl", "--kubeconfig", kube_path, "config", "current-context"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kubeintellect",
        description="KubeIntellect — AI-powered Kubernetes management platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
examples:
  kubeintellect init                             # first-time setup wizard
  kubeintellect serve                            # start server on :8000
  kubeintellect serve --port 9000                # custom port
  kubeintellect status                           # check all connectivity
  kubeintellect set OPENAI_API_KEY=sk-proj-...  # update a single config value
  kubeintellect set USE_SQLITE=true              # switch to SQLite database
  kubeintellect service install                  # install as background service
  kubeintellect service logs                     # tail live service logs
  kubeintellect kind-setup                       # create a local test cluster

config file:  ~/.kubeintellect/.env
  All options are written with comments when you run 'kubeintellect init'.
  Edit the file directly or use 'kubeintellect set KEY=VALUE'.

  Key options:
    LLM_PROVIDER                openai or azure
    OPENAI_API_KEY              OpenAI API key
    AZURE_OPENAI_API_KEY        Azure OpenAI API key
    AZURE_OPENAI_ENDPOINT       Azure endpoint URL
    USE_SQLITE                  true = SQLite (default), unset = PostgreSQL
    KUBEINTELLECT_ADMIN_KEYS    comma-separated admin API keys
    PROMETHEUS_URL              Prometheus endpoint for metrics queries
    LOKI_URL                    Loki endpoint for log queries

docs:         https://kubeintellect.com
github:       https://github.com/mskazemi/kubeintellect
support:      mohsen.seyedkazemi@gmail.com
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser(
        "init",
        help="Interactive setup wizard (writes ~/.kubeintellect/.env)",
        description=(
            "Create or update ~/.kubeintellect/.env interactively.\n"
            "Detects any existing configuration and offers to reuse each value.\n"
            "Validates the result and prints actionable hints for any issues found."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # serve
    serve_p = sub.add_parser(
        "serve",
        help="Start the API server",
        description=(
            "Start the KubeIntellect FastAPI server via uvicorn.\n"
            "Loads ~/.kubeintellect/.env, validates config, then starts.\n"
            "Misconfigurations are shown as warnings — the server still starts."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  kubeintellect serve\n"
            "  kubeintellect serve --port 9000\n"
            "  kubeintellect serve --host 127.0.0.1 --port 8080 --reload\n"
        ),
    )
    serve_p.add_argument("--host",   default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve_p.add_argument("--port",   type=int, default=8000, help="Bind port (default: 8000)")
    serve_p.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")

    # db-init
    sub.add_parser(
        "db-init",
        help="Initialize the database schema",
        description=(
            "Apply the KubeIntellect schema to your PostgreSQL database.\n"
            "Uses DATABASE_URL (if set) or POSTGRES_* vars from ~/.kubeintellect/.env.\n"
            "In SQLite mode (USE_SQLITE=true) the schema is created automatically on first start."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # status
    sub.add_parser(
        "status",
        help="Show configuration and connectivity status",
        description=(
            "Check all components: LLM provider, database, kubectl, kubeconfig, auth,\n"
            "and observability tools. Prints ✓/✗/- per component with fix hints."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # set
    set_p = sub.add_parser(
        "set",
        help="Set config values in ~/.kubeintellect/.env",
        description="Set one or more configuration values without running the full wizard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  kubeintellect set AZURE_OPENAI_API_KEY=sk-...\n"
            "  kubeintellect set AZURE_OPENAI_ENDPOINT=https://my.openai.azure.com/\n"
            "  kubeintellect set USE_SQLITE=true\n"
            "  kubeintellect set PROMETHEUS_URL=http://localhost:9090\n"
        ),
    )
    set_p.add_argument(
        "assignments",
        nargs="+",
        metavar="KEY=VALUE",
        help="One or more KEY=VALUE pairs to write to the config file",
    )

    # service
    service_p = sub.add_parser(
        "service",
        help="Manage the background kubeintellect server service",
        description=(
            "Install, remove, or control the systemd user service that runs\n"
            "kubeintellect serve automatically on login.\n"
            "Requires systemd (Linux only)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  kubeintellect service install    # enable and start the service\n"
            "  kubeintellect service status     # show current service state\n"
            "  kubeintellect service logs       # tail live service logs\n"
            "  kubeintellect service stop       # stop without uninstalling\n"
            "  kubeintellect service uninstall  # remove the service entirely\n"
        ),
    )
    service_p.add_argument(
        "action",
        choices=["install", "uninstall", "start", "stop", "status", "logs"],
        help="Action to perform on the service",
    )

    # kind-setup
    kind_p = sub.add_parser(
        "kind-setup",
        help="Create a local Kind cluster for testing",
        description=(
            "Create a Kind cluster with nginx ingress and cluster DNS configured.\n"
            "Installs kind, kubectl, and helm automatically if not found.\n"
            "Ideal for local development without a real Kubernetes cluster."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    kind_p.add_argument(
        "--cluster-name", default="kubeintellect",
        help="Kind cluster name (default: kubeintellect)",
    )
    kind_p.add_argument(
        "--skip-ingress", action="store_true",
        help="Skip nginx ingress controller installation",
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "db-init":
        cmd_db_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "kind-setup":
        cmd_kind_setup(args)
    elif args.command == "set":
        cmd_set(args)
    elif args.command == "service":
        cmd_service(args)


if __name__ == "__main__":
    main()
