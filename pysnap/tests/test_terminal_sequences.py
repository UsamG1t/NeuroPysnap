"""Tests for the control sequences that reach ``pyte``."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pysnap.terminal.emulator import SequenceFilter, TerminalEmulator

DATA = Path(__file__).parent / "data"


def _filtered(data: bytes) -> bytes:
    return SequenceFilter().feed(data)


class SequenceFilterTests(unittest.TestCase):
    """Verify which sequences pass, change or disappear."""

    def test_keeps_text_and_sequences_pyte_handles(self) -> None:
        """Pass text, C0 controls, ordinary CSI, DEC modes and ESC sequences."""
        for data in (
            "plain текст\r\n\t\x08".encode(),
            b"\x1b[1;31m", b"\x1b[2J", b"\x1b[10;20H", b"\x1b[1;24r",
            b"\x1b[?25l", b"\x1b[?1049h", b"\x1b[?2004l", b"\x1b[?1J",
            b"\x1b7", b"\x1b8", b"\x1b(B", b"\x1bM", b"\x1b=",
        ):
            with self.subTest(data=data):
                self.assertEqual(_filtered(data), data)

    def test_drops_sequences_pyte_mishandles(self) -> None:
        """Drop other private, prefixed and intermediate CSI sequences."""
        for data in (
            b"\x1b[?4m", b"\x1b[?u", b"\x1b[?1;2c", b"\x1b[>4;2m", b"\x1b[>c",
            b"\x1b[=1c", b"\x1b[<1u", b"\x1b[0%m", b"\x1b[2 q", b"\x1b[!p",
            b"\x1b[?1$p", b"\x1b[1:2H", b"\x1b[?1:2h",
        ):
            with self.subTest(data=data):
                self.assertEqual(_filtered(b"a" + data + b"b"), b"ab")

    def test_drops_control_strings(self) -> None:
        """Skip OSC, DCS, SOS, PM and APC strings up to BEL or ST."""
        for data in (
            b"\x1b]0;title\x07", b"\x1b]52;c;dGV4dA==\x1b\\", b"\x1bP+q436f\x1b\\",
            b"\x1bXsos\x1b\\", b"\x1b^pm\x1b\\", b"\x1b_apc\x1b\\", b"\x1bP1$r0m\x1b\\",
        ):
            with self.subTest(data=data):
                self.assertEqual(_filtered(b"a" + data + b"b"), b"ab")

    def test_rewrites_colon_sgr(self) -> None:
        """Turn colon sub-parameters into the semicolon form."""
        cases = {
            b"\x1b[38:2::1:2:3m": b"\x1b[38;2;1;2;3m",
            b"\x1b[48:2:1:2:3m": b"\x1b[48;2;1;2;3m",
            b"\x1b[1;38:5:208;4:3m": b"\x1b[1;38;5;208;4m",
            b"\x1b[4:0m": b"\x1b[24m",
            b"\x1b[38:2::1::3m": b"\x1b[38;2;1;0;3m",
            b"\x1b[7:1m": b"\x1b[7m",
        }
        for data, expected in cases.items():
            with self.subTest(data=data):
                self.assertEqual(_filtered(data), expected)

    def test_handles_aborts_and_limits(self) -> None:
        """Abort on CAN, SUB or ESC, act on C0 at once and bound lengths."""
        self.assertEqual(_filtered(b"\x1b[12\x18x"), b"x")
        self.assertEqual(_filtered(b"\x1b[12\x1ax"), b"x")
        self.assertEqual(_filtered(b"\x1b[1\x1b[2Jx"), b"\x1b[2Jx")
        self.assertEqual(_filtered(b"\x1b[1\r;2H"), b"\r\x1b[1;2H")
        self.assertEqual(_filtered(b"\x1b[" + b"1" * 300 + b"Hx"), b"x")
        self.assertEqual(_filtered(b"\x1bPabc\x1b[2Jx"), b"\x1b[2Jx")
        self.assertEqual(_filtered(b"\x1b]0;t\x18x"), b"x")
        self.assertEqual(_filtered(b"\x1b\x1b[2J"), b"\x1b\x1b[2J")
        self.assertEqual(_filtered(b"\x1b\x1b[?4mx"), b"\x1bx")
        endless = b"\x1bP" + b"z" * 70000 + b"visible"
        self.assertTrue(_filtered(endless).endswith(b"visible"))
        self.assertLess(len(_filtered(endless)), 10000)

    def test_chunks_give_the_same_result(self) -> None:
        """Keep state across reads: byte-wise feeding equals one feed."""
        data = (DATA / "vim-xterm.bin").read_bytes() + b"\x1b[38:2::1:2:3m\x1bPq\x1b\\x\x1b]0;t\x07"
        whole = _filtered(data)
        sequence_filter = SequenceFilter()
        pieces = b"".join(sequence_filter.feed(data[index:index + 1]) for index in range(len(data)))
        self.assertEqual(pieces, whole)
        for size in (2, 3, 7, 64):
            sequence_filter = SequenceFilter()
            pieces = b"".join(
                sequence_filter.feed(data[index:index + size]) for index in range(0, len(data), size)
            )
            self.assertEqual(pieces, whole, size)


class ScrollRegionTests(unittest.TestCase):
    """Verify the CSI S and T scroll commands."""

    def _emulator(self) -> TerminalEmulator:
        emulator = TerminalEmulator(4, 5)
        emulator.feed(b"a\r\nb\r\nc\r\nd\r\ne\x1b[2;4r\x1b[3;2H")
        return emulator

    def _rows(self, emulator: TerminalEmulator) -> list[str]:
        return [line.rstrip() for line in emulator.screen.display]

    def test_scrolls_inside_the_margins(self) -> None:
        """Move rows 2-4 only and keep the cursor where it was."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[S")
        self.assertEqual(self._rows(emulator), ["a", "c", "d", "", "e"])
        self.assertEqual((emulator.screen.cursor.y, emulator.screen.cursor.x), (2, 1))
        emulator = self._emulator()
        emulator.feed(b"\x1b[2T")
        self.assertEqual(self._rows(emulator), ["a", "", "", "b", "e"])
        self.assertEqual((emulator.screen.cursor.y, emulator.screen.cursor.x), (2, 1))

    def test_bounds_the_count(self) -> None:
        """Clear the region at most, also for huge counts."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[9999S")
        self.assertEqual(self._rows(emulator), ["a", "", "", "", "e"])
        emulator = self._emulator()
        emulator.feed(b"\x1b[0T")
        self.assertEqual(self._rows(emulator), ["a", "", "b", "c", "e"])


class EmulatorTests(unittest.TestCase):
    """Verify the emulator with editor output."""

    def test_editor_screens_match_a_real_terminal(self) -> None:
        """Render recorded vim and nano output like tmux did."""
        for name in ("vim-xterm", "nano-xterm", "nano-cut-xterm"):
            with self.subTest(name=name):
                emulator = TerminalEmulator(80, 24)
                emulator.feed((DATA / f"{name}.bin").read_bytes())
                expected = (DATA / f"{name}.txt").read_text().splitlines()
                self.assertEqual([line.rstrip() for line in emulator.screen.display], expected)

    def test_vim_queries_change_nothing(self) -> None:
        """Neither fail on nor apply the modifyOtherKeys sequences of vim."""
        emulator = TerminalEmulator(20, 2)
        emulator.feed(b"a\x1b[?4m\x1b[>4;2mb\x1b[0%mc")
        self.assertEqual(emulator.screen.display[0].rstrip(), "abc")
        self.assertFalse(any(emulator.screen.buffer[0][x].underscore for x in range(3)))

    def test_survives_parser_errors(self) -> None:
        """Lose the failed chunk but keep parsing later output."""
        emulator = TerminalEmulator(20, 2)
        with patch.object(emulator.stream, "feed", side_effect=TypeError("broken")):
            emulator.feed(b"lost")
        emulator.feed(b"kept")
        self.assertEqual(emulator.screen.display[0].rstrip(), "kept")


if __name__ == "__main__":
    unittest.main()
