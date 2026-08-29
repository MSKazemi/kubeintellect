"""Scene + narration spec for the KubeIntellect narrated demo.

Single source of truth, in the shape proven by `nova/experiments/azure-2026-08-27/video/`:
the builder reads this to synthesise narration, render frames and assemble the video, and
`make_script_md.py` renders it as a readable script.

**Every factual claim below is checked against a file in this repository.** Terminal scenes
name the transcript they replay and the line numbers the narration refers to; card scenes
carry a `sources` list. Nothing on screen is typed by hand or reconstructed — the transcripts
in `../transcripts-kq/` are verbatim renderings of the asciinema casts in `../casts-kq/`,
recorded against a live cluster and independently re-verified on 2026-08-28 (see
`../DEMOS.md` § *Verification*).

kind:
  card      -- full-screen text card (title, bullets)
  terminal  -- animated replay of a real captured transcript in ../transcripts-kq/
  shot      -- a screenshot with a slow pan, under a caption bar

`enabled=False` marks a scene whose footage does not exist yet or is known to show something
false. It stays here, with its reason, so the gap is visible rather than forgotten.

Narration is written **phonetically** so Piper pronounces things correctly
(`kube control`, `A G P L three`). Subtitles are read rather than heard, so the
`SUBS` table below is applied before writing the .srt.
"""

# Colours are the website's own tokens, not a palette invented here. `render.py` carries the
# mapping and names the file each value comes from. Until 2026-08-28 the accent was `#7c8cf8`,
# commented "KubeIntellect indigo" — a colour that appears in no brand asset, no stylesheet and
# no mark; it came from the upstream build this one was adapted from.
ACCENT = "#00e07a"   # v4/docs/assets/brand/ki-c-green.svg — the mark gradient, mid-stop
TEAL = "#14b8a6"     # website app/globals.css --teal-soft (what the site uses on dark panels)
CORAL = "#e87d7d"    # no brand red exists; kept, and recorded here as unsourced
AMBER = "#f59e0b"    # website app/globals.css --amber-soft = "human-in-the-loop / approval gate"

WEBSITE = "kubeintellect.com"
REPO = "github.com/MSKazemi/kubeintellect"

# Written form for subtitles, keyed by the phonetic form used in narration.
SUBS = {
    "kube control": "kubectl",
    "kube Q": "kq",
    "A G P L three": "AGPL-3.0",
    "R B A C": "RBAC",
    "S R E": "SRE",
    "A I": "AI",
    "O O M killed": "OOMKilled",
    "sixty four mebibytes": "64Mi",
    "Exit code one three seven": "Exit code 137",
    "sixty four C P Us and five hundred and twelve gibibytes": "64 CPUs and 512Gi",
    "A P I key": "API key",
    "kubeintellect dot com": "kubeintellect.com",
}

