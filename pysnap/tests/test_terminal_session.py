"""Unit tests for terminal session shutdown behavior."""

from __future__ import annotations

import unittest

from pysnap.terminal.session import _safe_exit_application


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
