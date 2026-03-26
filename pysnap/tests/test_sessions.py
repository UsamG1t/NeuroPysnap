"""Unit tests for the persistent runtime session registry."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pysnap.runtime.sessions import SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    """Verify live session persistence and cleanup."""

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


if __name__ == "__main__":
    unittest.main()
