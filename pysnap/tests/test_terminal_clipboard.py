"""Unit tests for terminal clipboard export helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pysnap.terminal.clipboard import copy_text_to_host_clipboard


class FakeOutput:
    """Collect raw output writes for OSC 52 clipboard fallback."""

    def __init__(self) -> None:
        """Initialize empty output buffers."""
        self.payloads: list[str] = []
        self.flushed = False

    def write_raw(self, data: str) -> None:
        """Collect one raw terminal payload."""
        self.payloads.append(data)

    def flush(self) -> None:
        """Record a flush event."""
        self.flushed = True


class ClipboardHelperTests(unittest.TestCase):
    """Verify host clipboard export strategies."""

    def test_copy_prefers_platform_command_when_available(self) -> None:
        """Use a native clipboard command before falling back to OSC 52."""
        with (
            patch("pysnap.terminal.clipboard.shutil.which", return_value="/usr/bin/pbcopy"),
            patch("pysnap.terminal.clipboard.subprocess.run") as run,
        ):
            self.assertTrue(copy_text_to_host_clipboard("hello"))

        run.assert_called_once()

    def test_copy_falls_back_to_osc52_when_no_command_exists(self) -> None:
        """Emit an OSC 52 sequence when no native clipboard command is found."""
        output = FakeOutput()
        with patch("pysnap.terminal.clipboard.shutil.which", return_value=None):
            copied = copy_text_to_host_clipboard("hello", output=output)

        self.assertTrue(copied)
        self.assertEqual(len(output.payloads), 1)
        self.assertTrue(output.payloads[0].startswith("\x1b]52;c;"))
        self.assertTrue(output.flushed)

    def test_copy_returns_false_without_any_strategy(self) -> None:
        """Report failure when neither native nor terminal clipboard is usable."""
        with patch("pysnap.terminal.clipboard.shutil.which", return_value=None):
            self.assertFalse(copy_text_to_host_clipboard("hello", output=None))


if __name__ == "__main__":
    unittest.main()
