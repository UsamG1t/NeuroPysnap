"""Tests for the control sequences that reach ``pyte``."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from vboxlab.terminal.emulator import SequenceFilter, TerminalEmulator

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


class AlternateScreenTests(unittest.TestCase):
    """Verify the alternate screen of full-screen programs."""

    def _emulator(self) -> TerminalEmulator:
        emulator = TerminalEmulator(6, 3, history=50)
        emulator.feed(b"s1\r\ns2\r\ns3\x1b[2;2H")
        return emulator

    def _rows(self, emulator: TerminalEmulator) -> list[str]:
        return [line.rstrip() for line in emulator.screen.display]

    def _cursor(self, emulator: TerminalEmulator) -> tuple[int, int]:
        return emulator.screen.cursor.y, emulator.screen.cursor.x

    def test_1049_switches_and_restores_the_cursor(self) -> None:
        """Show a clear screen, keep its lines out of the history, come back."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[?1049h")
        self.assertTrue(emulator.screen.in_alternate_screen)
        self.assertEqual(self._rows(emulator), ["", "", ""])
        emulator.feed(b"\x1b[Ha\r\nb\r\nc\r\nd\r\ne")
        self.assertEqual(self._rows(emulator), ["c", "d", "e"])
        self.assertEqual(len(emulator.screen.history.top), 0)
        emulator.feed(b"\x1b[?1049l")
        self.assertFalse(emulator.screen.in_alternate_screen)
        self.assertEqual(self._rows(emulator), ["s1", "s2", "s3"])
        self.assertEqual(self._cursor(emulator), (1, 1))

    def test_1047_and_47_keep_the_cursor_1048_only_saves_it(self) -> None:
        """Switch without restoring the cursor; 1048 restores it alone."""
        for mode in (b"47", b"1047"):
            with self.subTest(mode=mode):
                emulator = self._emulator()
                emulator.feed(b"\x1b[?" + mode + b"h\x1b[3;5Hx\x1b[?" + mode + b"l")
                self.assertEqual(self._rows(emulator), ["s1", "s2", "s3"])
                self.assertEqual(self._cursor(emulator), (2, 5))
        emulator = self._emulator()
        emulator.feed(b"\x1b[?1048h\x1b[3;5H\x1b[?1048l")
        self.assertEqual(self._cursor(emulator), (1, 1))
        self.assertFalse(emulator.screen.in_alternate_screen)

    def test_other_modes_in_the_same_sequence_still_apply(self) -> None:
        """Pass the remaining modes of a combined sequence on."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[?25l\x1b[?1049;25h")
        self.assertTrue(emulator.screen.in_alternate_screen)
        self.assertFalse(emulator.screen.cursor.hidden)
        emulator.feed(b"\x1b[?1049;25l")
        self.assertFalse(emulator.screen.in_alternate_screen)
        self.assertTrue(emulator.screen.cursor.hidden)

    def test_repeated_switches_and_reset(self) -> None:
        """Ignore a second switch and leave the alternate screen on reset."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[?1049h\x1b[?1049hx\x1b[?1049l")
        self.assertEqual(self._rows(emulator), ["s1", "s2", "s3"])
        emulator.feed(b"\x1b[?1049l")
        self.assertEqual(self._rows(emulator), ["s1", "s2", "s3"])
        emulator.feed(b"\x1b[?1049h\x1bc")
        self.assertFalse(emulator.screen.in_alternate_screen)

    def test_reverse_scroll_on_the_alternate_screen(self) -> None:
        """Keep reverse scrolling out of the history too."""
        emulator = self._emulator()
        emulator.feed(b"\x1b[?1049h\x1b[Ha\r\x1bM\x1bMb")
        self.assertEqual(self._rows(emulator), ["b", "", "a"])
        self.assertEqual(len(emulator.screen.history.bottom), 0)

    def test_no_local_scrollback_on_the_alternate_screen(self) -> None:
        """Leave the editor screen in place on Alt-Up; scroll the shell again after."""
        emulator = TerminalEmulator(6, 3, history=50)
        emulator.feed(b"".join(b"l%d\r\n" % index for index in range(10)))
        emulator.feed(b"\x1b[?1049hed")
        emulator.scroll_up(2)
        emulator.scroll_to_top()
        self.assertEqual(self._rows(emulator), ["", "", "ed"])
        self.assertFalse(emulator.is_scrollback_active)
        emulator.feed(b"\x1b[?1049l")
        emulator.scroll_up(2)
        self.assertEqual(self._rows(emulator), ["l6", "l7", "l8"])
        emulator.scroll_to_top()
        self.assertEqual(self._rows(emulator)[0], "l0")

    def test_resize_keeps_the_shell_screen(self) -> None:
        """Move shell lines to the history and cut cells when shrinking."""
        emulator = TerminalEmulator(6, 3, history=50)
        emulator.feed(b"abcdef\r\nline2\r\nline3\x1b[?1049h")
        emulator.resize(4, 2)
        emulator.feed(b"\x1b[?1049l")
        self.assertEqual(self._rows(emulator), ["line", "line"])
        self.assertEqual("".join(cell.data for cell in emulator.screen.history.top[-1].values()).rstrip(), "abcdef")
        self.assertEqual(emulator.screen.buffer[0][4].data, " ")


class EmulatorTests(unittest.TestCase):
    """Verify the emulator with editor output."""

    def test_editor_screens_match_a_real_terminal(self) -> None:
        """Render recorded vim and nano output like tmux did."""
        for name in ("vim-xterm", "nano-xterm", "nano-cut-xterm", "vim-exit-xterm", "less-exit-xterm"):
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
