"""Tests for reading the host clipboard and for pasted text."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from pysnap.terminal import clipboard
from pysnap.terminal.emulator import TerminalEmulator
from pysnap.terminal.keymap import paste_to_bytes


class PasteBytesTests(unittest.TestCase):
    """Verify the bytes sent for pasted text."""

    def test_line_breaks_become_enter(self) -> None:
        """Send CR for CR LF, LF and CR, as a terminal does."""
        self.assertEqual(paste_to_bytes("a\r\nb\nc\rd", bracketed=False), b"a\rb\rc\rd")
        self.assertEqual(paste_to_bytes("привет", bracketed=False), "привет".encode())

    def test_bracketed_paste_marks_the_text(self) -> None:
        """Wrap the text and drop markers inside it."""
        self.assertEqual(paste_to_bytes("ls\r\n", bracketed=True), b"\x1b[200~ls\r\x1b[201~")
        self.assertEqual(
            paste_to_bytes("a\x1b[201~rm -rf x\n\x1b[200~b", bracketed=True),
            b"\x1b[200~arm -rf x\rb\x1b[201~",
        )

    def test_emulator_follows_the_guest_mode(self) -> None:
        """Report bracketed paste while the guest has mode 2004 on."""
        emulator = TerminalEmulator(10, 2)
        self.assertFalse(emulator.bracketed_paste)
        emulator.feed(b"\x1b[?2004h")
        self.assertTrue(emulator.bracketed_paste)
        emulator.feed(b"\x1b[?2004l")
        self.assertFalse(emulator.bracketed_paste)


class ReadClipboardTests(unittest.TestCase):
    """Verify how the host clipboard is read outside Windows."""

    def _read(self, installed, answers, *, platform="linux", wayland=False):
        calls = []

        def run(command, **kwargs):
            calls.append(command[0])
            answer = answers[command[0]]
            if isinstance(answer, BaseException):
                raise answer
            return subprocess.CompletedProcess(command, answer[0], answer[1], answer[2])

        environment = {"WAYLAND_DISPLAY": "wayland-0"} if wayland else {}
        with patch("pysnap.terminal.clipboard.sys.platform", platform), \
                patch.dict("pysnap.terminal.clipboard.os.environ", environment, clear=True), \
                patch("pysnap.terminal.clipboard.shutil.which",
                      side_effect=lambda name: f"/usr/bin/{name}" if name in installed else None), \
                patch("pysnap.terminal.clipboard.subprocess.run", side_effect=run):
            try:
                return clipboard.read_host_clipboard(), calls
            except clipboard.ClipboardError as error:
                return error, calls

    def test_uses_the_first_working_program(self) -> None:
        """Prefer wl-paste under Wayland and fall back to the next program."""
        text, calls = self._read({"wl-paste", "xclip"}, {
            "wl-paste": (1, b"", b"no compositor"), "xclip": (0, "текст".encode(), b""),
        }, wayland=True)
        self.assertEqual((text, calls), ("текст", ["wl-paste", "xclip"]))
        text, calls = self._read({"wl-paste", "xclip"}, {"xclip": (0, b"x11", b"")})
        self.assertEqual((text, calls), ("x11", ["xclip"]))
        text, calls = self._read({"pbpaste", "xclip"}, {"pbpaste": (0, b"mac", b"")}, platform="darwin")
        self.assertEqual((text, calls), ("mac", ["pbpaste"]))

    def test_explains_failures(self) -> None:
        """Name the missing programs or the errors of the installed ones."""
        error, calls = self._read(set(), {})
        self.assertIsInstance(error, clipboard.ClipboardError)
        self.assertIn("install one of: xclip, xsel", str(error))
        self.assertEqual(calls, [])
        error, _ = self._read({"xclip", "xsel"}, {
            "xclip": (1, b"", b"Can't open display"),
            "xsel": subprocess.TimeoutExpired("xsel", 3),
        })
        self.assertIn("xclip: Can't open display", str(error))
        self.assertIn("xsel:", str(error))


if __name__ == "__main__":
    unittest.main()
