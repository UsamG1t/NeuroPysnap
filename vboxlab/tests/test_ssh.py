"""Tests for the PySnap ssh key, the ssh client and ssh-key setup."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import getpass
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from vboxlab.cli.app import run_cli
from vboxlab.config.protosettings import ProtoSettingsStore
from vboxlab.core.access import ACCESS_KEY, SSH_USER_KEY
from vboxlab.core.service import PySnapService
from vboxlab.errors import PySnapError
from vboxlab.ssh import client as ssh_client
from vboxlab.ssh.client import SshTarget, install_script, ssh_command
from vboxlab.ssh.keys import KEY_NAME, SshKeyStore
from vboxlab.tests.test_access import AccessClient
from vboxlab.tests.test_service import FakeSessionRegistry
from vboxlab.tests.test_vbox_client import FakeRunner
from vboxlab.vbox.client import VBoxManageClient

SSHD = shutil.which("sshd", path=os.environ.get("PATH", "") + os.pathsep + "/usr/sbin")
HAVE_OPENSSH = bool(shutil.which("ssh") and shutil.which("ssh-keygen"))


class KeyStoreTests(unittest.TestCase):
    """Verify the PySnap key pair and known_hosts files."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name) / "ssh"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    @unittest.skipUnless(HAVE_OPENSSH, "OpenSSH is required")
    def test_creates_the_key_once_in_a_private_directory(self) -> None:
        """Create a passphrase-free ed25519 pair and reuse it."""
        store = SshKeyStore(self.root)
        key = store.ensure_key()
        self.assertEqual(key, self.root / KEY_NAME)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        line = store.public_key_text()
        self.assertTrue(line.startswith("ssh-ed25519 "))
        self.assertIn(" pysnap-vm-access@", line)
        before = key.read_bytes()
        self.assertEqual(store.ensure_key(), key)
        self.assertEqual(key.read_bytes(), before)

    def test_never_overwrites_foreign_files(self) -> None:
        """Refuse a half pair, a non-directory root and symlinks."""
        self.root.mkdir(mode=0o700)
        (self.root / KEY_NAME).write_text("someone else's key")
        with self.assertRaisesRegex(PySnapError, "does not overwrite"):
            SshKeyStore(self.root).ensure_key()
        self.assertEqual((self.root / KEY_NAME).read_text(), "someone else's key")

        other = Path(self._temp_dir.name) / "file"
        other.write_text("x")
        with self.assertRaisesRegex(PySnapError, "not a directory"):
            SshKeyStore(other).ensure_key()

        linked = Path(self._temp_dir.name) / "linked"
        linked.mkdir()
        (linked / KEY_NAME).symlink_to(other)
        (linked / f"{KEY_NAME}.pub").write_text("x")
        with self.assertRaisesRegex(PySnapError, "not a regular file"):
            SshKeyStore(linked).ensure_key()

    def test_known_hosts_per_vm(self) -> None:
        """Keep one file per VM UUID and reject odd UUIDs."""
        store = SshKeyStore(self.root)
        path = store.known_hosts("4f1c-77aa")
        self.assertEqual(path, self.root / "known_hosts.d" / "4f1c-77aa")
        for bad in ("../x", "", "a b"):
            with self.subTest(uuid=bad), self.assertRaisesRegex(PySnapError, "Invalid virtual machine UUID"):
                store.known_hosts(bad)


