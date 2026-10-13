"""Tests for pausing and resuming VMs."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pysnap.cli.app import run_cli
from pysnap.config.protosettings import ProtoSettingsStore
from pysnap.core.models import VMInfo, VMReference
from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError
from pysnap.runtime.sessions import SessionRecord
from pysnap.terminal.session import PauseTracker
from pysnap.tests.test_service import FakeClient, FakeSessionRegistry
from pysnap.tests.test_vbox_client import FakeRunner
from pysnap.vbox.client import VBoxManageClient


class PauseClient(FakeClient):
    """Fake client whose VMs can be paused and resumed."""

    def add_vm(self, name: str, state: str, port: int = 2326) -> None:
        """Register a VM in the given raw state."""
        self.references[name] = VMReference(name=name, uuid=f"uuid-{name}")
        self.infos[name] = VMInfo(name=name, uuid=f"uuid-{name}", groups=("/Lab",),
                                  serial_port=port, vm_state=state)

    def _set_state(self, name: str, state: str) -> None:
        self.infos[name] = replace(self.infos[name], vm_state=state)

    def pause_vm(self, vm_name: str) -> None:
        self.calls.append(("pause_vm", vm_name))
        self._set_state(vm_name, "paused")

    def resume_vm(self, vm_name: str) -> None:
        self.calls.append(("resume_vm", vm_name))
        self._set_state(vm_name, "running")


class ClientTests(unittest.TestCase):
    """Verify the VBoxManage calls."""

    def test_pause_and_resume_use_controlvm(self) -> None:
        """Send controlvm pause and resume."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)
        client.pause_vm("first")
        client.resume_vm("first")
        self.assertEqual(runner.commands, [
            ("controlvm", "first", "pause"),
            ("controlvm", "first", "resume"),
        ])


