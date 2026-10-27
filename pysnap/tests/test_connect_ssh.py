"""Tests for pysnap connect over ssh."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pysnap.config.protosettings import ProtoSettingsStore
from pysnap.core.access import ACCESS_KEY, SSH_USER_KEY
from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError
from pysnap.ssh import client as ssh_client
from pysnap.ssh.client import AccessCheck, SshTarget
from pysnap.terminal.session import SessionStatus, SessionTransport, TerminalSession
from pysnap.tests.test_access import AccessClient
from pysnap.tests.test_service import FakeSessionRegistry
from pysnap.tests.test_ssh import HAVE_OPENSSH, SSHD, SshdFixture


class PrepareTests(unittest.TestCase):
    """Verify the preparation of ssh connections."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.client = AccessClient()
        self.service = PySnapService(
            client=self.client,
            session_registry=FakeSessionRegistry(),
            proto_settings_store=ProtoSettingsStore(path=Path(self._temp_dir.name) / "proto"),
        )
        self.checks: list[str] = []
        self.answers: list[str] = []
        self.original = ssh_client.check_key_access
        ssh_client.check_key_access = self._check

    def tearDown(self) -> None:
        ssh_client.check_key_access = self.original
        self._temp_dir.cleanup()

    def _check(self, target, store):
        self.checks.append(target.user)
        status = self.answers.pop(0) if self.answers else "ok"
        return AccessCheck(status, "connection refused" if status == "unreachable" else "")

    def test_prepare_vm_connection_follows_the_access(self) -> None:
        """Need an ssh port for ssh VMs and refuse paused ones; start stopped ones."""
        self.client.add("noport", state="running")
        with self.assertRaisesRegex(PySnapError, "no forwarded ssh port; run pysnap plug noport"):
            self.service.prepare_vm_connection("noport")
        self.client.add("paused", state="paused", ssh_port=2400)
        with self.assertRaisesRegex(PySnapError, "paused; ssh needs a running guest. Run pysnap resume paused"):
            self.service.prepare_vm_connection("paused")
        self.client.add("stopped", ssh_port=2401)
        self.assertEqual(self.service.prepare_vm_connection("stopped").vm_state, "running")
        self.assertIn(("start_vm_headless", "stopped"), self.client.calls)
        self.client.add("serial", state="paused", serial_port=2326, metadata={ACCESS_KEY: "com"})
        self.assertEqual(self.service.prepare_vm_connection("serial").vm_state, "paused")

    def test_waits_for_a_booting_guest(self) -> None:
        """Retry while no ssh server answers and say so once."""
        self.client.add("vm", state="running", ssh_port=2400)
        self.answers = ["unreachable", "unreachable", "ok"]
        messages = []
        target = self.service.prepare_ssh_terminal("vm", notify=messages.append, poll_interval=0)
        self.assertEqual((target.port, target.user), (2400, "root"))
        self.assertEqual(messages, ['Waiting for the ssh server of "vm" (Ctrl-C stops)...'])

        self.answers = ["unreachable"] * 1000
        with self.assertRaisesRegex(PySnapError, "No ssh server answered on port 2400"):
            self.service.prepare_ssh_terminal("vm", timeout=0.05, poll_interval=0.01)

    def test_installs_the_key_on_first_connect(self) -> None:
        """Run the key setup when the key is refused and use the resulting user."""
        self.client.add("vm", state="running", ssh_port=2400)
        working: set[str] = set()
        ssh_client.check_key_access = lambda target, store: AccessCheck(
            "ok" if target.user in working else "denied"
        )
        messages = []

        def installer(target, store):
            working.add("student")

        self.client.set_metadata("vm", {SSH_USER_KEY: "student"})
        with patch.object(type(self.service.ssh_key_store), "ensure_key", lambda store: store.private_key):
            target = self.service.prepare_ssh_terminal("vm", notify=messages.append, installer=installer)
        self.assertEqual(working, {"student"})
        self.assertEqual(target.user, "student")
        self.assertIn("is not installed for student", messages[0])