@unittest.skipUnless(HAVE_OPENSSH, "OpenSSH is required")
class CommandTests(unittest.TestCase):
    """Verify ssh command lines and the guest install script."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.store = SshKeyStore(Path(self._temp_dir.name) / "ssh")
        self.target = SshTarget("first", "uuid-1", 2400, "student")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_batch_and_password_command_lines(self) -> None:
        """Use only the PySnap key in batch mode and only passwords otherwise."""
        batch = ssh_command(self.target, self.store, remote_command="true")
        self.assertEqual(batch[1:3], ["-F", os.devnull])
        self.assertIn(f"UserKnownHostsFile={self.store.known_hosts('uuid-1')}", batch)
        self.assertIn("StrictHostKeyChecking=accept-new", batch)
        self.assertIn("BatchMode=yes", batch)
        self.assertIn("IdentitiesOnly=yes", batch)
        self.assertEqual(batch[batch.index("-i") + 1], str(self.store.private_key))
        self.assertEqual(batch[-2:], ["student@127.0.0.1", "true"])
        self.assertEqual(batch[batch.index("-p") + 1], "2400")

        password = ssh_command(self.target, self.store, batch=False, tty=True)
        self.assertNotIn("BatchMode=yes", password)
        self.assertIn("PubkeyAuthentication=no", password)
        self.assertIn("-tt", password)

    def test_install_script_adds_the_key_once(self) -> None:
        """Append the key to authorized_keys without duplicates."""
        home = Path(self._temp_dir.name) / "home"
        home.mkdir()
        key = "ssh-ed25519 AAAAtest pysnap-vm-access@host"
        for _ in range(2):
            subprocess.run(["sh", "-c", install_script(key, with_root=False)],
                           env={**os.environ, "HOME": str(home)}, check=True)
        keys = (home / ".ssh" / "authorized_keys").read_text().splitlines()
        self.assertEqual(keys, [key])
        self.assertEqual((home / ".ssh").stat().st_mode & 0o777, 0o700)
        self.assertIn("su - root -c", install_script(key, with_root=True))


class _TestSshd:
    """Run sshd as the current user on a free port with one authorized key."""

    def __init__(self, root: Path, public_key: str) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "host")], check=True)
        (root / "authorized_keys").write_text(public_key + "\n")
        os.chmod(root / "authorized_keys", 0o600)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        (root / "sshd_config").write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {root / 'host'}\n"
            f"PidFile {root / 'pid'}\nAuthorizedKeysFile {root / 'authorized_keys'}\n"
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
            "StrictModes no\nPermitRootLogin prohibit-password\n"
        )
        os.makedirs("/run/sshd", exist_ok=True) if os.geteuid() == 0 else None
        self.process = subprocess.Popen([SSHD, "-D", "-e", "-f", str(root / "sshd_config")],
                                        stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("sshd did not start")

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=5)


@unittest.skipUnless(HAVE_OPENSSH and SSHD and os.name == "posix", "OpenSSH client and server are required")
class SshdFixture(unittest.TestCase):
    """Start a real sshd that accepts the PySnap key of a temporary store."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls._temp_dir.name)
        cls.store = SshKeyStore(root / "ssh")
        cls.sshd = _TestSshd(root / "sshd", cls.store.public_key_text())
        cls.target = SshTarget("guest", "uuid-guest", cls.sshd.port, getpass.getuser())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sshd.stop()
        cls._temp_dir.cleanup()


class RealSshdTests(SshdFixture):
    """Log in, run commands and open a terminal against a real sshd."""

    def test_checks_key_access(self) -> None:
        """Tell a working key, a refused one and a missing server apart."""
        self.assertEqual(ssh_client.check_key_access(self.target, self.store).status, "ok")
        self.assertTrue(self.store.known_hosts("uuid-guest").read_text().strip())
        other = SshKeyStore(Path(self._temp_dir.name) / "other")
        self.assertEqual(ssh_client.check_key_access(self.target, other).status, "denied")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        missing = replace(self.target, port=closed)
        self.assertEqual(ssh_client.check_key_access(missing, self.store).status, "unreachable")

    def test_refuses_a_changed_host_key(self) -> None:
        """Keep the first host key of a VM and refuse a different one."""
        known = self.store.known_hosts("uuid-changed")
        known.write_text(f"[127.0.0.1]:{self.sshd.port} ssh-ed25519 "
                         "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n")
        check = ssh_client.check_key_access(replace(self.target, vm_uuid="uuid-changed"), self.store)
        self.assertEqual(check.status, "unreachable")
        self.assertIn("IDENTIFICATION HAS CHANGED", check.message.upper())

    def test_runs_commands(self) -> None:
        """Return output and exit status of a guest command."""
        result = ssh_client.run_remote(self.target, self.store, "echo $((6*7)); exit 3")
        self.assertEqual((result.returncode, result.stdout), (3, b"42\n"))

    def test_opens_a_terminal_of_the_given_size(self) -> None:
        """Start a login shell on a terminal with the requested size."""
        async def session() -> bytes:
            reader, writer, process = await ssh_client.open_terminal(
                self.target, self.store, columns=101, rows=33
            )
            writer.write(b"stty size; echo DONE-$((2+2))\r")
            await writer.drain()
            data = b""
            while b"DONE-4" not in data:
                data += await asyncio.wait_for(reader.read(4096), 10)
            writer.write(b"exit\r")
            await writer.drain()
            await asyncio.wait_for(process.wait(), 10)
            return data

        self.assertIn(b"33 101", asyncio.run(session()))


