"""Unit tests for size-related terminal query handling."""

from __future__ import annotations

import unittest

from pysnap.terminal.emulator import TerminalEmulator
from pysnap.terminal.protocol import TerminalQueryResponder


class TerminalQueryResponderTests(unittest.TestCase):
    """Verify serial terminal query responses."""

    def test_reports_cursor_position(self) -> None:
        """Reply to a cursor-position request using emulator coordinates."""
        emulator = TerminalEmulator(columns=80, lines=24)
        emulator.screen.cursor.x = 39
        emulator.screen.cursor.y = 11
        responder = TerminalQueryResponder()

        responses = responder.collect_responses(
            b"\x1b[6n",
            emulator=emulator,
            columns=80,
            lines=23,
        )

        self.assertEqual(responses, [b"\x1b[12;40R"])

    def test_reports_dec_cursor_position(self) -> None:
        """Reply to DEC-specific cursor-position queries too."""
        emulator = TerminalEmulator(columns=100, lines=40)
        emulator.screen.cursor.x = 4
        emulator.screen.cursor.y = 2
        responder = TerminalQueryResponder()

        responses = responder.collect_responses(
            b"\x1b[?6n",
            emulator=emulator,
            columns=100,
            lines=39,
        )

        self.assertEqual(responses, [b"\x1b[?3;5R"])

    def test_reports_text_area_size(self) -> None:
        """Reply to xterm text-area size requests."""
        emulator = TerminalEmulator(columns=120, lines=40)
        responder = TerminalQueryResponder()

        responses = responder.collect_responses(
            b"\x1b[18t",
            emulator=emulator,
            columns=120,
            lines=39,
        )

        self.assertEqual(responses, [b"\x1b[8;39;120t"])

    def test_resize_style_bottom_right_probe_uses_current_screen_geometry(self) -> None:
        """Support the classic far-cursor probe used by xterm-aware tools."""
        emulator = TerminalEmulator(columns=80, lines=24)
        responder = TerminalQueryResponder()
        payload = b"\x1b[999;999H\x1b[6n"

        emulator.feed(payload)
        responses = responder.collect_responses(
            payload,
            emulator=emulator,
            columns=80,
            lines=23,
        )

        self.assertEqual(responses, [b"\x1b[24;80R"])

    def test_reports_screen_size_in_characters(self) -> None:
        """Reply to xterm screen-size requests."""
        emulator = TerminalEmulator(columns=120, lines=40)
        responder = TerminalQueryResponder()

        responses = responder.collect_responses(
            b"\x1b[19t",
            emulator=emulator,
            columns=120,
            lines=39,
        )

        self.assertEqual(responses, [b"\x1b[9;39;120t"])

    def test_buffers_split_query_sequences(self) -> None:
        """Handle CSI queries that arrive across multiple TCP reads."""
        emulator = TerminalEmulator(columns=80, lines=24)
        responder = TerminalQueryResponder()

        self.assertEqual(
            responder.collect_responses(
                b"\x1b[1",
                emulator=emulator,
                columns=80,
                lines=23,
            ),
            [],
        )
        self.assertEqual(
            responder.collect_responses(
                b"8t",
                emulator=emulator,
                columns=80,
                lines=23,
            ),
            [b"\x1b[8;23;80t"],
        )

    def test_ignores_unrelated_control_sequences(self) -> None:
        """Avoid sending replies to escape sequences unrelated to sizing."""
        emulator = TerminalEmulator(columns=80, lines=24)
        responder = TerminalQueryResponder()

        responses = responder.collect_responses(
            b"\x1b[31mhello\x1b[0m",
            emulator=emulator,
            columns=80,
            lines=23,
        )

        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main()
