#!/usr/bin/env python3
"""Drive a real KubeIntellect session and answer its approval gates like a human would.

Why this exists
---------------
Recording a demo of the approval flow needs a person sitting at the keyboard: the agent
proposes a mutating command, stops, and waits for someone to type ``approve``. That makes an
unattended recording impossible and makes debugging the gate tedious -- you cannot loop on it.

This driver is that person. It opens a session, streams the answer, and when a gate arrives it
pauses for a beat, echoes the approval as if typed, and sends it. The run continues to the end.

What it deliberately does NOT do
--------------------------------
It does not disable, patch or bypass the gate, and it does not use the server's ``auto_approve``
flag. Both would make the gate *not happen*, which is the opposite of what a demo needs -- the
gate appearing and then being satisfied IS the thing worth showing. Everything here goes over
the ordinary HTTP API, so what you record is the real path a real client takes, and a bug in the
gate still reproduces under this driver. That is what makes it useful for debugging as well as
for recording.

Requirements
------------
* The key must be allowed to run mutating actions -- ``operator`` or above. A ``readonly`` key
  never reaches an approval gate, so the driver would (correctly) report that none appeared.
* Nothing outside the standard library. No install step, no extra dependency in the image.

Usage
-----
    # one prompt, approve every gate
    scripts/demo/auto_approve_driver.py --api-key "$KI_API_KEY" \
        --prompt "scale the payments deployment to 5 replicas"

    # a scripted multi-turn demo, recorded with asciinema
    asciinema rec demo.cast -c "scripts/demo/auto_approve_driver.py --scenario demo.txt"

    # debugging: deny the second gate instead, and keep a machine-readable trace
    scripts/demo/auto_approve_driver.py --prompt "..." --deny-nth 2 --json-log gates.jsonl

Exit codes
----------
    0  every gate encountered was answered and every prompt streamed to completion
    1  a stream failed, timed out, or a gate was left unanswered
    2  bad usage / configuration
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"
CHAT_PATH = "/v1/chat/completions"

# Must be a phrase the server actually accepts. Kept in sync with the server's
# _APPROVAL_PHRASES / _DENIAL_PHRASES in app/agent/hitl.py; --approve-phrase can override.
# Deliberately NOT "approve all" or any other _AUTO_APPROVE_PHRASES member: those switch the
# session into blanket auto-approval, so later gates never appear and the recording loses them.
DEFAULT_APPROVE = "approve"
DEFAULT_DENY = "deny"


# The result of the most recent `run()`, so an embedding caller can inspect the gates without
# re-parsing stdout. Set on every run, including a failed one.
LAST_RUN: "RunResult | None" = None


class DriverError(RuntimeError):
    """A failure that should end the run non-zero."""


@dataclass
class Gate:
    """One approval gate the server raised."""

    action_id: str
    risk_level: str
    human_summary: str
    answered_with: str = ""
    turn: int = 0

    def as_record(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "risk_level": self.risk_level,
            "human_summary": self.human_summary,
            "answered_with": self.answered_with,
            "turn": self.turn,
        }


@dataclass
class RunResult:
    gates: list[Gate] = field(default_factory=list)
    unanswered: list[Gate] = field(default_factory=list)
    turns: int = 0


# ── wire helpers ──────────────────────────────────────────────────────────────


def _post_stream(
    base_url: str,
    api_key: str | None,
    session_id: str,
    content: str,
    timeout: float,
):
    """POST one chat turn and yield decoded SSE data payloads (dicts), in order."""
    body = json.dumps(
        {
            "model": "kubeintellect",
            "stream": True,
            "messages": [{"role": "user", "content": content}],
        }
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Session-ID": session_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(base_url.rstrip("/") + CHAT_PATH, data=body, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - operator-supplied URL
    except urllib.error.HTTPError as exc:  # surface the server's own message, it is useful
        detail = exc.read().decode(errors="replace")[:400]
        raise DriverError(f"HTTP {exc.code} from {base_url}{CHAT_PATH}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DriverError(f"cannot reach {base_url}{CHAT_PATH}: {exc}") from exc

    with resp:
        for raw in resp:
            line = raw.decode(errors="replace").rstrip("\n")
            if not line or line.startswith(":"):  # blank separator or ": heartbeat"
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                # A malformed frame is a real defect worth seeing rather than swallowing,
                # but it should not abort a recording mid-way.
                print(f"\n[driver] skipped unparseable frame: {payload[:120]!r}", file=sys.stderr)


def _extract(chunk: dict[str, Any]) -> tuple[str, Gate | None]:
    """Return (content_delta, gate_or_None) for one SSE chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return "", None
    choice = choices[0]
    text = (choice.get("delta") or {}).get("content") or ""
    gate = None
    if choice.get("hitl_required"):
        gate = Gate(
            action_id=str(choice.get("action_id", "")),
            risk_level=str(choice.get("risk_level", "unknown")),
            human_summary=str(choice.get("human_summary", "")),
        )
    return text, gate


