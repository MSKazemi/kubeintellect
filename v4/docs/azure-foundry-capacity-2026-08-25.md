# Azure Foundry Capacity Change — 2026-08-25

## Purpose

This record captures the Azure OpenAI/Foundry rate-limit audit and the resulting deployment-capacity changes. The optimization target was throughput and avoidance of HTTP 429 responses; latency and priority processing were explicitly not priorities. No credentials or endpoint keys are recorded here.

## Evidence Before the Change

Azure Monitor was queried for 2026-07-26 through 2026-08-25. The only observed throttling was on `kubeintellect/gpt-4o-mini`: 347 successful requests and 65 HTTP 429 responses. All 65 throttles occurred during the 2026-08-24 11:00 UTC burst. During the surrounding two-hour window, its peak processed prompt volume was 506,726 tokens/minute, while the deployment limit was only 250,000 TPM. `kubeintellect/gpt-4o` peaked at 306,755 prompt tokens/minute with the same 250,000 TPM allocation but recorded no 429s.

All deployments were healthy (`Succeeded`), used the `GlobalStandard` SKU, and had `OnceNewDefaultVersionAvailable` configured.

## Capacity Changes

Capacity values below are thousands of tokens per minute (K TPM).

| Foundry resource | Deployment | Before | Final | Final request limit |
|---|---|---:|---:|---:|
| `kubeintellect` | `gpt-4o` | 250 | 45,000 | 45,000 / 10 s |
| `kubeintellect` | `gpt-4o-mini` | 250 | 225,000 | 2,250,000 / min |
| `mohsenhermes` | `gpt-5.4` | 250 | 15,000 | 150,000 / min |
| `mohsenhermes` | `gpt-5.4-mini` | 250 | 15,000 | 15,000 / min |
| `mohsenhermes` | `gpt-5.4-nano` | 250 | 225,000 | 225,000 / min |
| `mohsenhermes` | `gpt-5.3-codex` | 250 | 45,000 | 450,000 / min |
| `mohsenhermes` | `gpt-5.3-chat` | 250 | 5,250 | 52,500 / min |
| `examlops` | `gpt-5-mini` | 5,000 | 10,000 | 10,000 / min |
| `examlops` | `gpt-5.5` | 5,000 | 10,000 | 10,000 / min |
| `examlops` | `gpt-5.4-mini` | 4,875 | 15,000 | 15,000 / min |
| `mohsen-coding` | `gpt-5.3-codex` | 44,875 | 45,000 | 450,000 / min |
| `mohsen-coding` | `gpt-5.6-sol` | 5,000 | 10,000 | 10,000 / min |
| `kubeintellectv2-resource` | `gpt-5.4` | 5,000 | 15,000 | 150,000 / min |
| `kubeintellectv2-resource` | `gpt-5.4-nano` | 75,000 | **75,000** | 75,000 / min |
| `kubeintellectv2-resource` | `gpt-4o` | 15,000 | 45,000 | 45,000 / 10 s |
| `kubeintellectv2-resource` | `gpt-4o-mini` | 75,000 | 225,000 | 2,250,000 / min |

The final allocations consume all reported `GlobalStandard` subscription quota for every deployed model family except GPT-5.4 Nano. GPT-5.4 Nano uses 300M of 450M TPM across its two deployments.

## Exceptions and Follow-up

Azure rejected attempts to raise `kubeintellectv2-resource/gpt-5.4-nano` from 75M to 225M TPM with control-plane error `715-123420` (“unusual activity”). Two retries, including a smaller 150M target, were stopped after the same response. Retry later or open an Azure support request if that deployment needs the remaining 150M TPM.

Dynamic quota was attempted for the original KubeIntellect deployments, but Azure rejected it as unsupported for `GlobalStandard`. The SKU was retained because maximum rate limit—not regional data residency or fixed-latency guarantees—is the objective. Priority processing and provisioned throughput were not enabled.

Increasing TPM allocation has no fixed PTU reservation charge, but it raises the ceiling for billable token consumption. Continue monitoring `AzureOpenAIRequests` split by `StatusCode`, especially 429, and rebalance quota before creating additional deployments.
