"""The premise clause has to reach every tier that writes an answer — and stay two-sided.

Campaign lane `e2-baselines/r1`, scenario `06-oomkilled`, 2026-08-26: two independent agents
were handed evidence that refuted the query's premise (0 restarts, a container log showing the
allocation completing, a 4.57Mi peak against a 64Mi limit) and both reported the premise as the
root cause anyway. The repair is a prompt clause, so what a test can honestly prove is narrow
and worth stating plainly: that the clause exists, that it is in the prompt each answering tier
actually sends, that it names the checks and the hedges by the same words in both places, and
that it still carries the counterweight. It cannot prove a model obeys it — only a re-run on a
scenario whose premise is false can do that (`27-false-premise-restarts`).

That narrowness is the point. The truncation instruction in this same file spent months naming
a marker no trimmer emitted, and the fix was not a better instruction — it was a test that made
the instruction and its trigger share one source. Same shape here.
"""

import re
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.answer_contract import HEDGE_WORDS, PREMISE_CLAUSE  # noqa: E402

#: The clause is hand-wrapped prose, so a phrase that spans a line break contains a newline and
#: two spaces of indent. Asserting on the wrapped form would make every reflow a test failure and
#: teach the next reader to reflow the test instead of reading it.
FLAT = " ".join(PREMISE_CLAUSE.split())


@pytest.fixture(scope="module")
def coordinator_system():
    from app.agent.nodes.coordinator import _COORDINATOR_SYSTEM
    return _COORDINATOR_SYSTEM


@pytest.fixture(scope="module")
def synthesis_system():
    from app.cortex.graph import _SYNTHESIS_SYSTEM
    return _SYNTHESIS_SYSTEM


# ── the clause reaches the tiers that write answers ──────────────────────────

def test_the_coordinator_prompt_carries_the_clause(coordinator_system):
    assert PREMISE_CLAUSE in coordinator_system


def test_the_cortex_synthesis_prompt_carries_the_clause(synthesis_system):
    """The other route to an operator-visible answer. `06-oomkilled` ran on one of the two;
    a clause on only one of them is a fix that half the traffic never sees."""
    assert PREMISE_CLAUSE in synthesis_system


def test_the_coordinator_template_placeholder_is_actually_substituted(coordinator_system):
    """The clause is spliced into an r-string by `.replace`. If that call is ever dropped the
    prompt still builds, still ships, and reads as if the rule were there."""
    assert "{premise_clause}" not in coordinator_system


def test_both_tiers_get_the_same_text(coordinator_system, synthesis_system):
    """One constant, not two paraphrases. Two copies of a behavioural rule drift, and the
    drift is invisible from either side."""
    for tier in (coordinator_system, synthesis_system):
        assert tier.count(PREMISE_CLAUSE) == 1


# ── what the clause must say ─────────────────────────────────────────────────

def test_the_clause_names_the_check_for_each_symptom_it_lists():
    """A rule that says "verify the premise" without naming the field to read is advice, not an
    instruction. Each of these is the observation that refutes the matching claim."""
    for observation in ("RESTARTS", "Last State", "Reason: OOMKilled", "readiness"):
        assert observation in FLAT, observation


def test_the_clause_forbids_the_exact_hedges_the_lane_recorded():
    """`likely causing it to be terminated` and `suggests it would exceed the limit` are the
    two bridges the recorded answer used to get from refuting evidence to the premise. They are
    quoted here from the record, not invented."""
    for hedge in HEDGE_WORDS:
        assert f'"{hedge}"' in FLAT, hedge


def test_the_hedge_list_is_not_a_second_copy_of_itself():
    """`HEDGE_WORDS` exists so a future checker keys on the same list the prompt does. If the
    prompt ever stops quoting one of them, this is where it shows."""
    assert HEDGE_WORDS, "an empty forbidden-hedge list forbids nothing"
    assert len(set(HEDGE_WORDS)) == len(HEDGE_WORDS)


def test_the_contradiction_is_named_as_the_finding_not_as_a_caveat():
    assert "THE DISAGREEMENT IS THE FINDING" in FLAT


def test_the_clause_says_where_the_correction_goes():
    """Buried on line 40 of a report, a "could not reproduce" is not a correction. The lane's
    answer did open with its (wrong) conclusion, which is exactly the slot this must occupy."""
    assert "in the FIRST sentence" in FLAT


def test_the_clause_asks_for_the_alternatives_rather_than_stopping_at_the_refusal():
    """"I could not reproduce it" alone is not an answer to an operator holding an incident."""
    assert "already recovered" in FLAT


# ── the counterweight: it must not become a doubt bias ───────────────────────

def test_the_clause_still_tells_the_agent_to_confirm_a_fault_it_can_see():
    """The failure mode of this fix is the same defect with the sign flipped — an agent that
    disputes a real fault. The grader in this same lane had to be repaired for exactly that,
    in the opposite direction."""
    assert "do not manufacture doubt" in FLAT
    assert "confirm it plainly and get on with the diagnosis" in FLAT


def test_the_clause_does_not_tell_the_agent_the_user_is_usually_wrong():
    """No blanket statement about operator reliability: the rule is about evidence, and a prior
    over the operator would be a bias, not a check."""
    bad = re.compile(r"(operator|user)s? (are|is) (often|usually|frequently) (wrong|mistaken)",
                     re.IGNORECASE)
    assert not bad.search(FLAT)


# ── what this test does NOT establish ────────────────────────────────────────

def test_the_module_docstring_says_the_measurement_this_rests_on():
    """The clause is justified by one recorded lane. If the citation is ever removed, the rule
    becomes an opinion, and the next reader has no way to check it."""
    import app.answer_contract as ac
    doc = ac.__doc__ or ""
    assert "06-oomkilled" in doc
    assert "0 restarts" in doc
    assert "HolmesGPT did the same" in doc, "one arm doing it is an anecdote; two is a pattern"
