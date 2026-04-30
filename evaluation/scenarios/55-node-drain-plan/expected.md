# Scenario 55 — Expected Behavior

## Ground Truth
- **Task:** Provide safe node drain procedure (plan only, no execution)
- **Success:** Correct sequence: cordon → drain (with flags) → upgrade → uncordon

## Expected Agent Path
1. Explains cordon: kubectl cordon kind-worker (prevents new pods)
2. Explains drain: kubectl drain kind-worker --ignore-daemonsets --delete-emptydir-data
3. Mentions PodDisruptionBudget awareness (--disable-eviction flag if PDB blocks)
4. Node upgrade step (external, not kubectl)
5. Uncordon: kubectl uncordon kind-worker

## Minimum Passing Criteria
- [ ] Includes kubectl cordon
- [ ] Includes kubectl drain with --ignore-daemonsets
- [ ] Mentions PodDisruptionBudget or PDB awareness
- [ ] Includes kubectl uncordon at the end
- [ ] Does NOT execute any commands (plan only as requested)

## Critical Failures
- Executes commands instead of showing the plan
- Omits uncordon step
- Omits --ignore-daemonsets (drain fails otherwise)
- Does not mention PDB implications
