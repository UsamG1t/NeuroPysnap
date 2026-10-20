"""Unit tests for terminal key translation."""

from __future__ import annotations

import unittest

from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from pysnap.terminal.keymap import key_press_to_bytes


class KeymapTests(unittest.TestCase):
    """Verify prompt-toolkit key translation."""

    def test_special_key_is_translated_to_ansi_sequence(self) -> None:
        """Translate arrow keys into VT-compatible escape sequences."""
        self.assertEqual(key_press_to_bytes(KeyPress(Keys.Up, "\x1b[A")), b"\x1b[A")
        self.assertEqual(key_press_to_bytes(KeyPress(Keys.Up, "\x1bOA")), b"\x1b[A")

    def test_plain_text_key_is_encoded_as_utf8(self) -> None:
        """Pass regular text keys through as UTF-8 bytes."""
        self.assertEqual(key_press_to_bytes(KeyPress("x", "x")), b"x")

    def test_unsupported_virtual_key_returns_none(self) -> None:
        """Ignore prompt-toolkit virtual keys that have no serial mapping."""
        self.assertIsNone(key_press_to_bytes(KeyPress(Keys.CPRResponse, "")))

    def test_split_sequences_are_sent_once(self) -> None:
        """Send the bytes of a sequence that prompt_toolkit splits into several keys once."""
        for sequence in ("\x1b[1;3A", "\x1b[1;3D", "\x1b[3;3~", "\x1b\x1b[A"):
            with self.subTest(sequence=sequence):
                presses = []
                parser = Vt100Parser(presses.append)
                parser.feed(sequence)
                parser.flush()
                sent = b"".join(filter(None, (key_press_to_bytes(press) for press in presses)))
                self.assertEqual(sent, sequence.encode())


if __name__ == "__main__":
    unittest.main()
