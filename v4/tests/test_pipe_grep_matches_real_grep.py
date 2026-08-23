"""The pipe emulator must not answer a different question than the one asked.

Pass 85 of the standing audit (T38). `run_kubectl` supports `| grep` by reimplementing grep in
Python — a documented defence layer ("Layer 4 — pipe emulation") that had **no tests at all**.
The parser skipped every token starting with `-` that was not `-v`, `-i` or `-E`, which produced
two silent wrong answers, both measured against this machine's `/usr/bin/grep`:

* **A value-taking flag left its value in the pattern.** `grep -A 3 Traceback` searched for
  `"3 Traceback"` and returned *(no matching lines)*; real grep returns five lines. `-A/-B/-C`
  is *the* idiom for pulling a stack trace out of a log, so an agent investigating a crash was
  told the traceback in front of it did not exist.
* **Combined short flags vanished.** `-iv` matches neither `-i` nor `-v`, so `grep -iv info`
  ran as `grep info` and returned the exact **complement** of the requested set.

The fix is the rule the module's own docstring already stated for non-grep commands: unsupported
input is *named and refused*, never silently reinterpreted.

The class here is `TestDifferentialAgainstRealGrep`: every supported flag combination is run
through both implementations and compared byte for byte. A reimplementation that is only tested
against its own expectations can only prove it is self-consistent.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.tools.kubectl_tool import _apply_pipes

REAL_GREP = shutil.which("grep")

# The corpus is built so that -w, -x and -F each change the answer. A fixture that cannot
# distinguish a flag from its absence proves nothing about the code that implements it.
LOG = (
    "2026-08-20 10:00:01 INFO  starting worker\n"
    "2026-08-20 10:00:02 ERROR Traceback (most recent call last):\n"
    '2026-08-20 10:00:02 ERROR   File "/app/main.py", line 42\n'
    "2026-08-20 10:00:02 ERROR   ValueError: bad config\n"
    "2026-08-20 10:00:03 INFO  restarting\n"
    "2026-08-20 10:00:09 ERROR Traceback (most recent call last):\n"
    "2026-08-20 10:00:10 info  lowercase tail\n"
    "2026-08-20 10:00:11 INFORMATIONAL only\n"      # -w INFO must not match this
    "2026-08-20 10:00:12 DEBUG mainXpy decoy\n"     # -F main.py must not match this
    "restarting\n"                                  # -x restarting must match only this
)

SUPPORTED = [
    "grep Traceback",
    "grep -v INFO",
    "grep -i info",
    "grep -iv info",
    "grep -vi info",
    "grep -i -v info",
    "grep -E 'ERROR|WARN'",
    "grep -A 3 Traceback",
    "grep -B 1 Traceback",
    "grep -C 2 Traceback",
    "grep -A3 Traceback",
    "grep --after-context=2 Traceback",
    "grep -m 1 ERROR",
    "grep -c ERROR",
    "grep -c NOTHING_HERE",
    "grep -n Traceback",
    "grep -n -A 1 Traceback",
    "grep -w ERROR",
    "grep -w INFO",
    "grep INFO",
    "grep -x restarting",
    "grep restarting",
    "grep -F main.py",
    "grep main.py",
    "grep -F 'ValueError: bad config'",
    "grep -e ERROR -e INFO",
    "grep -- -A",
]


@pytest.mark.skipif(not REAL_GREP, reason="no system grep to compare against")
@pytest.mark.parametrize("command", SUPPORTED)
def test_emulated_grep_equals_real_grep(command):
    """Byte-for-byte, against the binary the emulator is standing in for."""
    import shlex
    proc = subprocess.run(shlex.split(command), input=LOG, capture_output=True, text=True)
    # grep exits 1 when nothing matched, but `-c` still prints "0" — compare stdout, not the
    # exit code. The emulator has no exit code, so an empty stdout is its "(no matching lines)".
    expected = proc.stdout
    got = _apply_pipes(LOG, [command])
    if expected == "":
        assert got == "(no matching lines)"
    else:
        assert got == expected, f"{command}: emulator diverges from real grep"


class TestTheDefectsThatWereMeasured:
    def test_context_flag_no_longer_poisons_the_pattern(self):
        """`grep -A 3 Traceback` searched for '3 Traceback' and found nothing."""
        out = _apply_pipes(LOG, ["grep -A 3 Traceback"])
        assert out != "(no matching lines)"
        assert "ValueError: bad config" in out

    def test_combined_short_flags_are_not_dropped(self):
        """`grep -iv info` returned the complement: the one line the caller excluded."""
        out = _apply_pipes(LOG, ["grep -iv INFO"])
        assert "starting worker" not in out
        assert "lowercase tail" not in out
        assert "bad config" in out

    def test_max_count_is_honoured(self):
        assert len(_apply_pipes(LOG, ["grep -m 1 ERROR"]).splitlines()) == 1

    def test_count_returns_a_number_not_the_lines(self):
        assert _apply_pipes(LOG, ["grep -c ERROR"]) == "4\n"


class TestUnsupportedFlagsAreRefusedNotIgnored:
    @pytest.mark.parametrize("command", [
        "grep -l ERROR", "grep -L ERROR", "grep -q ERROR", "grep -r ERROR",
        "grep -P ERROR", "grep --include=*.log ERROR", "grep -z ERROR",
    ])
    def test_an_unimplemented_flag_raises(self, command):
        with pytest.raises(ValueError) as exc:
            _apply_pipes(LOG, [command])
        assert "does not implement" in str(exc.value)

    def test_the_message_names_the_flag_and_the_supported_set(self):
        with pytest.raises(ValueError) as exc:
            _apply_pipes(LOG, ["grep -q ERROR"])
        assert "-q" in str(exc.value) and "Supported:" in str(exc.value)

    def test_a_flag_that_swallows_its_value_is_rejected_when_the_value_is_missing(self):
        with pytest.raises(ValueError, match="needs a value"):
            _apply_pipes(LOG, ["grep -A"])

    def test_a_context_flag_with_a_non_numeric_value_is_rejected(self):
        with pytest.raises(ValueError, match="needs a number"):
            _apply_pipes(LOG, ["grep -A xyz Traceback"])

    def test_an_invalid_regex_is_reported_not_raised_as_a_crash(self):
        with pytest.raises(ValueError, match="not a valid regex"):
            _apply_pipes(LOG, ["grep '('"])

    def test_a_non_grep_command_is_still_refused(self):
        with pytest.raises(ValueError, match="Only 'grep' is allowed"):
            _apply_pipes(LOG, ["awk '{print $1}'"])

    def test_grep_with_no_pattern_is_refused(self):
        with pytest.raises(ValueError, match="has no pattern"):
            _apply_pipes(LOG, ["grep -i"])


class TestFlagsThatChangeWhatMatches:
    """Each assertion pairs the flag with its absence, so the flag is what is under test."""

    def test_w_requires_a_word_boundary(self):
        assert "INFORMATIONAL" in _apply_pipes(LOG, ["grep INFO"])
        assert "INFORMATIONAL" not in _apply_pipes(LOG, ["grep -w INFO"])

    def test_x_requires_the_whole_line(self):
        assert len(_apply_pipes(LOG, ["grep restarting"]).splitlines()) == 2
        assert _apply_pipes(LOG, ["grep -x restarting"]) == "restarting\n"

    def test_F_makes_the_pattern_literal(self):
        assert "mainXpy" in _apply_pipes(LOG, ["grep main.py"])
        assert "mainXpy" not in _apply_pipes(LOG, ["grep -F main.py"])


class TestChainedPipes:
    def test_two_greps_compose(self):
        out = _apply_pipes(LOG, ["grep ERROR", "grep -v Traceback"])
        assert "Traceback" not in out
        assert "bad config" in out

    def test_a_chain_that_matches_nothing_says_so(self):
        assert _apply_pipes(LOG, ["grep ERROR", "grep NOPE"]) == "(no matching lines)"


class TestDeliberateDivergence:
    def test_several_bare_operands_are_one_pattern(self):
        """Real grep would treat the second operand as a FILE. A pipe segment has no files,
        so an unquoted multi-word pattern is the only thing it can have meant."""
        out = _apply_pipes(LOG, ["grep bad config"])
        assert "bad config" in out