class ServiceTests(unittest.TestCase):
    """Verify pause, resume and their effect on stop and connect."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.client = PauseClient()
        self.client.add_vm("first", "running", 2326)
        self.client.add_vm("second", "paused", 2327)
        self.client.add_vm("third", "poweroff", 2328)
        self.registry = FakeSessionRegistry()
        self.service = PySnapService(
            client=self.client,
            session_registry=self.registry,
            proto_settings_store=ProtoSettingsStore(path=Path(self._temp_dir.name) / ".ptotosettings"),
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _state(self, name: str) -> str:
        return self.client.infos[name].vm_state

    def test_pauses_and_resumes_one_vm(self) -> None:
        """Pause a running VM and resume a paused one."""
        self.service.pause_runtime_vm("first")
        self.assertEqual(self._state("first"), "paused")
        self.service.resume_runtime_vm("first")
        self.assertEqual(self._state("first"), "running")

    def test_pauses_an_attached_vm(self) -> None:
        """Pause a VM with a live connect session (Working)."""
        self.registry.live_sessions["first"] = SessionRecord("first", 2326, 1, "now")
        self.service.pause_runtime_vm("first")
        self.assertEqual(self._state("first"), "paused")

    def test_refuses_wrong_states(self) -> None:
        """Explain why a VM cannot be paused or resumed."""
        cases = [
            (self.service.pause_runtime_vm, "second", "already paused"),
            (self.service.pause_runtime_vm, "third", "cannot be paused from state Stopped"),
            (self.service.resume_runtime_vm, "first", r"is not paused \(Active\)"),
            (self.service.resume_runtime_vm, "third", r"is not paused \(Stopped\)"),
        ]
        for action, name, message in cases:
            with self.subTest(name=name, message=message), self.assertRaisesRegex(PySnapError, message):
                action(name)
        self.assertNotIn("pause_vm", [call[0] for call in self.client.calls])
        self.assertNotIn("resume_vm", [call[0] for call in self.client.calls])

    def test_pauses_and_resumes_all(self) -> None:
        """Act on all running or all paused VMs."""
        self.client.add_vm("fourth", "running", 2329)
        self.assertEqual(self.service.pause_all_runtime_vms(), ["first", "fourth"])
        self.assertEqual({self._state(name) for name in ("first", "second", "fourth")}, {"paused"})
        self.assertEqual(self.service.resume_all_runtime_vms(), ["first", "fourth", "second"])
        self.assertEqual(self._state("third"), "poweroff")
        self.assertEqual(self.service.resume_all_runtime_vms(), [])

    def test_reports_failures_of_all(self) -> None:
        """Name the VMs that did not change."""
        def broken(vm_name: str) -> None:
            raise PySnapError("controlvm failed")

        self.client.pause_vm = broken
        with self.assertRaisesRegex(PySnapError, "Unable to pause all requested VMs: first: controlvm failed"):
            self.service.pause_all_runtime_vms()

    def test_stop_resumes_a_paused_vm_first(self) -> None:
        """Resume before the ACPI power button, which a paused guest ignores."""
        self.service.stop_runtime_vm("second")
        calls = [call for call in self.client.calls if call[0] in {"resume_vm", "stop_vm_acpi"}]
        self.assertEqual(calls, [("resume_vm", "second"), ("stop_vm_acpi", "second")])
        self.assertEqual(self._state("second"), "poweroff")

    def test_stop_all_includes_paused_vms(self) -> None:
        """Stop running and paused VMs together."""
        self.assertEqual(self.service.stop_all_runtime_vms(), ["first", "second"])
        self.assertEqual(self._state("first"), "poweroff")
        self.assertEqual(self._state("second"), "poweroff")

    def test_connect_accepts_a_paused_vm(self) -> None:
        """Prepare a connection to a paused VM without starting or resuming it."""
        info = self.service.prepare_vm_connection("second")
        self.assertEqual(info.vm_state, "paused")
        self.assertEqual(self.client.calls, [])


class PauseTrackerTests(unittest.TestCase):
    """Verify how the connect session follows the VM state."""

    def test_follows_pause_and_resume(self) -> None:
        """Report changes once and end only on other states."""
        tracker = PauseTracker()
        self.assertEqual(
            [tracker.observe(state) for state in ("Working", "Paused", "Paused", "Working", "Working")],
            [None, "paused", None, "resumed", None],
        )
        self.assertFalse(tracker.paused)
        self.assertEqual(tracker.observe("Stopping"), "exit")

    def test_starts_paused(self) -> None:
        """Keep keys held back until the first running state."""
        tracker = PauseTracker(paused=True)
        self.assertTrue(tracker.paused)
        self.assertIsNone(tracker.observe("Paused"))
        self.assertEqual(tracker.observe("Active"), "resumed")
        self.assertEqual(PauseTracker(paused=True).observe("Stopped"), "exit")


class CommandTests(unittest.TestCase):
    """Verify the command line."""

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        client = PauseClient()
        client.add_vm("first", "running")
        client.add_vm("second", "paused", 2327)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = PySnapService(
                client=client,
                session_registry=FakeSessionRegistry(),
                proto_settings_store=ProtoSettingsStore(path=Path(temp_dir) / ".ptotosettings"),
            )
            output, errors = io.StringIO(), io.StringIO()
            code = run_cli(list(arguments), service=service, stdout=output, stderr=errors)
        return code, output.getvalue(), errors.getvalue()

    def test_pause_and_resume_commands(self) -> None:
        """Print what was changed."""
        self.assertEqual(self._run("pause", "first")[:2], (0, "Paused virtual machine: first\n"))
        self.assertEqual(self._run("resume", "second")[:2], (0, "Resumed virtual machine: second\n"))
        self.assertEqual(self._run("pause", "--all")[:2], (0, "Paused virtual machines: first\n"))
        self.assertEqual(self._run("resume", "--all")[:2], (0, "Resumed virtual machines: second\n"))

    def test_reports_errors(self) -> None:
        """Require one VM or --all and explain wrong states."""
        code, _, errors = self._run("pause")
        self.assertEqual(code, 2)
        self.assertIn('exactly one of "--all" or "VM"', errors)
        code, _, errors = self._run("resume", "first")
        self.assertEqual(code, 1)
        self.assertIn("is not paused", errors)


if __name__ == "__main__":
    unittest.main()
