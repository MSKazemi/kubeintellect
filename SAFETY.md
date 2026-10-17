# Safety, limitations and pre-deployment checks — kubeintellect

**Scope:** Conversational diagnostics and proposed remediation of live Kubernetes resources using AI and operational tools.  
**Issued:** 2026-10-17  
**Status:** Risk guidance, **not** a safety certification, compliance approval, or verified representation that technical controls have been implemented.

This document is meant to help users and maintainers identify foreseeable risks and the checks **they still need to perform**. It supplements [DISCLAIMER.md](DISCLAIMER.md) and the existing software license; it does not override either or remove non-waivable duties.

## Material risks to assess

- An inaccurate model diagnosis may prompt an inappropriate scale, restart, delete, Helm or other cluster operation.
- Kubernetes context, RBAC configuration, tool aliases, credentials and approvals may differ between environments; a UI prompt is not proof of server-side authorization.
- Prometheus, Loki, cluster logs and prompts may include credentials, confidential workloads or personal data.

## Required assessment before consequential deployment

- [ ] Demonstrate with tests that every mutating path fails closed unless the right user and action are approved by the enforcement layer.
- [ ] Verify identity, namespace and cluster targeting, RBAC, least-privilege service accounts, scoped credentials and audit logs.
- [ ] Exercise denial, timeouts, retries, prompt injection, network loss and partial failures against a disposable test cluster.
- [ ] Validate recovery, backups, rollback and emergency disable procedures before any production use.

## Operational conditions

- Start with an isolated, disposable environment and the least privileges possible.
- Do not infer that a feature claim in project documentation proves that the safeguard is effectively enforced in every execution path. Perform threat modeling and independent testing.
- Independently validate material outcomes. Preserve audit evidence, tested version, configuration, dependencies, approver identity and incident response arrangements where appropriate.
- Before exposing a networked service, inspect authentication, authorization, logging, data handling, supply chain and applicable laws.
- Pause deployment when unresolved critical flaws, missing permissions, unexplained automation, or inaccurate claims are discovered. Route security reports privately rather than disclosing exploitable details in public issues.

**Important:** Project maintainers and users remain subject to applicable mandatory safety, privacy, security and product laws. A disclaimer does not substitute for these checks or guarantee immunity from claims.
