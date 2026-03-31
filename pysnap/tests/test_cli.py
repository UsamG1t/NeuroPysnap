"""Unit tests for the PySnap CLI."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from pysnap.cli.app import run_cli
from pysnap.core.models import (
    IntegrationTestResult,
    VMGroup,
    VMInfo,
    VMMonitorRecord,
)


class FakeService:
    """Provide a controllable fake service for CLI tests."""

    def __init__(self) -> None:
        """Initialize fake outputs and call tracking."""
        self.clone_args: tuple | None = None
        self.import_args: tuple | None = None
        self.stopped_vm: str | None = None
        self.stop_all_requested = False

    def list_groups(self) -> list[VMGroup]:
        """Return a static group list."""
        return [VMGroup(name="/Lab", vm_names=("base-vm", "clone-vm"))]

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return a static VM description."""
        return VMInfo(name=vm_name, uuid="uuid", groups=("/Lab",), serial_port=2345)

    def clone_vm(
        self,
        base_vm: str,
        clone_vm: str,
        serial_port: int | None = None,
        networks: tuple[str, ...] = (),
    ) -> VMInfo:
        """Record clone arguments and return a clone description."""
        self.clone_args = (base_vm, clone_vm, serial_port, networks)
        return VMInfo(
            name=clone_vm,
            uuid="uuid-clone",
            groups=("/Lab",),
            serial_port=serial_port,
            parent_name=base_vm,
        )

    def import_image(self, image_path: str, progress_callback=None) -> list[VMInfo]:
        """Return a static import result."""
        self.import_args = (image_path,)
        if progress_callback is not None:
            progress_callback(5)
            progress_callback(55)
            progress_callback(100)
        return [VMInfo(name="base-vm", uuid="uuid", groups=("/Others",))]

    def run_integration_test(self, image_path: str) -> IntegrationTestResult:
        """Return a static integration-test result."""
        return IntegrationTestResult(
            machines=(
                VMInfo(name="base-vm", uuid="uuid-base", groups=("/Lab",), serial_port=1024),
                VMInfo(
                    name="clone-a",
                    uuid="uuid-a",
                    groups=("/Lab",),
                    serial_port=1025,
                    parent_name="base-vm",
                ),
            ),
            deleted_vm_names=("clone-a", "base-vm"),
            monitor_records=(
                VMMonitorRecord(
                    name="clone-a",
                    display_state="Working",
                    serial_port=1025,
                    group="/Lab",
                    raw_state="running",
                ),
                VMMonitorRecord(
                    name="clone-b",
                    display_state="Active",
                    serial_port=1026,
                    group="/Lab",
                    raw_state="running",
                ),
            ),
        )

    def erase_vm(self, vm_name: str) -> None:
        """Pretend to erase one VM."""

    def erase_group(self, group_name: str) -> list[str]:
        """Pretend to erase a group."""
        return ["base-vm", "clone-vm"]

    def erase_all(self) -> list[str]:
        """Pretend to erase all VMs."""
        return ["base-vm", "clone-vm"]

    def list_monitored_vms(self) -> list[VMMonitorRecord]:
        """Return compact runtime monitor data."""
        return [
            VMMonitorRecord(
                name="base-vm",
                display_state="Working",
                serial_port=2345,
                group="/Lab",
                raw_state="running",
            ),
            VMMonitorRecord(
                name="clone-vm",
                display_state="Changing",
                serial_port=2346,
                group="/Lab",
                raw_state="starting",
            ),
        ]

    def stop_runtime_vm(self, vm_name: str) -> None:
        """Record one runtime stop request."""
        self.stopped_vm = vm_name

    def stop_all_runtime_vms(self) -> list[str]:
        """Record a global runtime stop request."""
        self.stop_all_requested = True
        return ["base-vm", "clone-vm"]


class CliTests(unittest.TestCase):
    """Verify top-level CLI behavior."""

    def test_help_is_shown_without_arguments(self) -> None:
        """Show the root help when no arguments are supplied."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli([], service=FakeService(), stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: pysnap", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_help_flag_is_shown_without_error(self) -> None:
        """Show root help when ``--help`` is provided."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(["--help"], service=FakeService(), stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertIn("Manage VirtualBox appliance imports", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_clone_command_invokes_service(self) -> None:
        """Pass parsed clone arguments to the service layer."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["clone", "base-vm", "clone-vm", "-p", "2345", "net-a", "net-b"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            service.clone_args,
            ("base-vm", "clone-vm", 2345, ("net-a", "net-b")),
        )
        self.assertIn("Name: clone-vm", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_clone_command_rejects_more_than_three_networks(self) -> None:
        """Fail fast when too many internal network names are provided."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["clone", "base-vm", "clone-vm", "net-a", "net-b", "net-c", "net-d"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertIsNone(service.clone_args)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("at most three internal network names", stderr.getvalue())

    def test_list_command_formats_groups(self) -> None:
        """Render group listings in a human-readable way."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(["list"], service=FakeService(), stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertIn("Group: /Lab", stdout.getvalue())
        self.assertIn("- base-vm", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_import_command_formats_result_and_progress(self) -> None:
        """Render import progress and final import output."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["import", "~/Downloads/test.ova"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.import_args, ("~/Downloads/test.ova",))
        output = stdout.getvalue()
        self.assertIn("Importing [", output)
        self.assertIn("100%", output)
        self.assertIn("Imported virtual machines:", output)
        self.assertEqual("", stderr.getvalue())

    def test_bare_image_argument_is_rejected(self) -> None:
        """Require the explicit ``import`` subcommand for appliance imports."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["~/Downloads/test.ova"],
            service=FakeService(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("unknown command", stderr.getvalue())

    def test_integration_test_command_formats_result(self) -> None:
        """Render integration-test output through the CLI."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["--integration-test", "~/Downloads/test.ova"],
            service=FakeService(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Integration test completed successfully.", stdout.getvalue())
        self.assertIn("Monitor:", stdout.getvalue())
        self.assertIn("clone-a (state: Working ; 1025 ; /Lab)", stdout.getvalue())
        self.assertIn("Deletion order:", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_connect_command_runs_terminal_session(self) -> None:
        """Create a terminal session and hand control to it."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("pysnap.cli.app.TerminalSession") as session_class:
            session_class.return_value.run.return_value = 0

            exit_code = run_cli(
                ["connect", "base-vm"],
                service=FakeService(),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        session_class.assert_called_once()
        session_class.return_value.run.assert_called_once_with("base-vm")
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_monitor_command_formats_runtime_records(self) -> None:
        """Render compact runtime monitor output."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(["monitor"], service=FakeService(), stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("base-vm (state: Working ; 2345 ; /Lab)", output)
        self.assertIn("clone-vm (state: Changing ; 2346 ; /Lab)", output)
        self.assertEqual("", stderr.getvalue())

    def test_stop_command_invokes_single_vm_stop(self) -> None:
        """Stop one named VM through the service."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["stop", "base-vm"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.stopped_vm, "base-vm")
        self.assertIn("Stopped virtual machine: base-vm", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_stop_all_command_invokes_global_stop(self) -> None:
        """Stop all runtime VMs through the service."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["stop", "--all"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(service.stop_all_requested)
        self.assertIn("Stopped virtual machines: base-vm, clone-vm", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