SCENES = [
    # ------------------------------------------------------------------ ACT I
    dict(
        id="01-title", kind="card", act="", enabled=True,
        title="KubeIntellect",
        subtitle="Human-governed AI SRE for Kubernetes",
        bullets=[],
        links=[WEBSITE, REPO],
        caption="",
        logo=True,
        sources=["../../../README.md (tagline, links)"],
        narration=(
            "KubeIntellect. Human governed A I S R E for Kubernetes. "
            "Everything you are about to see is a real recording against a real cluster. "
            "No mock ups, no fixtures, no output typed by hand."
        ),
    ),
    dict(
        id="02-problem", kind="card", act="THE PROBLEM", enabled=True,
        title="A pod is failing.\nThe person who knows why\nis asleep.",
        subtitle="",
        bullets=[
            ("The signal is scattered", "pod status, events, logs, the deployment spec, a Secret"),
            ("The method is in someone's head", "the order you look in is the whole skill"),
            ("The cost of guessing is production", "the wrong fix is one command away"),
        ],
        caption="Why this is hard",
        sources=["../DEMOS.md § The scenarios"],
        narration=(
            "A pod is failing, and the person who knows why is asleep. "
            "The evidence is scattered across pod status, events, logs, the deployment spec "
            "and, often, a Secret that is not there. "
            "None of that is difficult. Knowing the order to look in is the whole skill, "
            "and it lives in somebody's head."
        ),
    ),
    dict(
        id="03-two-failures", kind="card", act="THE PROBLEM", enabled=True,
        title="Two ways this goes wrong",
        subtitle="",
        bullets=[
            ("It recites", "fluent, generic, and never actually looked at your cluster"),
            ("It acts", "confident, unasked, and now your cluster is different"),
        ],
        caption="The two failure modes of an AI cluster tool",
        sources=["../DEMOS.md § The approval gate, in both directions"],
        narration=(
            "Point a language model at this and it fails in one of two ways. "
            "Either it recites. Fluent, generic advice that never actually looked at your cluster. "
            "Or it acts. It runs the command it thought of, without asking, "
            "and now your cluster is different and nobody decided that."
        ),
    ),
    dict(
        id="04-answer", kind="card", act="THE ANSWER", enabled=True,
        title="Ask in English.\nIt reads the cluster.\nIt stops before it changes it.",
        subtitle="",
        bullets=[
            ("Investigate", "real kubectl against your real cluster — it reads before it writes"),
            ("Explain", "quoting the evidence it read, not a plausible story"),
            ("Ask", "a mutating command pauses at a gate a human answers"),
        ],
        caption="What KubeIntellect is",
        sources=["../../../README.md", "../DEMOS.md"],
        narration=(
            "KubeIntellect is the third thing. You ask in plain English. "
            "It investigates with real kube control against your real cluster, reading before it "
            "writes. "
            "It explains by quoting what it read. "
            "And the moment it wants to change anything, it stops and asks a human."
        ),
    ),

    # ----------------------------------------------------------------- ACT II
    dict(
        id="05-crashloop", kind="terminal", act="IT DIAGNOSES", enabled=True,
        source="01-crashloop.txt", lines=(59, 86),
        caption="A pod that will not stay up",
        evidence="transcripts-kq/01-crashloop.txt:71 (the command), :84-85 (the previous-container logs)",
        sources=["../DEMOS.md § Verification, row 01"],
        narration=(
            "Here is the first one, unedited. A pod that will not stay up. "
            "It does not guess from the pod name. It reads the log, "
            "and it quotes the line back: fatal, DATABASE underscore U R L is not set, "
            "refusing to start. Then it finds where that value was supposed to come from. "
            "A Secret called payments dash D B. "
            "That is the difference between diagnosing and describing."
        ),
    ),
    dict(
        id="06-oomkill", kind="terminal", act="IT DIAGNOSES", enabled=True,
        source="03-oomkill.txt", lines=(43, 64),
        caption="A worker the kernel keeps killing",
        evidence="transcripts-kq/03-oomkill.txt:59,62,63",
        sources=["../DEMOS.md § Verification, row 03"],
        narration=(
            "The same again, on a different kind of failure. "
            "A worker the kernel keeps killing. "
            "Limit, sixty four mebibytes. Reason, O O M killed. Exit code one three seven. "
            "Three facts, all read from the cluster, and the conclusion follows from them."
        ),
    ),
    dict(
        id="07-pending", kind="terminal", act="IT DIAGNOSES", enabled=True,
        source="05-pending-pod.txt", lines=(113, 133),
        caption="Capacity problem, or manifest problem?",
        evidence="transcripts-kq/05-pending-pod.txt:113-114",
        sources=["../DEMOS.md § Verification, row 05"],
        narration=(
            "This one is the question an operator actually asks. "
            "A pod that will never schedule. Is this a cluster capacity problem, "
            "or a manifest problem? "
            "The honest answer is the second one. A pod asking for sixty four C P Us "
            "and five hundred and twelve gibibytes of memory is not a cluster that is too small. "
            "It is a manifest that is wrong."
        ),
    ),
    dict(
        id="08-triage", kind="terminal", act="IT DIAGNOSES", enabled=True,
        source="08-complex-triage.txt", lines=(134, 155),
        caption="One namespace, six workloads, five faults, one healthy control",
        evidence="transcripts-kq/08-complex-triage.txt:89 (found), :135 + :143 (ranked, on screen)",
        sources=["../DEMOS.md § Verification, row 08"],
        narration=(
            "Now the whole namespace at once. Six workloads. Five of them broken, "
            "one deliberately healthy, and the healthy one must not be named. "
            "It finds all five, including the one with no pod level symptom at all, "
            "and here it is ranking them by user impact rather than listing them. "
            "The silent Service, the one with no failing pod, comes second."
        ),
    ),

    # ---------------------------------------------------------------- ACT III
    dict(
        id="09-gate-card", kind="card", act="ATTACK IT", enabled=True,
        title="A gate that is only ever approved\nhas not been shown to gate anything",
        subtitle="",
        bullets=[
            ("Both directions were recorded", "approved, and denied"),
            ("Checked against the cluster", "not against the transcript"),
            ("A real prompt, not a sentence", "the recording checks for the HITL prompt"),
        ],
        caption="How the gate was tested",
        sources=["../DEMOS.md § The approval gate, in both directions"],
        narration=(
            "Everything so far was read only. This is the part that matters. "
            "A gate that is only ever approved has not been shown to gate anything, "
            "so both directions were recorded, and both were checked against the cluster "
            "afterwards rather than against the transcript."
        ),
    ),
    dict(
        id="10-deny", kind="terminal", act="ATTACK IT", enabled=True,
        source="07-approval-denied.txt", lines=(65, 95),
        caption="Denied — and then proved denied, in the same session",
        evidence="transcripts-kq/07-approval-denied.txt:84-87,91-93",
        sources=["../DEMOS.md § Verification, row 07"],
        narration=(
            "Scale the web deployment to ten replicas. It stops. A human types deny. "
            "Watch the next line carefully, because it looks wrong. "
            "The client echoes the command it was asked to run, and then says: cancelled. "
            "The proof is the question after it. "
            "How many replicas does web have now? Two. "
            "The refusal is verified inside the session, against the cluster."
        ),
    ),
    dict(
        id="11-approve", kind="terminal", act="ATTACK IT", enabled=True,
        # (108, 140) ended on "with the same error:" — a colon. The narration calls this
        # "the most valuable thirty seconds ... the one a demo normally cuts", and the window
        # cut it: the evidence for *why* the restart failed (DATABASE_URL not set, exit code
        # 1) is on :143-:145 and never reached the screen. Extended to :145, and the
        # narration lengthened to match so the reveal rate stays readable.
        source="06-approval-gate.txt", lines=(108, 145),
        caption="Approved — and it did not fix the fault",
        evidence="transcripts-kq/06-approval-gate.txt:112 (gate), :127 (approve), :135 + :138 (it did not fix it)",
        sources=["../DEMOS.md § Verification, row 06", "../casts-kq/06-approval-gate.gates.jsonl"],
        narration=(
            "The other direction. Medium risk. A human types approve. "
            "The restart runs, and it works. "
            "And then the follow up question: did the restart change anything? "
            "No. The pods fail with the same error, because a restart was never going to "
            "supply a missing environment variable. "
            "It says so. That is the most valuable thirty seconds in this video, "
            "and it is the one a demo normally cuts. "
            "So watch what it actually returns, rather than taking my word for it. "
            "It re-reads the pods. It re-reads the events. "
            "And it names the same root cause it found at the very beginning: "
            "the DATABASE underscore U R L environment variable is not set, "
            "so the container exits with code one. "
            "The restart was a reasonable thing to try and the wrong thing to fix it — "
            "and the system is the one telling you that, not me."
        ),
    ),

    # ----------------------------------------------------------------- ACT IV
    dict(
        id="12-how", kind="card", act="HOW IT WORKS", enabled=True,
        title="How it works",
        subtitle="",
        bullets=[
            ("A coordinator, and subagents", "plan, fetch in parallel, then conclude"),
            ("A cluster snapshot first", "a healthy cluster biases the answer toward the snapshot"),
            ("The gate is at the tool boundary", "not in the prompt, and not advisory"),
            ("A role per API key", "read only, operator, admin, superadmin — once keys are set"),
            ("A hash chained decision log", "every step replayable after the fact"),
        ],
        caption="The parts that matter",
        sources=[
            "../../../v4/docs/how-it-works.md § Reading the diagram",
            "../../../v4/docs/security.md § 2. Role capabilities",
            "app/db/flight_recorder.py",
        ],
        narration=(
            "Briefly, how. A coordinator plans, subagents fetch in parallel, and only then "
            "does it conclude. A cluster snapshot is taken first, which biases a healthy cluster "
            "toward answering from the snapshot rather than fanning out. "
            "The approval gate sits at the tool boundary, not in the prompt, "
            "which is why it cannot be talked around. "
            "Once you configure keys, each one carries a role: read only, operator, admin or "
            "superadmin. "
            "And every decision is written to a hash chained log, so a run can be replayed "
            "after the fact rather than remembered."
        ),
    ),
    dict(
        id="12a-flow", kind="flow", act="HOW IT WORKS", enabled=True,
        title="How a signal becomes an action",
        subtitle="The stage names are the modules",
        caption="Perception is free; the model is the expensive part, so it is entered last",
        sources=[
            "../../../v4/docs/how-it-works.md",
            "../../../v4/docs/security.md § 2. Role capabilities",
        ],
        narration=(
            "Here is the whole pipeline. Signals arrive from the cluster — kube control "
            "watches, Prometheus queries, log queries — and the sensorium normalises each "
            "one into a single observation shape. "
            "Detectors are compiled predicates. They are always on, they run on every "
            "observation, and they cost zero tokens, because no model is involved. That is the "
            "point of the design: watching your cluster is free. "
            "Only when a detector actually fires is the L L M invoked at all, and only then does "
            "an investigation correlate the evidence into a root cause. "
            "If that investigation wants to change something, it does not simply change it. The "
            "proposal goes to the autonomy ladder, A zero through A three, and then through one "
            "write chokepoint — decide underscore write — which returns exactly three "
            "answers: do it, ask a human, or refuse. "
            "Every one of those outcomes is appended to a hash chained flight recorder, so the "
            "run can be replayed afterwards instead of remembered."
        ),
    ),
    dict(
        # `source` is a directory, so this is a clip rather than a still: ffmpeg decoded
        # ../chat-ui/chat-ui-crashloop.mp4 into shots-dark/chatui/ once, and render.py walks
        # that sequence with the scene clock. The whole recording plays, retimed onto the
        # narration rather than truncated — which is why the caption says so out loud.
        id="13-chat-ui", kind="shot", act="HOW IT WORKS", enabled=True,
        source="chatui",
        url="127.0.0.1:7861",
        speed=1.74,
        caption="The same server, in the browser — replayed faster than real time",
        sources=["../DEMOS.md", "../chat-ui/chat-ui-crashloop.json"],
        narration=(
            # Not "same gate": that recording is made with a read-only key, so what it shows
            # is an RBAC refusal, not the approval gate scenes 09 to 11 show.
            "Not everyone lives in a terminal, so the same server answers in a browser. This is "
            "a real recording, replayed here faster than it happened. "
            "The first question is the crash loop you already saw, and it settled in fifteen "
            "point three seconds — measured from pressing enter, not timed off this video. "
            "Then watch what happens on the second one. The user asks it to scale a deployment "
            "to three replicas. This session is holding a read only key, so the write is refused "
            "at the role boundary — before it is ever proposed, let alone run. "
            "Same evidence, same server, a different door."
        ),
    ),
    dict(
        # Captured live over ssh while this video was being built. Every command is a read,
        # and `term_title` says which cluster this is: labelling this footage with the local
        # demo cluster's prompt would be a false caption on true footage. It deliberately does
        # NOT name the login or the host IP — this repo and the video are both public, and
        # publishing the SSH account of a live production box buys nothing a viewer needs.
        id="13b-azure", kind="terminal", act="HOW IT WORKS", enabled=True,
        source="10-azure-live.txt", lines=(1, 17),
        term_title="kubectl  ·  production cluster in Azure  ·  read-only",
        caption="A real cluster in Azure — captured live while this video was built",
        sources=[
            "../transcripts-kq/10-azure-live.txt",
            "https://api.kubeintellect.com/healthz",
        ],
        narration=(
            "None of this is a laptop demo. This is a two node cluster running in Azure, and it "
            "has been up for one hundred and twenty five days. The server, its Postgres, and the "
            "ingress in front of them have been running for four months without attention. "
            "That endpoint, api dot kubeintellect dot com, is what the public demo talks to, and "
            "it answers health Z right now. "
            "One honest note while it is on screen: it reports version two point zero. That "
            "deployment has not been updated since April. The code you have been watching is "
            "newer than the box serving that URL."
        ),
    ),
    dict(
        id="14-install", kind="terminal", act="HOW IT WORKS", enabled=False,
        blocked_on=(
            "T2b — the cast installs 2.2.0 from PyPI, and on 2.2.0 the demo's own pre-flight "
            "`kubeintellect --version` exits 2. Re-measured 2026-08-29 against the PUBLISHED "
            "artifacts: the release gate is now CLEAR — a clean-venv `pip install "
            "kubeintellect==2.4.1` gives `--version` exit 0 and a server whose `/healthz` "
            "returns 200 with version 2.4.1, no ModuleNotFoundError. One gate remains: the "
            "cast itself (`09-install.cast`) still installs 2.2.0, so it must be re-recorded "
            "before this scene may be used."
        ),
        source="09-install.txt", lines=(1, 60),
        caption="Install it",
        sources=["../DEMOS.md", "../transcripts-kq/09-install.txt"],
        narration=(
            "Installing it is one command, and a wizard that asks six questions."
        ),
    ),

    # ------------------------------------------------------------------ ACT V
    dict(
        id="15-limits", kind="card", act="HONEST LIMITS", enabled=True,
        title="What it did not do",
        subtitle="",
        bullets=[
            ("It inferred one root cause", "right answer, reached without reading the value"),
            ("Found is not solved", "it ranked the silent Service, then filed it as a next step"),
            ("No metrics backend, and it said so", "an invented number is the worst failure there is"),
            ("The cold gate varies by deployment", "which is why the recording checks for the prompt"),
        ],
        caption="The most useful section of the docs",
        sources=["../DEMOS.md § What it did not do"],
        narration=(
            "What it did not do. In one run it reached the right root cause "
            "without ever reading the value it was diagnosing. Correct, but not confident. "
            "In another it found the silent Service and then stopped at a next step, "
            "rather than solving it. Found is not solved. "
            "This cluster ships no metrics backend, and when asked a metrics question "
            "it reported the backend unavailable instead of inventing a number, "
            "which is the failure that would matter most. "
            "All of that is written down, in the same page as the demos."
        ),
    ),

    # ----------------------------------------------------------------- ACT VI
    dict(
        id="16-close", kind="card", act="WHO IT IS FOR", enabled=True,
        title="Self-hosted. Your cluster,\nyour keys, your gate.",
        subtitle="",
        bullets=[
            ("AGPL-3.0, self-hosted", "it runs where your cluster runs"),
            ("Published", "an earlier version is described in a peer reviewed paper"),
            ("Nothing changes without you", "every mutating command stops at a gate you answer"),
        ],
        links=[WEBSITE, REPO],
        caption="",
        logo=True,
        sources=["../../../README.md (licence badge, DOI badge)"],
        narration=(
            "KubeIntellect is A G P L three and self hosted. It runs where your cluster runs, "
            "with your keys, and it does not change anything without a human answering a gate. "
            "The recordings in this video, and the page listing everything they did not do, "
            "are both in the repository. "
            "It is all at kubeintellect dot com — the repository link is on screen."
        ),
    ),
]
