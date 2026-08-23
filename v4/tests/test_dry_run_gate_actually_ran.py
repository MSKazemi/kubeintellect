"""A pre-apply validation gate must not report "would apply cleanly" when it never ran.

`validate_mutation` is the third link of the chokepoint chain — validate → apply → verify — and it
had the same defect as the other two: it read `run_kubectl`'s prose with substring tests
(`admission = any(m in low for m in _ADMISSION_MARKERS)`, `ok = not admission and "error" not in
low`). Measured 2026-08-20 by driving the **real `run_kubectl`**: every safety gate in the project
refuses with a marker string that contains none of those words, so all five produced
`DryRunResult(ok=True, admission_denied=False)` — "the API server validated this" about a command
the API server never saw.

The flag handling had the mirror-image hole. `_with_server_dry_run` skipped appending
`--dry-run=server` whenever the *substring* `--dry-run` appeared, so three real spellings turned
the server-side validation off while the result still claimed to be one:

  --dry-run=none      real kubectl (v1.36.3): "--dry-run='none': Must be \"none\", \"server\", or
                      \"client\"" — none is the default, i.e. NOT a dry run
  --dry-run           real kubectl: "--dry-run is deprecated and can be replaced with
                      --dry-run=client" — client-side, so admission is never consulted
  --dry-run=client    same reason

and `kubectl label deploy/web team=--dry-run` tripped it from inside a label *value*.

Note for the reader: the production HITL gate in `kubectl_tool.py` does the same test over
`args`, which is a `list[str]`, so there it is exact token membership and `--dry-run=none`
correctly still requires approval. The defect is confined to this module's string test.
"""
from __future__ import annotations

import pytest

from app.tools.aci.mutating import (
    DryRunResult,
    _with_server_dry_run,
    plan_mutation,
    validate_mutation,
)
from app.tools.kubectl_tool import run_kubectl

REFUSED_BY_A_REAL_GATE = [
    ("readonly key, a write", "delete deployment web -n prod", "readonly"),
    ("operator key, high-risk verb", "delete namespace prod", "operator"),
    ("admin, protected namespace", "patch deployment api -n kube-system -p '{}'", "admin"),
    ("admin, cluster-wide", "delete pods --all-namespaces", "admin"),
    ("admin, terminal verb", "edit deployment web -n prod", "admin"),
]

# Verbatim kubectl (bitnami/kubectl:latest v1.36.3, 2026-08-20).
UNREACHABLE = ("The connection to the server localhost:8080 was refused - "
               "did you specify the right host or port?")
# kubectl's documented output shape for a successful server-side dry-run.
SERVER_DRY_RUN_OK = "deployment.apps/web configured (server dry run)"
ADMISSION_DENIED = ('Error from server (Forbidden): admission webhook "policy.kyverno.io" '
                    "denied the request: require-limits")


def _refusal(command: str, role: str) -> str:
    return run_kubectl.invoke({"command": command},
                              config={"configurable": {"user_role": role, "hitl_bypass": True}})


class TestARefusedDryRunIsNotAPass:
    @pytest.mark.parametrize("label,command,role", REFUSED_BY_A_REAL_GATE,
                             ids=[c[0] for c in REFUSED_BY_A_REAL_GATE])
    def test_the_gate_reports_that_it_never_ran(self, label, command, role):
        out = _refusal(_with_server_dry_run(command), role)
        r = validate_mutation(command, _runner=lambda c, _o=out: _o)
        assert r.validated is False, f"{label}: {out.splitlines()[0]}"
        assert r.ok is False
        # and it does not invent an admission verdict it never received
        assert r.admission_denied is False
        assert "not validated" in r.output

    def test_an_unreachable_cluster_is_not_a_pass(self):
        r = validate_mutation("kubectl apply -f -", _runner=lambda c: UNREACHABLE)
        assert (r.validated, r.ok) == (False, False)

    def test_no_output_at_all_is_not_a_pass(self):
        r = validate_mutation("kubectl apply -f -", _runner=lambda c: "(no output)")
        assert (r.validated, r.ok) == (False, False)

    def test_a_raised_error_is_not_a_pass(self):
        def boom(cmd):
            raise RuntimeError("no cluster")
        r = validate_mutation("kubectl apply -f -", _runner=boom)
        assert (r.validated, r.ok) == (False, False)


class TestARealServerAnswerIsStillRead:
    def test_a_clean_server_dry_run_passes(self):
        r = validate_mutation("kubectl apply -f -", _runner=lambda c: SERVER_DRY_RUN_OK)
        assert (r.validated, r.ok, r.admission_denied) == (True, True, False)

    def test_an_admission_denial_is_a_real_rejection_not_a_blind_spot(self):
        r = validate_mutation("kubectl apply -f -", _runner=lambda c: ADMISSION_DENIED)
        assert r.validated is True, "the server answered — that is a validation result"
        assert r.ok is False and r.admission_denied is True

    def test_the_default_result_is_validated_so_existing_callers_are_unaffected(self):
        assert DryRunResult(True, False, "ok").validated is True


class TestTheServerDryRunFlagIsActuallyForced:
    @pytest.mark.parametrize("command", [
        "kubectl apply -f -",
        "kubectl apply -f - --dry-run=none",      # real kubectl: "none" is NOT a dry run
        "kubectl apply -f - --dry-run=client",    # client-side: admission never consulted
        "kubectl apply -f - --dry-run",           # real kubectl: deprecated alias for client
        "kubectl apply -f - --dry-run client",    # pflag's space form
    ])
    def test_every_spelling_ends_up_server_side(self, command):
        result = _with_server_dry_run(command)
        assert result.endswith("--dry-run=server")
        assert result.count("--dry-run") == 1, result

    def test_an_already_server_side_command_is_not_doubled(self):
        assert (_with_server_dry_run("kubectl apply -f - --dry-run=server")
                == "kubectl apply -f - --dry-run=server")

    def test_the_string_inside_a_value_does_not_count_as_a_flag(self):
        # `team=--dry-run` is a label value, not a dry-run flag.
        result = _with_server_dry_run("kubectl label deploy/web team=--dry-run")
        assert result == "kubectl label deploy/web team=--dry-run --dry-run=server"

    def test_the_rest_of_the_command_survives(self):
        result = _with_server_dry_run("kubectl patch deploy/web -n prod --dry-run=client -p {}")
        assert result == "kubectl patch deploy/web -n prod -p {} --dry-run=server"


class TestAnUnrunCheckNeverEarnsAutoExecution:
    def test_auto_is_downgraded_to_approve_when_the_dry_run_never_ran(self):
        refusal = _refusal(_with_server_dry_run("kubectl scale deploy/web --replicas=3 -n prod"),
                           "readonly")
        proposal, dry_run = plan_mutation(
            "kubectl scale deploy/web --replicas=3 -n prod",
            earned_rung="L4", _runner=lambda c: refusal,
        )
        assert dry_run is not None and dry_run.validated is False
        assert proposal.decision == "approve"
        assert "never ran" in proposal.reason

    def test_auto_survives_a_real_clean_dry_run(self):
        proposal, dry_run = plan_mutation(
            "kubectl scale deploy/web --replicas=3 -n prod",
            earned_rung="L4", _runner=lambda c: SERVER_DRY_RUN_OK,
        )
        assert dry_run is not None and dry_run.validated is True
        assert proposal.decision == "auto"
