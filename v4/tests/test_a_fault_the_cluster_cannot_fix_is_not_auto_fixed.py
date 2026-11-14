"""A fault the cluster cannot fix is investigated and reported, not handed to an unattended fix.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2): the Job faults in the A3 pilots fail
because the Job's own program exits 1. No change made from the cluster repairs that, yet the watchtower asked the
session to "apply the appropriate fix", the auto-approve prompt said "apply the fix immediately", and the playbook
ended "After fixing, recreate the job". The sessions that followed it looped 73 times or faked a fix with a stub Job.

A playbook may now declare `auto_fixable: false`; `JobBackoffLimitExceeded` does. The auto-approve prompt also says,
in so many words, that a fix must not substitute the workload to make a check pass.
"""

from __future__ import annotations

from app.agent.nodes import coordinator as coord
from app.agent.playbooks import get_playbook
from app.autonomy import watchtower as wt
from app.detectors.models import Finding


def test_the_job_backoff_playbook_is_not_auto_fixable():
    pb = get_playbook("JobBackoffLimitExceeded")
    assert pb is not None and pb.auto_fixable is False
    assert "recreate the job" not in pb.recommended_fix_template.lower()


def test_other_playbooks_keep_their_default():
    assert get_playbook("ImagePullBackOff").auto_fixable is True
    assert get_playbook("CreateContainerConfigError").auto_fixable is True


def test_the_watchtower_does_not_auto_fix_it_even_at_a3_on_the_allowlist(monkeypatch):
    monkeypatch.setattr(wt, "a3_allowed", lambda playbook, ns: True)
    job = Finding(
        playbook="JobBackoffLimitExceeded",
        cluster_id="c",
        namespace="h3-2",
        object_name="report",
        evidence="x",
    )
    img = Finding(
        playbook="ImagePullBackOff",
        cluster_id="c",
        namespace="b3-i1",
        object_name="web-1",
        evidence="x",
    )
    assert wt._should_auto_fix(job, "A3") is False
    assert wt._should_auto_fix(img, "A3") is True


def test_the_auto_approve_prompt_forbids_substituting_the_workload():
    block = coord._PROACTIVE_FIX_BLOCK.lower()
    assert "substitute" in block and "same work" in block
    assert "application" in block and "report it and stop" in block
