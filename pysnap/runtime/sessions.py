"""Persistent session registry used by terminal connections."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterator


@dataclass(frozen=True)
class SessionRecord:
    """Represent one live terminal session."""

    vm_name: str
    serial_port: int
    pid: int
    attached_at: str


class SessionRegistry:
    """Store terminal session metadata in a temp-directory registry."""

    WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WINDOWS_STILL_ACTIVE = 259
    WINDOWS_ERROR_ACCESS_DENIED = 5

    def __init__(self, root_dir: Path | None = None) -> None:
        """Initialize the session registry.

        :param root_dir: Optional custom root directory.
        """
        self.root_dir = root_dir or (Path(tempfile.gettempdir()) / "pysnap-sessions")
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def list_live_sessions(self) -> dict[str, SessionRecord]:
        """Return live session records keyed by VM name.

        :returns: Live session records.
        """
        sessions: dict[str, SessionRecord] = {}
        for record_path in sorted(self.root_dir.glob("*.json")):
            record = self._read_record(record_path)
            if record is None:
                continue
            if not self._pid_is_alive(record.pid):
                record_path.unlink(missing_ok=True)
                continue
            sessions[record.vm_name] = record
        return sessions

    def get_live_session(self, vm_name: str) -> SessionRecord | None:
        """Return one live session for the specified VM.

        :param vm_name: VM name to inspect.
        :returns: Live session record or ``None``.
        """
        return self.list_live_sessions().get(vm_name)

    @contextmanager
    def register(self, vm_name: str, serial_port: int) -> Iterator[SessionRecord]:
        """Register a session for the duration of a context manager.

        :param vm_name: VM name.
        :param serial_port: Attached serial TCP port.
        :yields: Persisted session record.
        """
        record = SessionRecord(
            vm_name=vm_name,
            serial_port=serial_port,
            pid=os.getpid(),
            attached_at=datetime.now(timezone.utc).isoformat(),
        )
        path = self._record_path(vm_name)
        path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        try:
            yield record
        finally:
            path.unlink(missing_ok=True)

    def _record_path(self, vm_name: str) -> Path:
        """Return the path used to store one VM session.

        :param vm_name: VM name.
        :returns: Path to the session file.
        """
        digest = hashlib.sha1(vm_name.encode("utf-8"), usedforsecurity=False).hexdigest()
        safe_name = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in vm_name
        ).strip("_")
        label = safe_name[:32] or "vm"
        return self.root_dir / f"{label}-{digest[:12]}.json"

    def _read_record(self, path: Path) -> SessionRecord | None:
        """Read one record from disk.

        :param path: Path to the session file.
        :returns: Parsed record or ``None`` when invalid.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return SessionRecord(
                vm_name=str(payload["vm_name"]),
                serial_port=int(payload["serial_port"]),
                pid=int(payload["pid"]),
                attached_at=str(payload["attached_at"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None

    def _pid_is_alive(self, pid: int) -> bool:
        """Check whether a process id is still alive.

        :param pid: Process identifier.
        :returns: ``True`` when the process appears to be alive.
        """
        if pid <= 0:
            return False
        if sys.platform == "win32":
            return self._pid_is_alive_windows(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _pid_is_alive_windows(self, pid: int) -> bool:
        """Check process liveness through the Windows process API.

        ``os.kill(pid, 0)`` is not a harmless existence probe on Windows, so
        session monitoring uses ``OpenProcess`` and ``GetExitCodeProcess``
        instead.

        :param pid: Process identifier.
        :returns: ``True`` when the process still appears to be alive.
        """
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False

        kernel32 = windll.kernel32
        process_handle = kernel32.OpenProcess(
            self.WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process_handle:
            return kernel32.GetLastError() == self.WINDOWS_ERROR_ACCESS_DENIED

        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
                return kernel32.GetLastError() == self.WINDOWS_ERROR_ACCESS_DENIED
            return exit_code.value == self.WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(process_handle)