class ServiceTests(unittest.TestCase):
    """Verify ssh targets and the key setup flow without a server."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        self.client = AccessClient()
        self.client.add("vm", state="running", ssh_port=2400)
        self.service = PySnapService(
            client=self.client,
            session_registry=FakeSessionRegistry(),
            proto_settings_store=ProtoSettingsStore(path=root / "proto"),
            ssh_key_store=_FakeStore(root),
        )
        self.working: set[str] = set()
        self.checks = []
        self.original = ssh_client.check_key_access
        ssh_client.check_key_access = self._check

    def tearDown(self) -> None:
        ssh_client.check_key_access = self.original
        self._temp_dir.cleanup()

    def _check(self, target, store):
        self.checks.append(target.user)
        return ssh_client.AccessCheck("ok" if target.user in self.working else "denied")

    def test_ssh_target_needs_a_port_and_a_running_vm(self) -> None:
        """Explain what is missing."""
        target = self.service.ssh_target("vm")
        self.assertEqual((target.port, target.user, target.vm_uuid), (2400, "root", "uuid-vm"))
        self.assertEqual(self.service.ssh_target("vm", "student").user, "student")
        self.client.add("noport", state="running")
        with self.assertRaisesRegex(PySnapError, "no forwarded ssh port; run pysnap plug noport"):
            self.service.ssh_target("noport")
        self.client.add("serial", state="running", metadata={ACCESS_KEY: "com"})
        with self.assertRaisesRegex(PySnapError, "reached through COM1"):
            self.service.ssh_target("serial")
        self.client.add("stopped", ssh_port=2401)
        with self.assertRaisesRegex(PySnapError, "is not running"):
            self.service.ssh_target("stopped")

    def test_installs_through_the_password_login(self) -> None:
        """Run the installer and report the users that work afterwards."""
        calls = []

        def installer(target, store):
            calls.append(target.user)
            self.working.update({"student", "root"})

        result = self.service.setup_ssh_key("vm", "student", installer=installer)
        self.assertEqual(calls, ["student"])
        self.assertEqual((result.users, result.ssh_user, result.changed_user), (("student", "root"), "root", False))

    def test_switches_the_ssh_user_when_root_was_skipped(self) -> None:
        """Use the password user when root did not get the key."""
        result = self.service.setup_ssh_key(
            "vm", "student", installer=lambda target, store: self.working.add("student")
        )
        self.assertEqual((result.users, result.ssh_user, result.changed_user), (("student",), "student", True))
        self.assertEqual(self.client.infos["vm"].metadata[SSH_USER_KEY], "student")

    def test_does_nothing_when_the_key_already_works(self) -> None:
        """Skip the installer for a working key."""
        self.working.add("root")
        result = self.service.setup_ssh_key("vm", installer=lambda target, store: self.fail("asked for a password"))
        self.assertTrue(result.already_installed)
        self.assertEqual(result.users, ("root",))

    def test_types_the_key_on_the_console(self) -> None:
        """Type the install command and wait until the key works."""
        typed = []

        def type_on_console(vm_name, text):
            typed.append(text)
            self.working.add("root")

        self.client.type_on_console = type_on_console
        result = self.service.setup_ssh_key("vm", console=True, installer=lambda *a: self.fail("ssh used"))
        self.assertEqual(result.users, ("root",))
        self.assertTrue(typed[0].startswith(" umask 077; mkdir -p ~/.ssh"))
        self.assertIn("ssh-ed25519 AAAAfake", typed[0])

    def test_reports_when_nothing_worked(self) -> None:
        """Explain a failed setup with the hints."""
        with self.assertRaisesRegex(PySnapError, "--user NAME.*--console"):
            self.service.setup_ssh_key("vm", installer=lambda target, store: None)

    def test_stops_when_no_server_answers(self) -> None:
        """Do not ask for passwords without an ssh server."""
        ssh_client.check_key_access = lambda target, store: ssh_client.AccessCheck("unreachable", "refused")
        with self.assertRaisesRegex(PySnapError, "No ssh server answered on port 2400"):
            self.service.setup_ssh_key("vm", installer=lambda *a: self.fail("asked for a password"))


class _FakeStore(SshKeyStore):
    """Key store with a fixed public key and no ssh-keygen."""

    def ensure_key(self) -> Path:
        return self.private_key

    def public_key_text(self) -> str:
        return "ssh-ed25519 AAAAfake pysnap-vm-access@test"


class ConsoleAndCommandLineTests(unittest.TestCase):
    """Verify keyboard input through VBoxManage and the ssh-key command."""

    def test_types_with_ctrl_u_and_enter(self) -> None:
        """Clear the line, type the text and press Enter."""
        runner = FakeRunner()
        VBoxManageClient(runner=runner).type_on_console("vm", "echo hi")
        self.assertEqual(runner.commands, [
            ("controlvm", "vm", "keyboardputscancode", "1d", "16", "96", "9d"),
            ("controlvm", "vm", "keyboardputstring", "echo hi"),
            ("controlvm", "vm", "keyboardputscancode", "1c", "9c"),
        ])

    def test_command_line_reports_the_result(self) -> None:
        """Print the users and a switched ssh user."""
        class Service:
            def setup_ssh_key(self, vm, user, console=False):
                from vboxlab.core.models import SshKeyResult
                return SshKeyResult(vm, ("student",), "student", changed_user=True)

        output = io.StringIO()
        code = run_cli(["ssh-key", "vm", "--user", "student"], service=Service(), stdout=output,
                       stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "PySnap key works for: student",
            "ssh user of vm set to student.",
        ])


if __name__ == "__main__":
    unittest.main()
