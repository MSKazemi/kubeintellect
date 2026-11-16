"""`kubeintellect init` ran `sudo` as its first action, before it had asked anything.

Seen live on 2026-08-27: `[sudo] password for mohsen:` appeared before the wizard's first
question. `_ensure_tool` downloaded a binary from the network and moved it into
`/usr/local/bin` -- for kubectl, kind and helm alike -- on the user's behalf, with no word
beforehand. On a fresh machine, which is the wizard's entire audience, that is the first thing
that happens, and nothing documents it.

Installing a binary into a system path needs the user's consent, and a closed stdin is not
consent: a container build or a CI job that pipes nothing in must get a sentence naming the
manual install, not a silent privileged write.
"""
from __future__ import annotations

import builtins
import subprocess

import pytest

from app import cli


@pytest.fixture
def missing_tool(monkeypatch):
    """`which <tool>` fails, so `_ensure_tool` reaches the decision to install."""
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
    )


def _answers(monkeypatch, *replies: str) -> list[str]:
    prompts: list[str] = []
    pending = iter(replies)

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(pending)

    monkeypatch.setattr(builtins, "input", _input)
    return prompts


def _closed_stdin(monkeypatch) -> None:
    def _eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)


class TestTheInstallerRunsOnlyWithConsent:
    @pytest.mark.parametrize("reply", ["", "y", "Y", "yes"])
    def test_an_affirmative_answer_installs(self, missing_tool, monkeypatch, reply):
        _answers(monkeypatch, reply)
        ran: list[str] = []
        cli._ensure_tool("kind", lambda: ran.append("kind"))
        assert ran == ["kind"]

    @pytest.mark.parametrize("reply", ["n", "N", "no", "nope", "later"])
    def test_anything_else_does_not(self, missing_tool, monkeypatch, reply):
        _answers(monkeypatch, reply)
        ran: list[str] = []
        with pytest.raises(SystemExit):
            cli._ensure_tool("kind", lambda: ran.append("kind"))
        assert ran == []

    def test_a_closed_stdin_is_a_refusal_not_consent(self, missing_tool, monkeypatch):
        _closed_stdin(monkeypatch)
        ran: list[str] = []
        with pytest.raises(SystemExit):
            cli._ensure_tool("helm", lambda: ran.append("helm"))
        assert ran == []

    def test_the_question_says_where_it_will_write_and_that_it_may_elevate(
        self, missing_tool, monkeypatch
    ):
        prompts = _answers(monkeypatch, "y")
        cli._ensure_tool("kind", lambda: None)
        assert len(prompts) == 1
        assert "kind" in prompts[0]
        assert "/usr/local/bin" in prompts[0]
        assert "sudo" in prompts[0]

    def test_a_tool_already_present_asks_nothing(self, monkeypatch):
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
        )
        prompts = _answers(monkeypatch)
        cli._ensure_tool("kind", lambda: pytest.fail("installer must not run"))
        assert prompts == []


class TestDecliningIsHandledLikeAnyOtherNoInstall:
    def test_an_optional_tool_that_is_declined_is_a_warning(
        self, missing_tool, monkeypatch, capsys
    ):
        _answers(monkeypatch, "n")
        cli._ensure_tool("kubectl", lambda: pytest.fail("declined"), required=False)
        out = capsys.readouterr().out
        assert "Continuing without it" in out

    def test_a_required_tool_that_is_declined_exits_naming_the_manual_step(
        self, missing_tool, monkeypatch, capsys
    ):
        _answers(monkeypatch, "n")
        with pytest.raises(SystemExit) as e:
            cli._ensure_tool("kind", lambda: pytest.fail("declined"))
        assert e.value.code == 1
        assert "kind" in capsys.readouterr().err

    def test_a_closed_stdin_on_an_optional_tool_does_not_end_the_wizard(
        self, missing_tool, monkeypatch, capsys
    ):
        _closed_stdin(monkeypatch)
        cli._ensure_tool("kubectl", lambda: pytest.fail("no consent"), required=False)
        assert "Continuing without it" in capsys.readouterr().out
