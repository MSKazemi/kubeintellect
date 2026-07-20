"""
Tests for cli/render.py — pager threshold logic.

These tests require the kube-q CLI package (pip install kube-q).
They are skipped automatically when the package is not installed.
"""
import unittest
from unittest.mock import patch

try:
    import cli.render  # noqa: F401
    _CLI_AVAILABLE = True
except ModuleNotFoundError:
    _CLI_AVAILABLE = False


@unittest.skipUnless(_CLI_AVAILABLE, "kube-q CLI not installed (pip install kube-q)")
class TestShouldUsePager(unittest.TestCase):
    """_should_use_pager returns True only for long TTY output."""

    def setUp(self):
        from cli.render import _should_use_pager, _PAGER_LINE_THRESHOLD
        self._fn = _should_use_pager
        self._cap = _PAGER_LINE_THRESHOLD  # currently 40

    # ── Non-TTY: pager must never activate ──────────────────────────────────

    def test_non_tty_never_pagers(self):
        """When stdout is not a TTY (piped), pager is always skipped."""
        long_text = "\n" * 200
        with patch("cli.render.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            self.assertFalse(self._fn(long_text))

    # ── TTY short: inline rendering ─────────────────────────────────────────

    def test_short_response_no_pager(self):
        """A 5-line response on a 24-line terminal should render inline."""
        short_text = "\n".join(["line"] * 5)
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 24
            self.assertFalse(self._fn(short_text))

    # ── TTY long on short terminal: pager fires ──────────────────────────────

    def test_long_response_short_terminal_uses_pager(self):
        """60 newlines on a 24-line terminal triggers pager (threshold = min(20, 40) = 20)."""
        long_text = "\n".join([f"Pod: pod-{i}" for i in range(60)])
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 24
            self.assertTrue(self._fn(long_text))

    # ── TTY long on TALL terminal: pager still fires (cap enforced) ──────────

    def test_long_response_tall_terminal_still_pages(self):
        """57 newlines on a 100-line terminal must still trigger pager (cap=40)."""
        text_57 = "\n" * 57  # 57 pods → 57+ newlines
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 100  # very tall terminal
            # threshold = min(100-4, 40) = 40 → 57 > 40 → True
            self.assertTrue(self._fn(text_57))

    def test_at_cap_threshold_no_pager(self):
        """Exactly _PAGER_LINE_THRESHOLD newlines on a tall terminal: no pager."""
        text_at_cap = "\n" * self._cap  # exactly 40
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 100
            self.assertFalse(self._fn(text_at_cap))

    def test_one_above_cap_triggers_pager(self):
        """One newline above cap triggers pager even on a very tall terminal."""
        text_above_cap = "\n" * (self._cap + 1)
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 100
            self.assertTrue(self._fn(text_above_cap))

    # ── Terminal height smaller than cap: height wins ────────────────────────

    def test_threshold_is_terminal_height_minus_margin_when_below_cap(self):
        """On a 30-line terminal, threshold is 26 (height-4), not 40 (cap)."""
        height = 30  # 30 - 4 = 26 < 40 cap → threshold = 26
        text_at_threshold = "\n" * (height - 4)  # exactly 26
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = height
            self.assertFalse(self._fn(text_at_threshold))

    def test_one_line_above_height_threshold_triggers_pager(self):
        """One newline above height-based threshold fires pager."""
        height = 30
        text_above = "\n" * (height - 3)  # 27 > 26
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = height
            self.assertTrue(self._fn(text_above))

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_none_console_height_falls_back_to_24(self):
        """When console.height is None/falsy, fall back to height=24."""
        long_text = "\n" * 30  # > min(24-4, 40)=20 → True
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = None
            self.assertTrue(self._fn(long_text))

    def test_minimum_threshold_is_10(self):
        """Even with a 1-line terminal, threshold is at least 10."""
        text_9_lines = "\n" * 9
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 1
            self.assertFalse(self._fn(text_9_lines))

    def test_minimum_threshold_just_above_10(self):
        """11 newlines on a 1-line terminal triggers pager (threshold=10)."""
        text_11_lines = "\n" * 11
        with patch("cli.render.sys") as mock_sys, \
             patch("cli.render.console") as mock_console:
            mock_sys.stdout.isatty.return_value = True
            mock_console.height = 1
            self.assertTrue(self._fn(text_11_lines))