# ── the human ─────────────────────────────────────────────────────────────────


def _type_like_a_human(text: str, cps: float) -> None:
    """Echo the approval the way it would appear if someone typed it.

    Purely cosmetic, and the point of the whole exercise: an instantly-appearing answer reads
    as automation, which is exactly what the recording must not look like. cps<=0 disables it.
    """
    if cps <= 0:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    delay = 1.0 / cps
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)


def run_turn(
    args: argparse.Namespace,
    session_id: str,
    content: str,
    result: RunResult,
    *,
    echo_prompt: bool,
) -> str | None:
    """Stream one turn. Returns the reply to send next (an approval), or None if done."""
    if echo_prompt:
        print(f"\n\033[1m> {content}\033[0m")
        sys.stdout.flush()

    seen: list[Gate] = []
    for chunk in _post_stream(args.base_url, args.api_key, session_id, content, args.timeout):
        text, gate = _extract(chunk)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
        if gate is not None:
            seen.append(gate)
    print()

    if not seen:
        return None

    for g in seen:
        g.turn = result.turns
    result.gates.extend(seen)

    if len(seen) > 1:
        # The server interrupts and waits, so it cannot have two gates outstanding on one
        # session: this is a protocol violation. Answering one and discarding the rest would
        # produce a recording that looks correct and is not, which is the exact failure this
        # tool exists to prevent -- so refuse the turn and make it visible instead.
        for g in seen:
            g.answered_with = "(refused: more than one gate in a single stream)"
        result.unanswered.extend(seen)
        print(
            f"[driver] REFUSED: more than one approval gate arrived in a single stream "
            f"({', '.join(g.action_id or '?' for g in seen)}). The server interrupts and waits, "
            f"so this should be impossible; answering one and dropping the rest would hide it.",
            file=sys.stderr,
        )
        return None

    pending = seen[0]
    n = len(result.gates)

    if args.max_approvals and n > args.max_approvals:
        pending.answered_with = "(refused: --max-approvals reached)"
        result.unanswered.append(pending)
        print(
            f"[driver] gate #{n} left unanswered: --max-approvals={args.max_approvals} reached.",
            file=sys.stderr,
        )
        return None

    deny = args.deny_nth is not None and n == args.deny_nth
    phrase = args.deny_phrase if deny else args.approve_phrase
    pending.answered_with = phrase

    # The pause is what makes it read as a person deciding rather than a script reacting.
    time.sleep(args.think_delay)
    print("\n\033[1m> \033[0m", end="")
    _type_like_a_human(phrase, args.type_cps)
    print()
    return phrase


