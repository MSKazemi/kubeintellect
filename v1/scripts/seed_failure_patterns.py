#!/usr/bin/env python3
# scripts/seed_failure_patterns.py
"""
Seed the failure_patterns table with the 30 canonical Kubernetes failure patterns.

Idempotent: patterns already present (by pattern_id) are skipped — safe to re-run.

Usage:
    uv run python scripts/seed_failure_patterns.py

Requires POSTGRES_* env vars (or a .env file in the project root).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app.models.failure_pattern import FailurePattern

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _dsn() -> str:
    host     = os.environ.get("POSTGRES_HOST", "localhost")
    db       = os.environ.get("POSTGRES_DB", "kubeintellectdb")
    user     = os.environ.get("POSTGRES_USER", "kubeuser")
    password = os.environ.get("POSTGRES_PASSWORD", "password")
    return f"postgresql://{user}:{password}@{host}/{db}"


# ---------------------------------------------------------------------------
# Canonical failure patterns
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)

PATTERNS: list[FailurePattern] = [

    # ── 1 ─ OOMKilled ───────────────────────────────────────────────────────
    FailurePattern(
        pattern_id="oomkilled_memory_limit",
        type="OOMKilled",
        signals=[
            "OOMKilled", "out of memory", "memory limit", "exit code 137",
            "MemoryError", "heap", "memory pressure", "evicted memory",
        ],
        root_cause="Container exceeded its memory limit and was killed by the Linux OOM killer.",
        recommended_checks=[
            "Run get_pod_logs with previous=True to retrieve the last container's logs.",
            "Check current memory limits and requests with describe_resource.",
            "Run top_pods sorted by memory to see live usage vs. limits.",
            "Check node memory pressure with top_nodes.",
        ],
        remediation_steps=[
            "Increase the container memory limit in the Deployment spec.",
            "Profile the application for memory leaks if usage grows unbounded.",
            "If the workload is bursty, consider a VPA to auto-tune limits.",
        ],
        confidence=0.9,
        verified=True,
    ),

    # ── 2 ─ CrashLoopBackOff ────────────────────────────────────────────────
    FailurePattern(
        pattern_id="crashloopbackoff_generic",
        type="CrashLoopBackOff",
        signals=[
            "CrashLoopBackOff", "crash loop", "back-off restarting",
            "restart count", "exit code", "container died",
        ],
        root_cause="Container exits non-zero repeatedly; kubelet backs off restart attempts exponentially.",
        recommended_checks=[
            "Get current logs with get_pod_logs (tail_lines=100).",
            "Get previous container logs with get_pod_logs previous=True.",
            "Describe the pod with describe_resource to see last state and exit code.",
            "Check liveness/readiness probe config — misconfigured probes kill healthy containers.",
        ],
        remediation_steps=[
            "Fix the application crash based on the logs.",
            "If a config or secret is missing, patch the Deployment env vars.",
            "Temporarily set terminationGracePeriodSeconds=0 to observe the crash faster in dev.",
        ],
        confidence=0.88,
        verified=True,
    ),

    # ── 3 ─ ImagePullBackOff ────────────────────────────────────────────────
    FailurePattern(
        pattern_id="imagepullbackoff",
        type="ImagePullBackOff",
        signals=[
            "ImagePullBackOff", "ErrImagePull", "pull access denied",
            "image not found", "repository does not exist",
            "unauthorized", "registry", "tag", "digest",
        ],
        root_cause="kubelet cannot pull the container image: wrong tag, missing registry credentials, or private registry.",
        recommended_checks=[
            "Describe the pod to see the exact pull error message.",
            "Verify the image name and tag exist in the registry.",
            "Check for an imagePullSecret on the pod spec.",
            "Confirm the ServiceAccount has access to the pull secret.",
        ],
        remediation_steps=[
            "Correct the image name/tag in the Deployment spec.",
            "Create an imagePullSecret and reference it in the pod spec.",
            "If the registry is private, ensure the secret is in the correct namespace.",
        ],
        confidence=0.92,
        verified=True,
    ),

    # ── 4 ─ PendingInsufficientResources ────────────────────────────────────
    FailurePattern(
        pattern_id="pending_insufficient_resources",
        type="Pending",
        signals=[
            "Pending", "insufficient cpu", "insufficient memory",
            "unschedulable", "no nodes available", "0/3 nodes",
            "taints", "tolerations", "node selector",
        ],
        root_cause="Pod cannot be scheduled because no node has sufficient free resources or matches the scheduling constraints.",
        recommended_checks=[
            "Describe the pod for the scheduler's reason (kubectl describe → Events).",
            "Run top_nodes to see current CPU/memory availability.",
            "Check if node taints block scheduling with describe_resource on affected nodes.",
            "Verify resource requests are not set higher than any node's allocatable capacity.",
        ],
        remediation_steps=[
            "Scale up the node group / add nodes.",
            "Reduce the pod's resource requests if over-specified.",
            "Add tolerations for node taints or remove unnecessary taints.",
        ],
        confidence=0.85,
        verified=True,
    ),

    # ── 5 ─ LivenessProbeFailure ─────────────────────────────────────────────
    FailurePattern(
        pattern_id="liveness_probe_failure",
        type="LivenessProbeFailure",
        signals=[
            "liveness probe failed", "probe failed", "liveness",
            "killing container", "unhealthy", "back-off restarting failed container",
        ],
        root_cause="Liveness probe is returning failure, causing kubelet to kill and restart the container.",
        recommended_checks=[
            "Describe the pod to see liveness probe config and failure events.",
            "Get recent pod logs to see if the app is actually unhealthy or the probe is misconfigured.",
            "Verify the probe path, port, and initialDelaySeconds are correct.",
            "Check if the probe endpoint is accessible inside the container.",
        ],
        remediation_steps=[
            "Increase initialDelaySeconds if the app needs more time to start.",
            "Fix the probe path or port to match the actual health endpoint.",
            "If the app is genuinely unhealthy, fix the underlying application bug.",
        ],
        confidence=0.82,
        verified=True,
    ),

    # ── 6 ─ ReadinessProbeFailure ────────────────────────────────────────────
    FailurePattern(
        pattern_id="readiness_probe_failure",
        type="ReadinessProbeFailure",
        signals=[
            "readiness probe failed", "not ready", "endpoints",
            "service no endpoints", "traffic", "0/1 ready",
        ],
        root_cause="Readiness probe failing keeps the pod out of Service endpoints, routing no traffic to it.",
        recommended_checks=[
            "Describe the pod to see readiness probe configuration.",
            "Check pod logs for startup errors preventing the app from becoming ready.",
            "Check service endpoints with check_service_endpoints.",
            "Verify the readiness probe path and port match the app's actual ready state.",
        ],
        remediation_steps=[
            "Fix the readiness probe path/port.",
            "Allow more startup time with initialDelaySeconds.",
            "Investigate application startup failures logged before the probe fires.",
        ],
        confidence=0.80,
        verified=True,
    ),

    # ── 7 ─ PVC Pending / Not Bound ──────────────────────────────────────────
    FailurePattern(
        pattern_id="pvc_pending_not_bound",
        type="PVCPending",
        signals=[
            "PVC", "PersistentVolumeClaim", "pending", "not bound",
            "no persistent volumes available", "storageclass", "volume",
            "provisioner", "WaitForFirstConsumer",
        ],
        root_cause="PVC cannot find a matching PV: StorageClass not found, provisioner unavailable, or no suitable PV exists.",
        recommended_checks=[
            "Describe the PVC with describe_persistent_volume_claim.",
            "List StorageClasses to verify the requested class exists.",
            "Check provisioner pod logs (e.g. csi-provisioner) for errors.",
            "Verify node affinity constraints are not blocking WaitForFirstConsumer binding.",
        ],
        remediation_steps=[
            "Create the StorageClass or correct the PVC's storageClassName.",
            "Manually create a matching PV if dynamic provisioning is unavailable.",
            "Check CSI driver pods are running in kube-system.",
        ],
        confidence=0.83,
        verified=True,
    ),

    # ── 8 ─ Deployment Rollout Stuck ────────────────────────────────────────
    FailurePattern(
        pattern_id="deployment_rollout_stuck",
        type="RolloutStuck",
        signals=[
            "rollout", "stuck", "not progressing", "deadline exceeded",
            "ProgressDeadlineExceeded", "unavailable", "replica",
            "update", "maxUnavailable",
        ],
        root_cause="Deployment rollout has stalled: new pods are not becoming ready within the progress deadline.",
        recommended_checks=[
            "Run rollout_status for the deployment.",
            "Describe the deployment to see conditions and events.",
            "Get logs from the new (failing) pods.",
            "Check if image pull, resource limits, or probe failures are blocking pod readiness.",
        ],
        remediation_steps=[
            "Fix whatever is preventing the new pods from becoming ready.",
            "Use rollout_undo to roll back to the previous working revision.",
            "Increase progressDeadlineSeconds if the app genuinely needs more time.",
        ],
        confidence=0.84,
        verified=True,
    ),

    # ── 9 ─ RBAC Forbidden ──────────────────────────────────────────────────
    FailurePattern(
        pattern_id="rbac_forbidden",
        type="RBACForbidden",
        signals=[
            "forbidden", "RBAC", "unauthorized", "403",
            "cannot get", "cannot list", "cannot create",
            "User", "ServiceAccount", "ClusterRole", "Role",
        ],
        root_cause="ServiceAccount or User lacks the RBAC permissions required to perform the requested API operation.",
        recommended_checks=[
            "Describe the offending pod to see its ServiceAccount.",
            "List RoleBindings and ClusterRoleBindings for the ServiceAccount.",
            "Use describe_resource on the Role/ClusterRole to see what verbs are permitted.",
            "Check audit logs for the specific resource and verb that was denied.",
        ],
        remediation_steps=[
            "Create or update a Role/ClusterRole with the missing permissions.",
            "Bind it to the ServiceAccount via RoleBinding or ClusterRoleBinding.",
            "If using ClusterRole for namespace-scoped access, use RoleBinding not ClusterRoleBinding.",
        ],
        confidence=0.87,
        verified=True,
    ),

    # ── 10 ─ Service No Endpoints ─────────────────────────────────────────────
    FailurePattern(
        pattern_id="service_no_endpoints",
        type="ServiceUnavailable",
        signals=[
            "no endpoints", "service unavailable", "connection refused",
            "503", "ECONNREFUSED", "selector", "endpoints empty",
        ],
        root_cause="Service selector matches no ready pods — either selector mismatch, pods not ready, or no pods exist.",
        recommended_checks=[
            "Run check_service_endpoints to see current endpoint count.",
            "Describe the Service to verify the selector labels.",
            "List pods with matching labels to confirm they exist and are Ready.",
            "Check if readiness probes are failing on the pods.",
        ],
        remediation_steps=[
            "Fix the Service selector to match the pod labels.",
            "Fix readiness probes so pods enter Ready state.",
            "If no pods exist, scale the Deployment up.",
        ],
        confidence=0.86,
        verified=True,
    ),

    # ── 11 ─ NetworkPolicy Blocking ───────────────────────────────────────────
    FailurePattern(
        pattern_id="network_policy_blocking",
        type="NetworkPolicyBlocking",
        signals=[
            "NetworkPolicy", "network policy", "connection timeout",
            "blocked", "ingress denied", "egress denied",
            "pod cannot connect", "traffic blocked",
        ],
        root_cause="A NetworkPolicy is blocking ingress or egress traffic between pods or namespaces.",
        recommended_checks=[
            "List NetworkPolicies in the namespace.",
            "Describe each policy to check ingress/egress rules and podSelector.",
            "Verify the source pod's labels match the NetworkPolicy's podSelector.",
            "Check if a deny-all policy exists without an allow for the required port.",
        ],
        remediation_steps=[
            "Add an allow rule in the NetworkPolicy for the required pod-to-pod traffic.",
            "If testing, temporarily delete the restrictive NetworkPolicy.",
            "Add namespace labels to enable cross-namespace traffic if needed.",
        ],
        confidence=0.78,
        verified=False,
    ),

    # ── 12 ─ Container Config Error ───────────────────────────────────────────
    FailurePattern(
        pattern_id="container_config_error",
        type="CreateContainerConfigError",
        signals=[
            "CreateContainerConfigError", "configmap not found",
            "secret not found", "env from", "volume mount",
            "references non-existent", "missing secret", "missing configmap",
        ],
        root_cause="Pod references a ConfigMap or Secret that does not exist in the same namespace.",
        recommended_checks=[
            "Describe the pod to see the exact missing resource name.",
            "List ConfigMaps and Secrets in the namespace.",
            "Check whether the reference is in the correct namespace.",
            "Verify spelling of ConfigMap/Secret name in the Deployment spec.",
        ],
        remediation_steps=[
            "Create the missing ConfigMap or Secret.",
            "Correct the name/namespace reference in the Deployment spec.",
            "If the secret should come from a different namespace, use secretKeyRef with a cross-ns solution.",
        ],
        confidence=0.88,
        verified=True,
    ),

    # ── 13 ─ Evicted Pod ─────────────────────────────────────────────────────
    FailurePattern(
        pattern_id="pod_evicted",
        type="Evicted",
        signals=[
            "Evicted", "eviction", "disk pressure", "DiskPressure",
            "ephemeral storage", "low disk", "node condition",
            "MemoryPressure",
        ],
        root_cause="Pod was evicted from its node due to resource pressure (disk, memory, or PID) exceeding eviction thresholds.",
        recommended_checks=[
            "Describe the evicted pod for the eviction message.",
            "Run top_nodes to see node resource pressure conditions.",
            "Check ephemeral storage usage: log volumes, emptyDir, container logs.",
            "Identify which containers are writing large amounts of data.",
        ],
        remediation_steps=[
            "Add ephemeral storage limits to the container spec.",
            "Clean up log rotation and large emptyDir mounts.",
            "If the node is in persistent DiskPressure, free disk space or expand the disk.",
        ],
        confidence=0.82,
        verified=True,
    ),

    # ── 14 ─ StatefulSet Rollout Stuck ────────────────────────────────────────
    FailurePattern(
        pattern_id="statefulset_rollout_stuck",
        type="StatefulSetStuck",
        signals=[
            "StatefulSet", "statefulset", "ordered rollout",
            "partition", "not ready", "replica stuck",
            "PVC not bound", "volume claim",
        ],
        root_cause="StatefulSet ordered rollout is blocked: a pod is not reaching Ready state, halting the ordered update.",
        recommended_checks=[
            "Run rollout_status on the StatefulSet.",
            "Describe the stalled pod to see events and conditions.",
            "Check PVC status for the stalled pod — PVC binding failure blocks pod start.",
            "Get pod logs to see application startup errors.",
        ],
        remediation_steps=[
            "Fix the root cause preventing the stalled pod from becoming ready.",
            "If using partition update, verify the partition value is correct.",
            "Use rollout_undo to revert if the new image is the problem.",
        ],
        confidence=0.80,
        verified=False,
    ),

    # ── 15 ─ CPU Throttling ───────────────────────────────────────────────────
    FailurePattern(
        pattern_id="cpu_throttling",
        type="CPUThrottling",
        signals=[
            "cpu throttling", "throttled", "CPU limit",
            "container_cpu_cfs_throttled", "latency", "slow response",
            "high cpu", "cpu usage",
        ],
        root_cause="Container CPU usage repeatedly hits its limit, causing CFS throttling and increased request latency.",
        recommended_checks=[
            "Run top_pods sorted by CPU to confirm throttling.",
            "Query Prometheus for container_cpu_cfs_throttled_seconds_total.",
            "Describe the deployment to see CPU request and limit values.",
            "Check application performance metrics for increased p99 latency.",
        ],
        remediation_steps=[
            "Increase the container's CPU limit.",
            "Profile the application for CPU hot paths and optimize.",
            "If requests << limits, also raise the request for better scheduling.",
        ],
        confidence=0.78,
        verified=False,
    ),

    # ── 16 ─ DaemonSet Not Scheduled ──────────────────────────────────────────
    FailurePattern(
        pattern_id="daemonset_not_scheduled",
        type="DaemonSetNotScheduled",
        signals=[
            "DaemonSet", "daemonset", "not scheduled", "desired: 0",
            "node selector", "nodeAffinity", "taint", "toleration",
            "scheduling disabled",
        ],
        root_cause="DaemonSet pods are not scheduled on one or more nodes due to taint/toleration or node-selector mismatch.",
        recommended_checks=[
            "Describe the DaemonSet to see nodeSelector and tolerations.",
            "List nodes and their taints with describe_resource.",
            "Check whether target nodes are cordoned (unschedulable).",
            "Verify the DaemonSet's nodeAffinity matches node labels.",
        ],
        remediation_steps=[
            "Add the required tolerations to the DaemonSet spec.",
            "Correct the nodeSelector or nodeAffinity to match node labels.",
            "Uncordon nodes that were incorrectly cordoned.",
        ],
        confidence=0.80,
        verified=False,
    ),

    # ── 17 ─ Job Failing / Backoff ────────────────────────────────────────────
    FailurePattern(
        pattern_id="job_backoff_limit_exceeded",
        type="JobBackoffLimit",
        signals=[
            "Job", "BackoffLimitExceeded", "job failed",
            "backoff limit", "completions", "activeDeadlineSeconds",
            "failed pods",
        ],
        root_cause="Job's pods are consistently failing; backoffLimit retries are exhausted.",
        recommended_checks=[
            "Describe the Job to see failure count and backoff events.",
            "Get logs from the failed pod (job pods retain logs after failure).",
            "Check exit code — non-zero indicates application failure vs infrastructure.",
            "Verify environment variables, secrets, and volume mounts for the job pod.",
        ],
        remediation_steps=[
            "Fix the application error causing the non-zero exit code.",
            "Increase backoffLimit if failures are transient.",
            "Set activeDeadlineSeconds to prevent indefinite retries.",
        ],
        confidence=0.83,
        verified=True,
    ),

    # ── 18 ─ High Memory Pressure on Node ────────────────────────────────────
    FailurePattern(
        pattern_id="node_memory_pressure",
        type="NodeMemoryPressure",
        signals=[
            "MemoryPressure", "node memory", "node pressure",
            "eviction threshold", "system OOM", "allocatable memory",
            "kube-reserved",
        ],
        root_cause="Node-level memory usage exceeds kubelet eviction thresholds, triggering pod evictions.",
        recommended_checks=[
            "Run top_nodes to check memory usage across all nodes.",
            "List evicted pods on the affected node.",
            "Check kubelet eviction thresholds in node config.",
            "Identify which pods are consuming the most memory with top_pods.",
        ],
        remediation_steps=[
            "Evict high-memory pods and redistribute them.",
            "Add memory requests/limits to pods that have none set.",
            "Scale the node group or increase node memory.",
        ],
        confidence=0.81,
        verified=False,
    ),

    # ── 19 ─ Ingress Not Routing ──────────────────────────────────────────────
    FailurePattern(
        pattern_id="ingress_not_routing",
        type="IngressNotRouting",
        signals=[
            "ingress", "404", "502", "backend", "host", "path",
            "ingress class", "rewrite", "no address",
        ],
        root_cause="Ingress rule misconfiguration or missing backend Service prevents traffic routing.",
        recommended_checks=[
            "Describe the Ingress to see rules, TLS config, and address assignment.",
            "Verify the backend Service name and port match the Ingress rule.",
            "Check ingress controller pod logs for routing errors.",
            "Confirm the IngressClass is correct and the controller is running.",
        ],
        remediation_steps=[
            "Fix the backend Service name or port in the Ingress spec.",
            "Ensure the Service is in the same namespace as the Ingress.",
            "Add or correct the host field if virtual hosting is used.",
        ],
        confidence=0.79,
        verified=False,
    ),

    # ── 20 ─ TLS / Certificate Expired ───────────────────────────────────────
    FailurePattern(
        pattern_id="tls_certificate_expired",
        type="TLSCertExpired",
        signals=[
            "certificate expired", "TLS", "x509", "SSL",
            "certificate verify failed", "handshake", "HTTPS",
            "cert-manager",
        ],
        root_cause="TLS certificate has expired or is invalid, causing HTTPS connections to fail.",
        recommended_checks=[
            "Check the certificate expiry: kubectl get certificate -A.",
            "Describe the Certificate resource for cert-manager status.",
            "Check cert-manager controller logs for renewal failures.",
            "Verify the certificate's Secret exists and has the correct keys.",
        ],
        remediation_steps=[
            "Manually trigger cert-manager renewal: kubectl annotate cert X cert-manager.io/issue-once=true.",
            "If not using cert-manager, replace the TLS Secret with a valid certificate.",
            "Check DNS resolution for the cert's ACME challenge if using Let's Encrypt.",
        ],
        confidence=0.86,
        verified=True,
    ),

    # ── 21 ─ NodeNotReady Disk Pressure (additional) ─────────────────────────
    FailurePattern(
        pattern_id="node_not_ready_disk_pressure",
        type="NodeNotReady",
        signals=[
            "NodeNotReady", "NotReady", "DiskPressure", "disk pressure",
            "node condition", "kubelet", "eviction", "inode",
            "low disk space",
        ],
        root_cause="Node disk usage exceeds kubelet DiskPressure threshold, causing NodeNotReady condition and scheduling suspension.",
        recommended_checks=[
            "Describe the node to see DiskPressure condition and last transition time.",
            "SSH to the node and check df -h for disk usage by filesystem.",
            "Check /var/log and /var/lib/docker (or containerd) for large log files.",
            "List evicted pods on this node.",
        ],
        remediation_steps=[
            "Run docker system prune / crictl rmi to remove unused images.",
            "Rotate or truncate large container log files.",
            "Expand the node's disk volume or add a larger data disk.",
            "Set log rotation limits in the container runtime config.",
        ],
        confidence=0.85,
        verified=True,
    ),

    # ── 22 ─ HPA Missing Metrics Server ──────────────────────────────────────
    FailurePattern(
        pattern_id="hpa_missing_metrics_server",
        type="HPAMissingMetrics",
        signals=[
            "HPA", "HorizontalPodAutoscaler", "metrics server",
            "unable to get metrics", "unknown", "metrics not available",
            "failed to get cpu", "no metrics",
        ],
        root_cause="HPA cannot scale because metrics-server is not installed or not responding to the metrics API.",
        recommended_checks=[
            "Describe the HPA to see the current metrics and conditions.",
            "Check if metrics-server is running: list pods in kube-system.",
            "Run top_pods to verify metrics-server is returning data.",
            "Check metrics-server logs for API connectivity errors.",
        ],
        remediation_steps=[
            "Install metrics-server if absent: kubectl apply -f metrics-server.yaml.",
            "Restart metrics-server if it is running but returning no data.",
            "Verify metrics-server's APIService is registered: kubectl get apiservice v1beta1.metrics.k8s.io.",
        ],
        confidence=0.88,
        verified=True,
    ),

    # ── 23 ─ Pod Stuck Terminating / Finalizer ───────────────────────────────
    FailurePattern(
        pattern_id="pod_stuck_terminating_finalizer",
        type="StuckTerminating",
        signals=[
            "Terminating", "stuck terminating", "finalizer",
            "cannot delete", "force delete", "grace period",
            "DeletionTimestamp",
        ],
        root_cause="Pod has a finalizer that a controller is not removing, or the kubelet is unreachable and cannot complete graceful termination.",
        recommended_checks=[
            "Describe the pod to see finalizers and DeletionTimestamp.",
            "Check if the owning controller (e.g. StatefulSet controller) is running.",
            "Verify the node the pod runs on is reachable and kubelet is healthy.",
            "Check for any webhook that may be blocking deletion.",
        ],
        remediation_steps=[
            "If safe, patch finalizers to empty: kubectl patch pod X -p '{\"metadata\":{\"finalizers\":[]}}'.",
            "Force delete: kubectl delete pod X --force --grace-period=0 (last resort).",
            "Fix the controller that owns the finalizer so it completes cleanup.",
        ],
        confidence=0.83,
        verified=True,
    ),

    # ── 24 ─ Ingress 503 Missing Backend ─────────────────────────────────────
    FailurePattern(
        pattern_id="ingress_503_missing_backend",
        type="Ingress503",
        signals=[
            "503", "service unavailable", "ingress", "backend",
            "no healthy upstream", "nginx upstream",
            "upstream connect error", "no endpoints",
        ],
        root_cause="Ingress controller cannot reach the backend Service because it has no ready endpoints.",
        recommended_checks=[
            "Run check_service_endpoints on the backend Service.",
            "Describe the backend pods to confirm they are Ready.",
            "Check ingress controller access logs for the upstream cluster.",
            "Verify the Service port matches the pod's containerPort.",
        ],
        remediation_steps=[
            "Fix the underlying pod readiness issue (see readiness probe pattern).",
            "Correct the Service port or targetPort to match the container.",
            "Scale up the Deployment if zero replicas are running.",
        ],
        confidence=0.84,
        verified=True,
    ),

    # ── 25 ─ Secret Wrong Namespace ───────────────────────────────────────────
    FailurePattern(
        pattern_id="secret_wrong_namespace",
        type="SecretNamespaceMismatch",
        signals=[
            "secret", "not found", "namespace", "cross-namespace",
            "envFrom", "secretKeyRef", "imagePullSecret",
            "secret does not exist",
        ],
        root_cause="Pod references a Secret that exists in a different namespace; Kubernetes Secrets are namespace-scoped.",
        recommended_checks=[
            "Describe the pod to identify which Secret is missing.",
            "List Secrets across all namespaces to find where it actually lives.",
            "Confirm the pod's namespace and the secret's namespace match.",
            "Check if the Secret was accidentally created in the wrong namespace.",
        ],
        remediation_steps=[
            "Copy the Secret to the correct namespace: kubectl get secret X -n src -o yaml | kubectl apply -n dst -f -.",
            "If using ExternalSecrets or Vault, update the sync target namespace.",
            "For imagePullSecrets, the secret must be in the same namespace as the pod.",
        ],
        confidence=0.86,
        verified=True,
    ),

    # ── 26 ─ Resource Quota Exceeded ──────────────────────────────────────────
    FailurePattern(
        pattern_id="resource_quota_exceeded",
        type="ResourceQuotaExceeded",
        signals=[
            "resource quota", "quota exceeded", "exceeded quota",
            "forbidden: exceeded", "ResourceQuota",
            "cpu quota", "memory quota", "pods quota",
        ],
        root_cause="Namespace ResourceQuota is exhausted; new pods, services, or resources cannot be created.",
        recommended_checks=[
            "Describe the ResourceQuota in the namespace.",
            "List all running pods to identify high-consumer workloads.",
            "Check if requests/limits are set on all pods (unset = quota not tracked correctly).",
            "Compare current usage vs quota hard limits.",
        ],
        remediation_steps=[
            "Increase the ResourceQuota limits for the namespace.",
            "Delete unused or completed pods to free quota.",
            "Ensure all pods have resource requests/limits set to make quota accounting accurate.",
        ],
        confidence=0.87,
        verified=True,
    ),

    # ── 27 ─ PDB Blocking Rollout ─────────────────────────────────────────────
    FailurePattern(
        pattern_id="pdb_blocking_rollout",
        type="PDBBlockingRollout",
        signals=[
            "PodDisruptionBudget", "PDB", "disruption budget",
            "cannot evict", "minAvailable", "maxUnavailable",
            "rollout blocked", "eviction denied",
        ],
        root_cause="PodDisruptionBudget prevents eviction of pods during a node drain or rollout, blocking progress.",
        recommended_checks=[
            "Describe the PDB to see minAvailable/maxUnavailable and current disruptions allowed.",
            "Check how many replicas are currently Ready vs the PDB minimum.",
            "Check rollout_status to confirm the rollout is stalled waiting on PDB.",
            "Verify the PDB's selector matches the correct pods.",
        ],
        remediation_steps=[
            "Temporarily increase Deployment replicas to satisfy PDB minAvailable before rolling.",
            "Adjust the PDB policy if it is more restrictive than needed.",
            "If draining a node, wait for pods to reschedule elsewhere before the PDB allows eviction.",
        ],
        confidence=0.81,
        verified=False,
    ),

    # ── 28 ─ cert-manager Certificate Expired ────────────────────────────────
    FailurePattern(
        pattern_id="cert_manager_renewal_failure",
        type="CertManagerRenewalFailure",
        signals=[
            "cert-manager", "certificate", "renewal", "ACME",
            "CertificateRequest", "challenge failed", "order failed",
            "Issuer", "ClusterIssuer",
        ],
        root_cause="cert-manager failed to renew the certificate; ACME challenge may have failed or the Issuer is misconfigured.",
        recommended_checks=[
            "Describe the Certificate resource to see renewal status and conditions.",
            "Describe the CertificateRequest and Order for ACME challenge status.",
            "Check cert-manager controller pod logs for issuer errors.",
            "Verify DNS resolves to the correct IP for HTTP-01 challenge.",
        ],
        remediation_steps=[
            "Delete the failed CertificateRequest to trigger a fresh renewal attempt.",
            "Update the Issuer/ClusterIssuer with correct ACME credentials.",
            "For DNS-01, ensure the DNS provider API credentials are valid.",
            "If the cert is already expired, restart affected pods after renewal to pick up the new secret.",
        ],
        confidence=0.83,
        verified=False,
    ),

    # ── 29 ─ Admission Webhook Timeout ────────────────────────────────────────
    FailurePattern(
        pattern_id="admission_webhook_timeout",
        type="AdmissionWebhookTimeout",
        signals=[
            "admission webhook", "webhook", "timeout", "context deadline exceeded",
            "failed calling webhook", "connection refused webhook",
            "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration",
        ],
        root_cause="An admission webhook is unreachable or timing out, blocking all resource creation/modification in the cluster.",
        recommended_checks=[
            "List MutatingWebhookConfigurations and ValidatingWebhookConfigurations.",
            "Describe the failing webhook to see the service reference and failurePolicy.",
            "Check if the webhook's backing Service and pod are running.",
            "Inspect webhook pod logs for panic or error on admission.",
        ],
        remediation_steps=[
            "Restart the webhook deployment if it is unhealthy.",
            "If the webhook pod is OOMKilled, increase its memory limit.",
            "Temporarily set failurePolicy=Ignore to unblock the cluster while debugging.",
            "If the webhook is obsolete, delete the webhook configuration.",
        ],
        confidence=0.85,
        verified=False,
    ),

    # ── 30 ─ ErrImageNeverPull ────────────────────────────────────────────────
    FailurePattern(
        pattern_id="err_image_never_pull",
        type="ErrImageNeverPull",
        signals=[
            "ErrImageNeverPull", "imagePullPolicy", "Never",
            "image not present", "local image", "not found locally",
        ],
        root_cause="Pod imagePullPolicy is set to Never but the image is not present on the node's local image store.",
        recommended_checks=[
            "Describe the pod to confirm imagePullPolicy: Never.",
            "List images on the node (crictl images) to see if the image is cached.",
            "Verify the image was pre-loaded onto the node (e.g. via kind load or docker pull on the node).",
            "Check if the pod was rescheduled to a different node that lacks the image.",
        ],
        remediation_steps=[
            "Change imagePullPolicy to IfNotPresent or Always.",
            "Pre-load the image onto all relevant nodes before deployment.",
            "For Kind/Minikube, use kind load docker-image or minikube image load.",
        ],
        confidence=0.90,
        verified=True,
    ),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    dsn = _dsn()
    print(f"Connecting to: {dsn.replace(os.environ.get('POSTGRES_PASSWORD', 'password'), '***')}")

    with psycopg.connect(dsn) as conn:
        # Ensure table exists (idempotent)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failure_patterns (
                pattern_id          TEXT PRIMARY KEY,
                type                TEXT        NOT NULL,
                signals             TEXT[]      NOT NULL,
                root_cause          TEXT        NOT NULL,
                recommended_checks  TEXT[]      NOT NULL,
                remediation_steps   TEXT[]      NOT NULL,
                confidence          FLOAT       NOT NULL DEFAULT 0.8,
                times_seen          INTEGER     NOT NULL DEFAULT 0,
                last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                verified            BOOLEAN     NOT NULL DEFAULT FALSE,
                namespace_scope     TEXT,
                signals_text        TEXT GENERATED ALWAYS AS (
                                        array_to_string(signals, ' ')
                                    ) STORED
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals "
            "ON failure_patterns USING GIN (signals)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failure_patterns_signals_text "
            "ON failure_patterns USING GIN (to_tsvector('english', signals_text))"
        )

        inserted = 0
        skipped = 0
        for p in PATTERNS:
            result = conn.execute(
                """
                INSERT INTO failure_patterns
                    (pattern_id, type, signals, root_cause,
                     recommended_checks, remediation_steps,
                     confidence, times_seen, last_seen,
                     verified, namespace_scope)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (pattern_id) DO NOTHING
                """,
                (
                    p.pattern_id,
                    p.type,
                    p.signals,
                    p.root_cause,
                    p.recommended_checks,
                    p.remediation_steps,
                    p.confidence,
                    p.times_seen,
                    p.last_seen,
                    p.verified,
                    p.namespace_scope,
                ),
            )
            if result.rowcount:
                inserted += 1
                print(f"  [+] {p.pattern_id}")
            else:
                skipped += 1
                print(f"  [=] {p.pattern_id} (already exists, skipped)")

        conn.commit()

    print(f"\nDone. {inserted} inserted, {skipped} skipped (total {len(PATTERNS)} patterns).")


if __name__ == "__main__":
    main()
