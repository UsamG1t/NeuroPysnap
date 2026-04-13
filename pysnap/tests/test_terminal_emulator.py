"""Unit tests for terminal emulator scrollback behavior."""

from __future__ import annotations

import unittest

from pysnap.terminal.emulator import TerminalEmulator


def _visible_lines(emulator: TerminalEmulator) -> list[str]:
    """Return trimmed visible lines from the current emulator buffer."""
    visible: list[str] = []
    for y in range(emulator.screen.lines):
        line = emulator.screen.buffer[y]
        visible.append(
            "".join(line[x].data for x in range(emulator.screen.columns)).rstrip()
        )
    return visible


class TerminalEmulatorTests(unittest.TestCase):
    """Verify local scrollback navigation."""

    def test_scroll_up_and_down_navigate_local_history(self) -> None:
        """Expose older and newer lines without affecting guest output."""
        emulator = TerminalEmulator(columns=10, lines=3)
        emulator.feed(b"1\r\n2\r\n3\r\n4\r\n5\r\n")

        self.assertEqual(_visible_lines(emulator), ["4", "5", ""])
        self.assertFalse(emulator.is_scrollback_active)

        emulator.scroll_up(1)
        self.assertEqual(_visible_lines(emulator), ["3", "4", "5"])
        self.assertTrue(emulator.is_scrollback_active)

        emulator.scroll_up(1)
        self.assertEqual(_visible_lines(emulator), ["2", "3", "4"])

        emulator.scroll_down(1)
        self.assertEqual(_visible_lines(emulator), ["3", "4", "5"])

    def test_scroll_to_top_and_bottom_jump_through_history(self) -> None:
        """Jump to the oldest retained output and back to the live bottom."""
        emulator = TerminalEmulator(columns=10, lines=3)
        emulator.feed(b"1\r\n2\r\n3\r\n4\r\n5\r\n")

        emulator.scroll_to_top()
        self.assertEqual(_visible_lines(emulator), ["1", "2", "3"])
        self.assertTrue(emulator.is_scrollback_active)

        emulator.scroll_to_bottom()
        self.assertEqual(_visible_lines(emulator), ["4", "5", ""])
        self.assertFalse(emulator.is_scrollback_active)


if __name__ == "__main__":
    unittest.main()
