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
from unittest.mock import patch

from vboxlab.core.access import AccessInfo
from vboxlab.core.models import VMInfo
from vboxlab.report.extract import ExtractError, extract_file, remote_shell_path
from vboxlab.runtime.sessions import SessionRecord
from vboxlab.ssh.client import SshTarget
from vboxlab.tests.test_ssh import HAVE_OPENSSH, SSHD, SshdFixture

REPORT_PS1 = "[\\u@01-first \\W]# "


class _Service:
    """Provide the two service calls used by the transfer."""

    def __init__(self, *, state="running", port=2326, session=None) -> None:
        self.vm_info = VMInfo(name="first", uuid="u", groups=("/Lab",), serial_port=port, vm_state=state)
        self.session_registry = SimpleNamespace(get_live_session=lambda name: session)

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return the fake VM."""
        return self.vm_info

    def get_vm_access(self, vm_name: str) -> AccessInfo:
        """Reach the fake VM through COM1."""
        return AccessInfo("com", "protosettings", self.vm_info.serial_port, None, "root")


class _ShellServer:
    """Serve an interactive bash on a PTY through TCP, like a VirtualBox UART."""

    def __init__(
        self, home: Path, prompt: str = "[root@first ~]# ", silent: bool = False, typed: bytes = b""
    ) -> None:
        self.home = home
        self.prompt = prompt
        self.silent = silent
        self.typed = typed
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
        if self.typed:
            # A line typed in the console but not finished with Enter.
            os.write(master, self.typed)
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
        erase, *commands = [part for part in bytes(server.received).split(b"\r") if part]
        self.assertEqual(erase, b"\x15")  # Ctrl-U before the first Enter
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
        self.assertEqual(bytes(server.received), b"\x15\r")

    def test_erases_an_unfinished_line_instead_of_running_it(self) -> None:
        """Clear a typed line with Ctrl-U so the first Enter does not run it."""
        with _ShellServer(self.guest, typed=b"touch PARTIAL") as server:
            self._extract(server)

        self.assertEqual((self.host / "report.01.first").read_bytes(), self.content)
        self.assertFalse((self.guest / "PARTIAL").exists())

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

    def test_requires_a_running_vm_with_a_serial_port(self) -> None:
        """Refuse stopped VMs, VMs without a port and old attached sessions."""
        cases = [
            (_Service(state="poweroff"), "is not running"),
            (_Service(port=None), "has no TCP serial port"),
            (
                _Service(session=SessionRecord("first", 2326, 1, "now")),
                "without a control channel",
            ),
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


class _SshService:
    """Service of a VM reached through ssh."""

    def __init__(self, target=None, store=None, error: str | None = None) -> None:
        self.target, self.ssh_key_store, self.error = target, store, error
        self.session_registry = SimpleNamespace(
            get_live_session=lambda name: SessionRecord("first", 2400, 1, "now")
        )

    def get_vm_access(self, vm_name: str) -> AccessInfo:
        """Reach the fake VM through ssh."""
        return AccessInfo("ssh", "default", None, 2400, "root")

    def ssh_target(self, vm_name: str):
        """Return the target or refuse like the real service."""
        if self.error:
            from vboxlab.errors import VBoxLabError
            raise VBoxLabError(self.error)
        return self.target


class ExtractOverSshTests(unittest.TestCase):
    """Copy files through a separate ssh session."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.destination = Path(self._temp_dir.name) / "copy"
        self.answers: list = []
        self.commands: list[str] = []
        patcher = patch("vboxlab.report.extract.ssh_client.run_remote", self._run_remote)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._temp_dir.cleanup)

    def _run_remote(self, target, store, command, *, timeout=None):
        self.commands.append(command)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _extract(self, service=None, name="report.01.first", **kwargs):
        return extract_file(service or _SshService(target=SshTarget("first", "u", 2400, "root")), "first", name,
                            destination=self.destination, **kwargs)

    @staticmethod
    def _answer(code=0, stdout=b"", stderr=b""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_copies_and_verifies_the_file(self) -> None:
        """Ignore the attached session, verify the checksum and write the file."""
        content = b"\x00report\nline\r\n"
        digest = hashlib.sha256(content).hexdigest()
        self.answers = [self._answer(stdout=digest.encode() + b"  -\n" + content)]
        result = self._extract(max_bytes=len(content))
        self.assertEqual((result.size, result.sha256), (len(content), digest))
        self.assertEqual(self.destination.read_bytes(), content)
        self.assertIn('"$HOME"/report.01.first', self.commands[0])
        self.assertIn(f"head -c {len(content) + 1}", self.commands[0])

    def test_reports_failures(self) -> None:
        """Explain missing files, refused keys, ssh errors and bad data."""
        digest = hashlib.sha256(b"data").hexdigest().encode()
        cases = [
            (self._answer(3), "was not found or is not readable"),
            (self._answer(255, stderr=b"root@127.0.0.1: Permission denied (publickey)."),
             "refused the VBoxLab key for root; run vboxlab ssh-key first"),
            (self._answer(255, stderr=b"Connection refused"), "ssh transfer from \"first\" failed: Connection refused"),
            (self._answer(1), "failed: exit status 1"),
            (self._answer(stdout=b""), "incomplete transfer"),
            (self._answer(stdout=digest + b"  -\ndat"), "checksum of the received file does not match"),
            (self._answer(stdout=digest + b"  -\n" + b"x" * 5), "larger than 4 bytes"),
            (subprocess.TimeoutExpired("ssh", 120), "did not finish within 120 seconds"),
        ]
        for answer, message in cases:
            self.answers = [answer]
            with self.subTest(message=message), self.assertRaisesRegex(ExtractError, message):
                self._extract(max_bytes=4)
            self.assertFalse(self.destination.exists())

    def test_needs_a_running_vm_with_a_port(self) -> None:
        """Pass on why the service cannot reach the VM."""
        with self.assertRaisesRegex(ExtractError, "is not running; ssh needs a running guest"):
            self._extract(_SshService(error='Virtual machine "first" is not running; ssh needs a running guest.'))


@unittest.skipUnless(HAVE_OPENSSH and SSHD and os.name == "posix", "OpenSSH client and server are required")
class ExtractWithSshdTests(SshdFixture):
    """Copy a file from a real sshd."""

    def test_copies_a_file_with_spaces_in_its_path(self) -> None:
        """Transfer binary content byte for byte."""
        source = Path(self._temp_dir.name) / "my report"
        content = bytes(range(256)) * 40
        source.write_bytes(content)
        destination = Path(self._temp_dir.name) / "copy"
        result = extract_file(_SshService(self.target, self.store), "first", str(source),
                              destination=destination)
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
        with self.assertRaisesRegex(ExtractError, "not found"):
            extract_file(_SshService(self.target, self.store), "first", str(source) + "-missing",
                         destination=Path(self._temp_dir.name) / "other")
