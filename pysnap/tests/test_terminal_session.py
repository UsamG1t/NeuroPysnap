"""Unit tests for terminal session shutdown behavior."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from pysnap.core.models import VMInfo
from pysnap.terminal.session import (
    TerminalSession,
    _safe_exit_application,
    _should_use_full_screen,
    _wake_serial_console,
)


class FakeApplication:
    """Provide a small fake prompt-toolkit application."""

    def __init__(
        self,
        *,
        is_running: bool,
        is_done: bool,
        exit_error: Exception | None = None,
    ) -> None:
        """Initialize fake application state."""
        self.is_running = is_running
        self.is_done = is_done
        self.exit_error = exit_error
        self.exit_calls = 0

    def exit(self) -> None:
        """Record one exit attempt and raise a configured error when needed."""
        self.exit_calls += 1
        if self.exit_error is not None:
            raise self.exit_error
        self.is_done = True
        self.is_running = False


class TerminalSessionTests(unittest.TestCase):
    """Verify safe application shutdown helpers."""

    def test_should_use_full_screen_by_default(self) -> None:
        """Keep alternate-screen rendering in ordinary terminals."""
        with (
            patch("pysnap.terminal.session.sys.platform", "linux"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(_should_use_full_screen())

    def test_should_disable_full_screen_for_windows_git_bash(self) -> None:
        """Avoid alternate-screen mode for Git Bash terminals on Windows."""
        with (
            patch("pysnap.terminal.session.sys.platform", "win32"),
            patch.dict(os.environ, {"MSYSTEM": "MINGW64"}, clear=True),
        ):
            self.assertFalse(_should_use_full_screen())

    def test_run_swallows_keyboard_interrupt_from_terminal_runtime(self) -> None:
        """Return cleanly when the surrounding terminal runtime interrupts the app."""

        class FakeService:
            """Provide only the VM lookup required by ``TerminalSession.run``."""

            def prepare_vm_connection(self, vm_name: str) -> VMInfo:
                """Return a ready VM description."""
                return VMInfo(
                    name=vm_name,
                    uuid="uuid-srv",
                    groups=("/Lab",),
                    serial_port=2326,
                    vm_state="running",
                )

        session = TerminalSession(service=FakeService())

        def raising_run(coroutine):
            coroutine.close()
            raise KeyboardInterrupt()

        with patch("pysnap.terminal.session.asyncio.run", side_effect=raising_run):
            self.assertEqual(session.run("srv"), 0)

    def test_run_swallows_cancelled_error_from_terminal_runtime(self) -> None:
        """Return cleanly when prompt-toolkit cancels the session task."""

        class FakeService:
            """Provide only the VM lookup required by ``TerminalSession.run``."""

            def prepare_vm_connection(self, vm_name: str) -> VMInfo:
                """Return a ready VM description."""
                return VMInfo(
                    name=vm_name,
                    uuid="uuid-srv",
                    groups=("/Lab",),
                    serial_port=2326,
                    vm_state="running",
                )

        session = TerminalSession(service=FakeService())

        def raising_run(coroutine):
            coroutine.close()
            raise asyncio.CancelledError()

        with patch("pysnap.terminal.session.asyncio.run", side_effect=raising_run):
            self.assertEqual(session.run("srv"), 0)

    def test_wake_serial_console_sends_initial_newline(self) -> None:
        """Prompt the guest serial console to emit its current login screen."""

        class FakeWriter:
            """Collect bytes written to the transport."""

            def __init__(self) -> None:
                """Initialize the writer state."""
                self.chunks: list[bytes] = []
                self.drained = False

            def write(self, payload: bytes) -> None:
                """Collect one payload chunk."""
                self.chunks.append(payload)

            async def drain(self) -> None:
                """Record one successful drain."""
                self.drained = True

        writer = FakeWriter()
        asyncio.run(_wake_serial_console(writer))

        self.assertEqual(writer.chunks, [b"\r\n"])
        self.assertTrue(writer.drained)

    def test_safe_exit_exits_running_application_once(self) -> None:
        """Exit a live application successfully."""
        app = FakeApplication(is_running=True, is_done=False)

        _safe_exit_application(app)

        self.assertEqual(app.exit_calls, 1)
        self.assertFalse(app.is_running)
        self.assertTrue(app.is_done)

    def test_safe_exit_ignores_already_stopped_application(self) -> None:
        """Ignore exit requests after the application has already stopped."""
        app = FakeApplication(is_running=False, is_done=False)

        _safe_exit_application(app)

        self.assertEqual(app.exit_calls, 0)

    def test_safe_exit_ignores_completed_application(self) -> None:
        """Ignore exit requests when the application result is already set."""
        app = FakeApplication(is_running=True, is_done=True)

        _safe_exit_application(app)

        self.assertEqual(app.exit_calls, 0)

    def test_safe_exit_swallows_benign_prompt_toolkit_shutdown_error(self) -> None:
        """Suppress the known prompt-toolkit race during repeated shutdown."""
        app = FakeApplication(
            is_running=True,
            is_done=False,
            exit_error=Exception("Application is not running. Application.exit() failed."),
        )

        _safe_exit_application(app)

        self.assertEqual(app.exit_calls, 1)

    def test_safe_exit_swallows_return_value_already_set_error(self) -> None:
        """Suppress the known duplicate-exit race once the future is resolved."""
        app = FakeApplication(
            is_running=True,
            is_done=False,
            exit_error=Exception("Return value already set. Application.exit() failed."),
        )

        _safe_exit_application(app)

        self.assertEqual(app.exit_calls, 1)

    def test_safe_exit_preserves_unexpected_errors(self) -> None:
        """Propagate unexpected shutdown failures for visibility."""
        app = FakeApplication(
            is_running=True,
            is_done=False,
            exit_error=RuntimeError("boom"),
        )

        with self.assertRaises(RuntimeError):
            _safe_exit_application(app)


if __name__ == "__main__":
    unittest.main()
