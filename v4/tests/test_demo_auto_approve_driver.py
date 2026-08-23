"""The demo auto-approval driver answers a real HITL gate over a real socket.

`scripts/demo/auto_approve_driver.py` exists so a demo of the approval flow can be recorded
without a person at the keyboard. Its whole value rests on one claim: it behaves like a human
client, so what it drives is the real gate rather than a bypass. That claim is only worth
anything if it is tested against an actual HTTP conversation, so these tests stand up a
throwaway SSE server on a loopback port and let the driver talk to it.

The fake server mimics the contract in `app/api/v1/endpoints/chat_completions.py`: `data:`
frames, `": heartbeat"` comments, a terminating `data: [DONE]`, and a gate carried as
`hitl_required` / `action_id` / `risk_level` / `human_summary` on the choices entry. It
deliberately does NOT import the app -- the point is to pin the wire format the driver depends
on, so a change to that format fails here loudly instead of only at demo-recording time.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_DRIVER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "demo" / "auto_approve_driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("auto_approve_driver", _DRIVER_PATH)
    assert spec and spec.loader, f"cannot load {_DRIVER_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Register before executing: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is None for a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _frame(content: str = "", **extra) -> str:
    choice = {"index": 0, "delta": {"content": content}}
    choice.update(extra)
    return "data: " + json.dumps({"object": "chat.completion.chunk", "choices": [choice]}) + "\n\n"


# The real server sends the gate fields on the SAME chunk that carries the "Approval Required"
# message (`_make_chunk` does `choice.update(hitl_data)` with content already set), so the
# driver must render the text and detect the gate from one frame.
GATE_TEXT = "\n\n---\n\U0001f534 **Approval Required** \u2014 risk level: `HIGH`\n"
GATE = {
    "hitl_required": True,
    "action_id": "act-42",
    "risk_level": "high",
    "human_summary": "scale payments to 5 replicas",
}


class _Server:
    """A loopback SSE endpoint that records what the driver sent it."""

    def __init__(self, script):
        self.script = list(script)  # one entry per POST: the frames to emit
        self.received: list[dict] = []
        self.headers_seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # keep pytest output clean
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append(json.loads(body))
                # Lower-cased on purpose: urllib capitalises header names on the wire
                # ("X-session-id"), HTTP header names are case-insensitive per RFC 7230, and
                # Starlette lower-cases them before `request.headers.get` ever sees them. A
                # case-sensitive assertion here would fail on behaviour the server accepts.
                outer.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
                turn = len(outer.received) - 1
                frames = outer.script[turn] if turn < len(outer.script) else [_frame("done.")]
                payload = ("".join(frames) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def _last_run_gates(mod):
    """The gates the most recent `main()` call recorded (set by the driver for callers)."""
    return mod.LAST_RUN.gates


def _argv(url: str, *extra: str) -> list[str]:
    # No typing delay and no think pause: the cosmetics are what make a recording look human
    # and are exactly what must not slow a test suite down.
    return ["--base-url", url, "--prompt", "scale payments", "--type-cps", "0",
            "--think-delay", "0", *extra]


class TestItAnswersTheGate:
    def test_a_gate_is_answered_on_the_same_session_and_the_run_succeeds(self, capsys):
        script = [
            [_frame(GATE_TEXT, **GATE)],                          # turn 1: the gate
            [_frame("Scaled to 5 replicas.\n")],                 # turn 2: after approval
        ]
        with _Server(script) as srv:
            code = driver.main(_argv(srv.url, "--session-id", "demo-1"))

        assert code == 0
        assert len(srv.received) == 2, "the driver must send a second turn to answer the gate"
        assert srv.received[1]["messages"][0]["content"] == "approve"
        # Same session on both turns, or the server resumes nothing and the gate never clears.
        sessions = {h["x-session-id"] for h in srv.headers_seen}
        assert sessions == {"demo-1"}, sessions
        out = capsys.readouterr().out
        assert "Approval Required" in out, "the gate text rides on the gate chunk itself"
        assert "Scaled to 5 replicas." in out, "the post-approval stream must be rendered"

    def test_the_approval_word_is_one_the_server_actually_accepts(self):
        """A phrase the server does not recognise would hang every recording."""
        from app.agent import hitl

        assert hitl.is_approval(driver.DEFAULT_APPROVE)
        assert hitl.is_denial(driver.DEFAULT_DENY)
        # Not a blanket auto-approve: that would suppress every later gate, and the gates are
        # the part of the demo worth recording.
        assert not hitl.is_auto_approve_request(driver.DEFAULT_APPROVE)

    def test_no_gate_means_no_second_turn(self):
        with _Server([[_frame("3 pods are running.\n")]]) as srv:
            code = driver.main(_argv(srv.url))
        assert code == 0
        assert len(srv.received) == 1

    def test_it_sends_the_bearer_token_when_given_a_key(self):
        with _Server([[_frame("ok")]]) as srv:
            driver.main(_argv(srv.url, "--api-key", "ki-secret"))
        assert srv.headers_seen[0]["authorization"] == "Bearer ki-secret"

    def test_chained_gates_are_each_answered(self):
        script = [
            [_frame("", **GATE)],
            [_frame("", **GATE)],
            [_frame("both done.\n")],
        ]
        with _Server(script) as srv:
            code = driver.main(_argv(srv.url))
        assert code == 0
        assert len(srv.received) == 3
        assert [m["messages"][0]["content"] for m in srv.received[1:]] == ["approve", "approve"]


class TestItFailsLoudly:
    def test_stopping_early_leaves_a_gate_unanswered_and_exits_nonzero(self):
        """A recording that silently stopped half-way is worse than one that reports failure."""
        script = [[_frame("", **GATE)], [_frame("", **GATE)], [_frame("done")]]
        with _Server(script) as srv:
            code = driver.main(_argv(srv.url, "--max-approvals", "1"))
        assert code == 1, "the second gate was refused, so the run did not complete"
        assert len(srv.received) == 2, "it must not send a third turn after refusing"

    def test_zero_means_no_limit(self):
        with _Server([[_frame("", **GATE)], [_frame("done")]]) as srv:
            code = driver.main(_argv(srv.url, "--max-approvals", "0"))
        assert code == 0

    def test_an_endless_gate_loop_is_bounded(self):
        # A server that gates forever must not spin: --max-chained stops it.
        with _Server([[_frame("", **GATE)]] * 50) as srv:
            code = driver.main(_argv(srv.url, "--max-chained", "3"))
        assert code == 1
        assert len(srv.received) <= 5

    def test_an_unreachable_server_exits_one_rather_than_raising(self, capsys):
        code = driver.main(_argv("http://127.0.0.1:1", "--timeout", "2"))
        assert code == 1
        assert "FAILED" in capsys.readouterr().err

    def test_deny_nth_sends_the_denial_instead(self):
        script = [[_frame("", **GATE)], [_frame("", **GATE)], [_frame("stopped.\n")]]
        with _Server(script) as srv:
            code = driver.main(_argv(srv.url, "--deny-nth", "2"))
        assert code == 0
        assert [m["messages"][0]["content"] for m in srv.received[1:]] == ["approve", "deny"]

    def test_heartbeats_and_junk_frames_do_not_derail_the_stream(self, capsys):
        raw = [": heartbeat\n\n", "\n", _frame("hello "), "data: {not json}\n\n", _frame("world")]
        with _Server([raw]) as srv:
            code = driver.main(_argv(srv.url))
        assert code == 0
        assert "hello world" in capsys.readouterr().out

    def test_a_one_based_flag_rejects_zero(self):
        assert driver.main(["--prompt", "x", "--deny-nth", "0"]) == 2

    def test_no_prompt_at_all_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            driver.main([])
        assert exc.value.code == 2


class TestItNeverDropsAGateSilently:
    """The server cannot have two outstanding gates on one session -- it interrupts and waits.

    So two gates in a single stream is a protocol violation, and the one thing the driver must
    not do with it is pick one and discard the other without saying so. A demo recorded against
    a server in that state would look fine and be wrong, which is precisely the failure this
    tool exists to make impossible.
    """

    def test_two_gates_in_one_stream_are_both_recorded_and_the_run_fails(self, capsys):
        script = [
            [_frame("first ", **GATE),
             _frame("second ", **{**GATE, "action_id": "act-99"})],
            [_frame("done")],
        ]
        with _Server(script) as srv:
            code = driver.main(_argv(srv.url))

        assert code == 1, "a dropped gate must not exit zero"
        ids = [g.action_id for g in _last_run_gates(driver)]
        assert "act-42" in ids and "act-99" in ids, f"both gates must be recorded, got {ids}"
        assert "more than one approval gate" in capsys.readouterr().err.lower()
        assert len(srv.received) == 1, "it must not answer a stream it could not interpret"
