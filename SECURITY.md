# Security Policy

KubeIntellect operates on live Kubernetes clusters and executes LLM-orchestrated actions, so we
take security seriously. Thank you for helping keep KubeIntellect and its users safe.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues, Discussions, or pull
requests.**

Instead, report them privately through one of:

- **GitHub Security Advisories** — [open a private report](https://github.com/MSKazemi/kubeintellect/security/advisories/new)
  (preferred; keeps disclosure coordinated).
- **Email** — **mohsen.seyedkazemi@gmail.com** with the subject line `SECURITY: <short summary>`.

Please include, as far as you can:

- The version / commit and which generation (`v4/`, `v1/`, …) is affected.
- A description of the vulnerability and its impact.
- Steps to reproduce (a minimal PoC helps enormously).
- Any suggested mitigation.

We aim to acknowledge reports within **72 hours** and to provide a remediation timeline after triage.
We will keep you informed through the process and credit you in the advisory (unless you prefer to
remain anonymous).

## Scope

Security-relevant areas we especially care about:

- **The human-in-the-loop (HITL) gate** — any path that lets a mutating cluster action execute
  *without* explicit human approval and RBAC is a critical bug.
- **Indirect prompt injection** — cluster-derived text (logs, events, ConfigMaps, user YAML) being
  treated as instructions rather than untrusted data.
- **Secret leakage** — Secret values appearing in logs, traces, prompts, or tool output (tools must
  return key names only, never values).
- **Privilege escalation** — a tool obtaining Kubernetes permissions beyond least-privilege.
- **Sandbox escape** — for any generated/dynamic code execution path.

## Out of scope

- Vulnerabilities in third-party dependencies should generally be reported upstream (tell us too if
  KubeIntellect's usage makes them exploitable).
- Findings that require an already-compromised cluster-admin or host.
- Denial of service from unbounded LLM cost without a concrete exploit (we track cost controls as
  regular issues).

## Supported versions

Active security fixes target the **current version (`v4/`)**. Earlier generations (`v1/`–`v3/`) are
maintained for reference and receive fixes on a best-effort basis.

| Version | Supported |
|---|---|
| `v4/` (current) | ✅ |
| `v1/`–`v3/` | 🔸 Best-effort |

## Disclosure

We follow **coordinated disclosure**: we ask that you give us a reasonable window to ship a fix
before any public disclosure, and we will do the same in keeping you updated. We will publish a
security advisory once a fix is available.
