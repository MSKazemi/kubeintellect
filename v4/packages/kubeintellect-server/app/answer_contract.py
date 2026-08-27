"""What an answer is not allowed to claim — the clause shared by every tier that writes one.

Measured on 2026-08-26, campaign lane `e2-baselines/r1`, scenario `06-oomkilled`: the query
said "the pod keeps restarting", the pod was `1/1 Running` with **0 restarts**, and the answer
opened "The pod `memory-hog` … is restarting due to memory allocation exceeding its configured
limit". Every piece of evidence it had gathered contradicted that sentence — it quoted the
container log showing the 100 MB `dd` running to completion (`104857600 bytes … copied`, then
`Done`), and it reported the Prometheus peak as **4.57Mi against a 64Mi limit** — and it
resolved the contradiction *in favour of the question*, hedging with "suggests it would exceed
the limit during execution" and "likely causing it to be terminated". HolmesGPT did the same on
the same scenario. Two independent agents, given evidence that refuted the premise, confirmed
the premise.

That is not a retrieval failure and no extra tool call fixes it: the observation was already in
context. It is an answer-contract failure, so the contract is where it is repaired — in one
place, because the coordinator and the cortex synthesis tier are a package apart and both write
final answers to an operator.

The clause is deliberately two-sided. An agent that has been told "the user may be wrong" and
nothing else buys its confirmation bias back as a doubt bias, and manufacturing a contradiction
against a fault that *is* there is the same defect with the sign flipped — see the grader this
lane also had to fix, which marked arms down for correctly reporting a healthy pod.
"""

# Handed to every tier that composes a final answer. One copy: a route that gets the rule
# without the counterweight would trade unfounded confirmation for unfounded doubt.
PREMISE_CLAUSE = """IMPORTANT — The question is a claim, not an observation:
  A request usually asserts a fault ("the pod keeps restarting", "the deployment is down",
  "it got OOMKilled"). That assertion is the operator's HYPOTHESIS. Confirming it is not the
  goal; establishing what is actually true is.

  Before you report the asserted fault as the root cause, check it against your own evidence:
    - "keeps restarting" / "crashlooping" → the RESTARTS column, and `Last State` in describe.
    - "OOMKilled" / "exit 137"           → `Last State: Terminated, Reason: OOMKilled`.
    - "is down" / "crashing"             → pod phase and readiness, not the log text alone.
    - a resource claim ("out of memory")  → the measured value against the limit, not the
                                            command that was supposed to breach it.

  If your evidence does not show it, say so in the FIRST sentence, then report what you did
  find. For example:
  "> ⚠️ I could not reproduce the reported symptom: `memory-hog` is Running with 0 restarts and
   no OOMKilled termination. Here is the state I actually observed…"
  Then give the explanations that would fit (already recovered, a different namespace or
  cluster, a different workload) and say what would confirm one.

  Never write a root cause your own evidence contradicts, and never soften a contradiction into
  "likely", "suggests it would", or "would have". If a metric, a log line, or a restart count
  disagrees with the premise, THE DISAGREEMENT IS THE FINDING — report it as the answer, not as
  a caveat on the answer.

  Equally: do not manufacture doubt. When the evidence does show the reported symptom, confirm
  it plainly and get on with the diagnosis. This rule exists to stop unfounded confirmation,
  not to make you second-guess a fault you can see."""


# The words the clause forbids as a bridge from "my evidence says no" to "the premise holds
# anyway". Named here so a test can assert the clause still forbids them, and so any future
# checker keys on the same list the prompt does rather than a second, drifting copy.
HEDGE_WORDS = ("likely", "suggests it would", "would have")
