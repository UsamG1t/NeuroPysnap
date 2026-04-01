"""Unit tests for the persistent runtime session registry."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pysnap.runtime.sessions import SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    """Verify live session persistence and cleanup."""

    class FakeKernel32:
        """Provide a tiny fake ``kernel32`` API surface for Windows PID checks."""

        def __init__(
            self,
            *,
            process_handle: int = 1,
            last_error: int = 0,
            exit_code: int = SessionRegistry.WINDOWS_STILL_ACTIVE,
            get_exit_code_result: int = 1,
        ) -> None:
            """Initialize fake WinAPI responses."""
            self.process_handle = process_handle
            self.last_error = last_error
            self.exit_code = exit_code
            self.get_exit_code_result = get_exit_code_result
            self.open_process_calls: list[tuple[int, bool, int]] = []
            self.close_handle_calls: list[int] = []

        def OpenProcess(self, access: int, inherit_handle: bool, pid: int) -> int:
            """Return a configured process handle."""
            self.open_process_calls.append((access, inherit_handle, pid))
            return self.process_handle

        def GetLastError(self) -> int:
            """Return the configured WinAPI error code."""
            return self.last_error

        def GetExitCodeProcess(self, handle: int, exit_code_pointer) -> int:
            """Write a configured process exit code into the provided pointer."""
            exit_code_pointer._obj.value = self.exit_code
            return self.get_exit_code_result

        def CloseHandle(self, handle: int) -> int:
            """Record one handle-close request."""
            self.close_handle_calls.append(handle)
            return 1

    def test_register_exposes_session_while_context_is_active(self) -> None:
        """Keep a session visible only for the duration of the context."""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = SessionRegistry(root_dir=Path(temp_dir))

            with registry.register("srv", 2345):
                session = registry.get_live_session("srv")
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.serial_port, 2345)

            self.assertIsNone(registry.get_live_session("srv"))

    def test_stale_pid_record_is_removed(self) -> None:
        """Drop dead session records when their process no longer exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = SessionRegistry(root_dir=Path(temp_dir))
            record_path = registry._record_path("ghost")
            record_path.write_text(
                (
                    '{\n'
                    '  "vm_name": "ghost",\n'
                    '  "serial_port": 2345,\n'
                    '  "pid": 999999,\n'
                    '  "attached_at": "2026-03-26T00:00:00+00:00"\n'
                    '}\n'
                ),
                encoding="utf-8",
            )

            sessions = registry.list_live_sessions()

            self.assertEqual(sessions, {})
            self.assertFalse(record_path.exists())

    def test_register_exposes_session_while_context_is_active_on_windows(self) -> None:
        """Keep a Windows session visible without probing PID liveness via ``os.kill``."""
        fake_kernel32 = self.FakeKernel32()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("pysnap.runtime.sessions.sys.platform", "win32"),
            patch(
                "pysnap.runtime.sessions.ctypes.windll",
                SimpleNamespace(kernel32=fake_kernel32),
                create=True,
            ),
            patch(
                "pysnap.runtime.sessions.os.kill",
                side_effect=AssertionError("os.kill must not be used on Windows"),
            ),
        ):
            registry = SessionRegistry(root_dir=Path(temp_dir))

            with registry.register("srv", 2345):
                session = registry.get_live_session("srv")
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.serial_port, 2345)

        self.assertEqual(len(fake_kernel32.open_process_calls), 1)
        self.assertEqual(fake_kernel32.close_handle_calls, [1])

    def test_pid_is_alive_windows_returns_false_for_missing_process(self) -> None:
        """Treat missing Windows processes as stale session records."""
        fake_kernel32 = self.FakeKernel32(process_handle=0, last_error=87)
        with (
            patch("pysnap.runtime.sessions.sys.platform", "win32"),
            patch(
                "pysnap.runtime.sessions.ctypes.windll",
                SimpleNamespace(kernel32=fake_kernel32),
                create=True,
            ),
        ):
            registry = SessionRegistry()
            self.assertFalse(registry._pid_is_alive(999999))

    def test_pid_is_alive_windows_treats_access_denied_as_alive(self) -> None:
        """Preserve sessions when Windows denies querying another live process."""
        fake_kernel32 = self.FakeKernel32(process_handle=0, last_error=5)
        with (
            patch("pysnap.runtime.sessions.sys.platform", "win32"),
            patch(
                "pysnap.runtime.sessions.ctypes.windll",
                SimpleNamespace(kernel32=fake_kernel32),
                create=True,
            ),
        ):
            registry = SessionRegistry()
            self.assertTrue(registry._pid_is_alive(1234))


if __name__ == "__main__":
    unittest.main()