class TransportTests(unittest.TestCase):
    """Verify how the session picks and describes its transport."""

    def test_labels_and_roles(self) -> None:
        """Mark only the COM1 console as serial."""
        serial = SessionTransport.for_serial(2326)
        ssh = SessionTransport.for_ssh(SshTarget("vm", "u", 2400, "root"), None)
        self.assertEqual((serial.name, serial.label, serial.port, serial.serial), ("Serial", "UART1:2326", 2326, True))
        self.assertEqual((ssh.name, ssh.label, ssh.port, ssh.serial), ("ssh", "ssh:2400", 2400, False))

    def test_run_uses_ssh_for_ssh_vms(self) -> None:
        """Prepare the ssh login and pass an ssh transport to the session."""
        class Service:
            ssh_key_store = None

            def prepare_vm_connection(self, vm_name):
                from pysnap.core.models import VMInfo
                return VMInfo(name=vm_name, uuid="u", groups=("/Lab",), vm_state="running", ssh_port=2400)

            def get_vm_access(self, vm_name):
                from pysnap.core.access import AccessInfo
                return AccessInfo("ssh", "default", None, 2400, "root")

            def prepare_ssh_terminal(self, vm_name, notify=None):
                return SshTarget(vm_name, "u", 2400, "root")

        seen = []

        def fake_run(coroutine):
            seen.append(coroutine.cr_frame.f_locals["transport"])
            coroutine.close()
            return 0

        with patch("pysnap.terminal.session.asyncio.run", side_effect=fake_run):
            self.assertEqual(TerminalSession(service=Service()).run("vm"), 0)
        self.assertEqual(seen[0].label, "ssh:2400")

        def interrupted(vm_name, notify=None):
            raise KeyboardInterrupt

        Service.prepare_ssh_terminal = lambda self, vm_name, notify=None: interrupted(vm_name)
        with patch("pysnap.terminal.session.asyncio.run") as run, patch("builtins.print"):
            self.assertEqual(TerminalSession(service=Service()).run("vm"), 130)
        run.assert_not_called()


class _Writer:
    """Collect what the session sends to the guest."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Registry:
    """Session registry that records the registration."""

    def __init__(self) -> None:
        self.records: list[tuple] = []

    @contextmanager
    def register(self, vm_name, port, *, control_port=None, control_token=None):
        self.records.append((vm_name, port, control_port))
        yield


class _Service:
    """Service with a scripted VM state."""

    def __init__(self, states: list[str]) -> None:
        self.session_registry = _Registry()
        self.states = states

    def get_session_state_label(self, vm_name: str) -> str:
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class SessionLoopTests(unittest.TestCase):
    """Run the terminal session on a scripted transport."""

    def _run(self, serial: bool, *, paused: bool = False, output: bytes = b"", linger: float = 0.0):
        service = _Service(["Paused", "Paused", "Working"] if paused else ["Working"])
        writer = _Writer()
        messages: list[str] = []
        closed: list[bool] = []

        class Status(SessionStatus):
            def __setattr__(self, name, value):
                if name == "message":
                    messages.append(value)
                super().__setattr__(name, value)

        async def scenario() -> None:
            reader = asyncio.StreamReader()

            async def close() -> None:
                closed.append(True)

            async def opener(columns: int, rows: int):
                if output:
                    reader.feed_data(output)
                asyncio.get_running_loop().call_later(linger, reader.feed_eof)
                return reader, writer, close

            transport = SessionTransport("Serial" if serial else "ssh", "label", 2400, serial, opener)
            with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
                await TerminalSession(service=service)._run_async("vm", transport, paused=paused)

        with patch("pysnap.terminal.session.SessionStatus", Status), \
                patch("pysnap.terminal.session.STATE_POLL_INTERVAL", 0.3):
            asyncio.run(scenario())
        return bytes(writer.data), service.session_registry.records, messages, closed

    def test_serial_console_extras(self) -> None:
        """Wake the console, offer the control channel and hint at silence."""
        data, records, messages, closed = self._run(True, linger=2.3)
        self.assertTrue(data.startswith(b"\r\n"))
        self.assertIsNotNone(records[0][2])
        self.assertIn("Connected. Press Enter if the guest stays silent.", messages)
        self.assertEqual(closed, [True])
        data, _, _, _ = self._run(True, paused=True, linger=1.6)
        self.assertIn(b"\r\n", data)

    def test_ssh_has_none_of_them(self) -> None:
        """Send nothing by itself, register no control channel and close ssh."""
        data, records, messages, closed = self._run(False, linger=2.3)
        self.assertEqual(data, b"")
        self.assertEqual(records, [("vm", 2400, None)])
        self.assertIn("Connected through ssh. Ctrl-] q detaches.", messages)
        self.assertNotIn("Connected. Press Enter if the guest stays silent.", messages)
        self.assertIn("ssh connection closed.", " ".join(messages))
        self.assertEqual(closed, [True])
        data, _, _, _ = self._run(False, paused=True, linger=1.6)
        self.assertEqual(data, b"")


@unittest.skipUnless(HAVE_OPENSSH and SSHD, "OpenSSH client and server are required")
class SshTransportTests(SshdFixture):
    """Open the ssh transport of a session against a real sshd."""

    def test_transport_opens_and_closes(self) -> None:
        """Connect, exchange data and stop ssh on close."""
        async def session() -> tuple[bytes, int | None]:
            transport = SessionTransport.for_ssh(self.target, self.store)
            reader, writer, close = await transport.opener(90, 30)
            writer.write(b"stty size; echo READY-$((3+4))\r")
            await writer.drain()
            data = b""
            while b"READY-7" not in data:
                data += await asyncio.wait_for(reader.read(4096), 10)
            await close()
            return data, None

        data, _ = asyncio.run(session())
        self.assertIn(b"30 90", data)


if __name__ == "__main__":
    unittest.main()
