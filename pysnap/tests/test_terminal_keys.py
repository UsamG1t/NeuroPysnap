"""Tests for the keys, the mouse wheel and copying in pysnap connect."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pysnap.terminal.emulator import TerminalEmulator
from pysnap.terminal.session import SessionStatus, SessionTransport, TerminalSession
from pysnap.tests.test_connect_ssh import _Service, _Writer

WHEEL_UP = b"\x1b[<64;5;2M"
WHEEL_DOWN = b"\x1b[<65;5;2M"


class _Session:
    """Run a session on scripted guest output and send it keys."""

    def __init__(self, output: bytes = b"", *, paused: bool = False) -> None:
        self.output = output
        self.paused = paused
        self.writer = _Writer()
        self.messages: list[str] = []
        self.scrolls: list[tuple[str, int]] = []
        self.copies: list[str] = []
        self.emulator: TerminalEmulator | None = None

    def run(self, *steps: bytes, pause: float = 0.15) -> "_Session":
        messages, scrolls, session = self.messages, self.scrolls, self

        class Status(SessionStatus):
            def __setattr__(self, name, value):
                if name == "message":
                    messages.append(value)
                super().__setattr__(name, value)

        class Emulator(TerminalEmulator):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                session.emulator = self

            def scroll_up(self, lines=1):
                scrolls.append(("up", lines))
                super().scroll_up(lines)

            def scroll_down(self, lines=1):
                scrolls.append(("down", lines))
                super().scroll_down(lines)

            def scroll_to_top(self):
                scrolls.append(("top", 0))

            def scroll_to_bottom(self):
                scrolls.append(("bottom", 0))

        async def scenario() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(self.output)

            async def opener(columns: int, rows: int):
                return reader, self.writer, None

            transport = SessionTransport("ssh", "ssh:1", 1, False, opener)
            with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
                async def type_keys() -> None:
                    await asyncio.sleep(0.3)
                    for step in steps:
                        pipe_input.send_bytes(step)
                        await asyncio.sleep(pause)
                    reader.feed_eof()

                typing = asyncio.ensure_future(type_keys())
                state = "Paused" if self.paused else "Working"
                await TerminalSession(service=_Service([state]))._run_async(
                    "vm", transport, paused=self.paused
                )
                typing.cancel()

        def copy(text, output=None):
            self.copies.append(text)
            return True

        with patch("pysnap.terminal.session.SessionStatus", Status), \
                patch("pysnap.terminal.session.TerminalEmulator", Emulator), \
                patch("pysnap.terminal.session.copy_text_to_host_clipboard", copy):
            asyncio.run(scenario())
        return self

    @property
    def sent(self) -> bytes:
        return bytes(self.writer.data)


class KeyTests(unittest.TestCase):
    """Verify which keys reach the guest and which belong to PySnap."""

    def test_editor_keys_reach_the_guest(self) -> None:
        """Forward Ctrl-Q, Ctrl-L, Esc and Alt+arrows unchanged."""
        session = _Session().run(b"\x11", b"\x0c", b"\x1b", b"\x1b[1;3A", b"\x1b\x1b[B", b"\x03", b"q")
        self.assertEqual(session.sent, b"\x11\x0c\x1b\x1b[1;3A\x1b\x1b[B\x03q")
        self.assertNotIn("Detached.", session.messages)
        self.assertEqual(session.scrolls, [])

    def test_command_mode(self) -> None:
        """Ctrl-] q detaches; Ctrl-] Ctrl-] sends Ctrl-]; other keys only leave the mode."""
        session = _Session().run(b"\x1d", b"\x1d", b"\x1d", b"x", b"\x1d", b"\x03", b"\x1d", b"\x1b", b"a", b"\x1d", b"q", b"z")
        self.assertEqual(session.sent, b"\x1da")
        self.assertIn("q detach, Up/Down/PgUp/PgDn/Home/End scroll, Esc back", session.messages)
        self.assertIn("Detached.", session.messages)

    def test_command_mode_scrolls(self) -> None:
        """Scroll the local history with arrows, pages, Home and End."""
        session = _Session().run(b"\x1d", b"\x1b[A", b"\x1b[B", b"\x1b[5~", b"\x1b[6~", b"\x1b[H", b"\x1b[F", b"\x1b")
        lines = session.emulator.screen.lines
        self.assertEqual(session.scrolls, [("up", 1), ("down", 1), ("up", lines), ("down", lines), ("top", 0), ("bottom", 0)])
        self.assertEqual(session.sent, b"")


    def test_paused_vm_gets_no_keys(self) -> None:
        """Hold back keys, Ctrl-C and Ctrl-] Ctrl-] while paused; commands still work."""
        session = _Session(paused=True).run(b"a", b"\x03", b"\x1d", b"\x1d", b"\x1d", b"q")
        self.assertEqual(session.sent, b"")
        self.assertIn("Detached.", session.messages)


class WheelTests(unittest.TestCase):
    """Verify the mouse wheel on the shell and on full-screen programs."""

    def test_wheel_scrolls_the_local_history(self) -> None:
        """Move three lines per step on the shell screen."""
        with patch("pysnap.terminal.session._should_enable_mouse_scrolling", return_value=True):
            session = _Session(b"x\r\n" * 60).run(WHEEL_UP, WHEEL_DOWN)
        self.assertEqual(session.scrolls, [("up", 3), ("down", 3)])
        self.assertEqual(session.sent, b"")

    def test_wheel_sends_cursor_keys_to_full_screen_programs(self) -> None:
        """Send three cursor keys in the mode the guest asked for."""
        with patch("pysnap.terminal.session._should_enable_mouse_scrolling", return_value=True):
            normal = _Session(b"\x1b[?1049h").run(WHEEL_UP, WHEEL_DOWN)
            application = _Session(b"\x1b[?1049h\x1b[?1h").run(WHEEL_DOWN)
        self.assertEqual(normal.sent, b"\x1b[A" * 3 + b"\x1b[B" * 3)
        self.assertEqual(application.sent, b"\x1bOB" * 3)
        self.assertEqual(normal.scrolls, [])
        with patch("pysnap.terminal.session._should_enable_mouse_scrolling", return_value=True):
            paused = _Session(b"\x1b[?1049h", paused=True).run(WHEEL_DOWN)
        self.assertEqual(paused.sent, b"")


class CopyTests(unittest.TestCase):
    """Verify copying selected text to the host."""

    def test_release_copies_and_ctrl_c_copies_again(self) -> None:
        """Copy when the button is released; Ctrl-C then copies instead of interrupting."""
        drag = b"\x1b[<0;1;1M\x1b[<32;5;1M\x1b[<0;5;1m"
        session = _Session(b"hello world").run(drag, b"\x03", b"\x03")
        self.assertEqual(session.copies, ["hello", "hello"])
        self.assertIn("Selection copied.", session.messages)
        self.assertEqual(session.sent, b"\x03")


if __name__ == "__main__":
    unittest.main()