def run(args: argparse.Namespace, prompts: list[str]) -> RunResult:
    global LAST_RUN
    session_id = args.session_id or str(uuid.uuid4())
    result = RunResult()
    LAST_RUN = result
    print(f"[driver] session {session_id} -> {args.base_url}", file=sys.stderr)

    for prompt in prompts:
        result.turns += 1
        reply = run_turn(args, session_id, prompt, result, echo_prompt=True)
        # A gate can follow a gate (a plan with several mutating steps), so keep answering
        # until the turn completes without raising one.
        guard = 0
        while reply is not None:
            guard += 1
            if guard > args.max_chained:
                raise DriverError(
                    f"more than --max-chained={args.max_chained} consecutive gates in one turn; "
                    "refusing to loop forever"
                )
            reply = run_turn(args, session_id, reply, result, echo_prompt=False)

    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="auto_approve_driver.py",
        description="Drive a real KubeIntellect session and answer its approval gates as a human.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", action="append", metavar="TEXT",
                     help="a prompt to send; repeat for a multi-turn demo")
    src.add_argument("--scenario", metavar="FILE",
                     help="file of prompts, one per line ('#' comments and blanks ignored)")

    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    p.add_argument("--api-key", default=os.environ.get("KI_API_KEY") or None,
                   help="Bearer key; must be operator or above to reach a gate at all. "
                        "Defaults to $KI_API_KEY, which is the form to prefer: a key passed "
                        "on the command line is visible in `ps` and lands in any terminal "
                        "recording of the session.")
    p.add_argument("--session-id", default=None, help="reuse a specific session id")

    p.add_argument("--approve-phrase", default=DEFAULT_APPROVE,
                   help=f"what the human types to approve (default {DEFAULT_APPROVE!r})")
    p.add_argument("--deny-phrase", default=DEFAULT_DENY,
                   help=f"what the human types to deny (default {DEFAULT_DENY!r})")
    p.add_argument("--deny-nth", type=int, default=None, metavar="N",
                   help="deny the Nth gate instead of approving it (1-based) -- for testing "
                        "that a denial actually stops the action")

    p.add_argument("--think-delay", type=float, default=1.2, metavar="SEC",
                   help="pause before answering, so it reads as a decision (default 1.2)")
    p.add_argument("--type-cps", type=float, default=14.0, metavar="CPS",
                   help="characters per second when echoing the answer; 0 = instant")
    p.add_argument("--timeout", type=float, default=180.0, metavar="SEC",
                   help="per-request timeout (default 180)")
    p.add_argument("--max-approvals", type=int, default=0, metavar="N",
                   help="stop approving after N gates (0 = no limit)")
    p.add_argument("--max-chained", type=int, default=10, metavar="N",
                   help="safety bound on consecutive gates in one turn (default 10)")
    p.add_argument("--json-log", metavar="FILE",
                   help="append one JSON object per gate, for debugging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.scenario:
        try:
            raw = open(args.scenario, encoding="utf-8").read().splitlines()
        except OSError as exc:
            print(f"error: cannot read --scenario {args.scenario}: {exc}", file=sys.stderr)
            return 2
        prompts = [ln.strip() for ln in raw if ln.strip() and not ln.lstrip().startswith("#")]
    else:
        prompts = list(args.prompt or [])

    if not prompts:
        print("error: no prompts to send", file=sys.stderr)
        return 2
    if args.deny_nth is not None and args.deny_nth < 1:
        print("error: --deny-nth is 1-based", file=sys.stderr)
        return 2

    try:
        result = run(args, prompts)
    except DriverError as exc:
        print(f"\n[driver] FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[driver] interrupted", file=sys.stderr)
        return 1

    if args.json_log:
        with open(args.json_log, "a", encoding="utf-8") as fh:
            for g in result.gates:
                fh.write(json.dumps(g.as_record()) + "\n")

    answered = len(result.gates) - len(result.unanswered)
    print(
        f"\n[driver] {result.turns} prompt(s), {len(result.gates)} gate(s), {answered} answered.",
        file=sys.stderr,
    )
    if not result.gates:
        print(
            "[driver] note: no approval gate appeared. A readonly key never reaches one, and a "
            "read-only question would not raise one either.",
            file=sys.stderr,
        )
    return 1 if result.unanswered else 0


if __name__ == "__main__":
    raise SystemExit(main())
