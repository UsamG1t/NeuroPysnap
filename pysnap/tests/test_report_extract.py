"""Tests for copying report files from a VM through its serial console."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

from pysnap.core.models import VMInfo
from pysnap.report.extract import ExtractError, extract_file, remote_shell_path

REPORT_PS1 = "[\\u@01-first \\W]# "


class _Service:
    """Provide the two service calls used by the transfer."""

    def __init__(self, *, state="running", port=2326, live=False) -> None:
        self.vm_info = VMInfo(name="first", uuid="u", groups=("/Lab",), serial_port=port, vm_state=state)
        self.session_registry = SimpleNamespace(get_live_session=lambda name: object() if live else None)

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return the fake VM."""
        return self.vm_info


class _ShellServer:
    """Serve an interactive bash on a PTY through TCP, like a VirtualBox UART."""

    def __init__(self, home: Path, prompt: str = "[root@first ~]# ", silent: bool = False) -> None:
        self.home = home
        self.prompt = prompt
        self.silent = silent
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = bytearray()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "_ShellServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.listener.close()
        self.thread.join(timeout=5)
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=5)

    def connect(self, host: str, port: int, timeout: float) -> socket.socket:
        """Connect like ``open_serial_socket`` would."""
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

    def _serve(self) -> None:
        try:
            client, _ = self.listener.accept()
        except OSError:
            return
        if self.silent:
            with client:
                while client.recv(4096):
                    pass
            return
        master, slave = os.openpty()
        environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PS1": self.prompt,
            "TERM": "vt220",
            "HISTFILE": str(self.home / ".bash_history"),
            "HISTCONTROL": "ignoreboth",
        }
        self.process = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "-i"],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=self.home, env=environment, start_new_session=True,
        )
        os.close(slave)
        with client:
            while True:
                ready, _, _ = select.select([client, master], [], [], 0.1)
                try:
                    if client in ready:
                        data = client.recv(65536)
                        if not data:
                            break
                        self.received += data
                        os.write(master, data)
                    if master in ready:
                        client.sendall(os.read(master, 65536))
                except OSError:
                    break
        os.close(master)


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("base64"),
    "bash and coreutils are required",
)
class ExtractWithShellTests(unittest.TestCase):
    """Run transfers against a real shell."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        self.guest = root / "guest"
        self.host = root / "host"
        self.guest.mkdir()
        self.host.mkdir()
        self.content = os.urandom(20_000) + b"\x00\r\n end"
        (self.guest / "report.01.first").write_bytes(self.content)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _extract(self, server: _ShellServer, name: str = "report.01.first", **kwargs):
        kwargs.setdefault("destination", self.host / "report.01.first")
        return extract_file(
            _Service(), "first", name, connector=server.connect,
            shell_timeout=3.0, idle_timeout=5.0, **kwargs,
        )

    def test_copies_a_file_from_the_home_directory(self) -> None:
        """Transfer the exact bytes and report their checksum."""
        with _ShellServer(self.guest) as server:
            result = self._extract(server)

        written = (self.host / "report.01.first").read_bytes()
        self.assertEqual(written, self.content)
        self.assertEqual(result.size, len(self.content))
        self.assertEqual(result.sha256, hashlib.sha256(self.content).hexdigest())
        self.assertEqual(result.remote_path, '"$HOME"/report.01.first')
        commands = [part for part in bytes(server.received).split(b"\r") if part]
        self.assertEqual(len(commands), 3)  # shell check, transfer, clear
        self.assertTrue(commands[0].startswith(b" echo PYSNAP_"))
        self.assertTrue(commands[1].startswith(b" f="))
        self.assertEqual(commands[2], b" clear")
        # A leading space keeps the commands out of the history (HISTCONTROL).
        self.assertTrue(all(command.startswith(b" ") for command in commands))

    def test_copies_a_file_by_path_with_spaces(self) -> None:
        """Quote explicit paths for the shell."""
        folder = self.guest / "my reports"
        folder.mkdir()
        (folder / "report.02.x").write_bytes(b"data")
        with _ShellServer(self.guest) as server:
            self._extract(server, name=str(folder / "report.02.x"), destination=self.host / "copy")

        self.assertEqual((self.host / "copy").read_bytes(), b"data")

    def test_reports_missing_files(self) -> None:
        """Explain that the file does not exist in the VM."""
        with _ShellServer(self.guest) as server:
            with self.assertRaisesRegex(ExtractError, "not found or is not readable"):
                self._extract(server, name="report.09.none")
        self.assertFalse((self.host / "report.01.first").exists())

    def test_refuses_while_a_report_is_recorded(self) -> None:
        """Stop after one Enter when the report prompt is visible."""
        with _ShellServer(self.guest, prompt=REPORT_PS1) as server:
            with self.assertRaisesRegex(ExtractError, "report recording is running"):
                self._extract(server)
        self.assertEqual(bytes(server.received), b"\r")

    def test_refuses_to_replace_files_without_force(self) -> None:
        """Keep existing host files unless ``force`` is given."""
        target = self.host / "report.01.first"
        target.write_bytes(b"old")
        with self.assertRaisesRegex(ExtractError, "already exists; use --force"):
            extract_file(_Service(), "first", "report.01.first", destination=target)

        with _ShellServer(self.guest) as server:
            self._extract(server, destination=target, force=True)
        self.assertEqual(target.read_bytes(), self.content)

    def test_times_out_without_a_shell(self) -> None:
        """Explain a console that does not answer the shell check."""
        with _ShellServer(self.guest, silent=True) as server:
            with self.assertRaisesRegex(ExtractError, "not at a shell prompt"):
                extract_file(
                    _Service(), "first", "report.01.first",
                    destination=self.host / "x", connector=server.connect, shell_timeout=0.5,
                )


class ExtractPreconditionTests(unittest.TestCase):
    """Verify checks made before connecting."""

    def test_requires_a_running_vm_with_a_free_serial_port(self) -> None:
        """Refuse stopped VMs, VMs without a port and attached sessions."""
        cases = [
            (_Service(state="poweroff"), "is not running"),
            (_Service(port=None), "has no TCP serial port"),
            (_Service(live=True), "detach with Ctrl-Q"),
        ]
        for service, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                with self.assertRaisesRegex(ExtractError, message):
                    extract_file(service, "first", "report.01.first", destination=Path(temp_dir) / "r",
                                 connector=lambda *args: self.fail("must not connect"))

    def test_quotes_remote_paths(self) -> None:
        """Look up bare names in the home directory and quote paths."""
        self.assertEqual(remote_shell_path("report.01.first"), '"$HOME"/report.01.first')
        self.assertEqual(remote_shell_path("/root/my report"), "'/root/my report'")
        self.assertEqual(remote_shell_path("a'b"), '"$HOME"/\'a\'"\'"\'b\'')
        for name in ("", "bad\nname", "dir/"):
            with self.subTest(name=name), self.assertRaises(ExtractError):
                remote_shell_path(name)


if __name__ == "__main__":
    unittest.main()
