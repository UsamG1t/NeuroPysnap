"""Unit tests for the PySnap CLI."""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from pysnap.cli.app import run_cli
from pysnap.errors import CommandExecutionError
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
        self.plugged_vm: str | None = None
        self.proto_settings_vm: str | None = None
        self.stopped_vm: str | None = None
        self.stop_all_requested = False
        self.erased_vm: str | None = None
        self.erase_clones_group: object = "unused"
        self.full_clean_paths: tuple | None = None
        self.clone_vm_names: set[str] = {"clone-vm"}
        self.list_groups_error: Exception | None = None

    def list_groups(self) -> list[VMGroup]:
        """Return a static group list or raise the configured error."""
        if self.list_groups_error is not None:
            raise self.list_groups_error
        return [VMGroup(name="/Lab", vm_names=("base-vm", "clone-vm"))]

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return a static VM description."""
        return VMInfo(name=vm_name, uuid="uuid", groups=("/Lab",), serial_port=2345)

    def plug_vm(self, vm_name: str) -> VMInfo:
        """Record a plug request and return the updated VM description."""
        self.plugged_vm = vm_name
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

    def import_image(
        self,
        image_path: str,
        vm_name: str | None = None,
        progress_callback=None,
    ) -> list[VMInfo]:
        """Return a static import result."""
        self.import_args = (image_path, vm_name)
        if progress_callback is not None:
            progress_callback(5)
            progress_callback(55)
            progress_callback(100)
        return [
            VMInfo(name=vm_name or "base-vm", uuid="uuid", groups=("/Others",))
        ]

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

    def register_proto_settings_vm(self, vm_name: str) -> tuple[str, ...]:
        """Record one proto-settings registration request."""
        self.proto_settings_vm = vm_name
        return ("base-vm", vm_name) if vm_name != "base-vm" else ("base-vm",)

    def erase_vm(self, vm_name: str) -> None:
        """Record one erase request."""
        self.erased_vm = vm_name

    def erase_group(self, group_name: str) -> list[str]:
        """Pretend to erase a group."""
        return ["base-vm", "clone-vm"]

    def erase_all(self) -> list[str]:
        """Pretend to erase all VMs."""
        return ["base-vm", "clone-vm"]

    def erase_clones(self, group_name: str | None = None) -> list[str]:
        """Record a clones-only erase request."""
        self.erase_clones_group = group_name
        return ["clone-vm"]

    def is_clone_vm(self, vm_name: str) -> bool:
        """Report whether one fake VM counts as a clone."""
        return vm_name in self.clone_vm_names

    def full_clean(self, paths) -> list[str]:
        """Record a full-clean request and report the paths as removed."""
        self.full_clean_paths = tuple(str(path) for path in paths)
        return [str(path) for path in paths]

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
            VMMonitorRecord(
                name="stopping-vm",
                display_state="Stopping",
                serial_port=2347,
                group="/Lab",
                raw_state="stopping",
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

    def test_list_command_reports_stub_when_vm_data_is_unavailable(self) -> None:
        """Report an empty listing when VBoxManage cannot deliver VM data."""
        service = FakeService()
        service.list_groups_error = CommandExecutionError(
            ["VBoxManage", "list", "vms"],
            "",
            "VBoxManage did not respond within 15.0 seconds.",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(["list"], service=service, stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertIn("No virtual machines found.", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_erase_clones_only_erases_all_clones(self) -> None:
        """Delete only clones when ``--clones-only`` is combined with ``--all``."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["erase", "--all", "--clones-only"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertIsNone(service.erase_clones_group)
        self.assertIn("Erased virtual machines: clone-vm", stdout.getvalue())

    def test_erase_clones_only_passes_group_filter(self) -> None:
        """Forward the group filter to clones-only deletion."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["erase", "--group", "Lab", "--clones-only"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.erase_clones_group, "Lab")
        self.assertIn("Erased virtual machines: clone-vm", stdout.getvalue())

    def test_erase_clones_only_deletes_single_clone(self) -> None:
        """Keep normal single-VM deletion behavior for clones."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["erase", "clone-vm", "--clones-only"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.erased_vm, "clone-vm")
        self.assertIn("Erased virtual machine: clone-vm", stdout.getvalue())

    def test_erase_clones_only_refuses_base_vm(self) -> None:
        """Warn and skip deletion when the target VM is not a clone."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["erase", "base-vm", "--clones-only"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 1)
        self.assertIsNone(service.erased_vm)
        self.assertIn(
            'Virtual machine "base-vm" is not a clone. Verify the operation '
            "or remove the --clones-only flag.",
            stderr.getvalue(),
        )

    def test_full_clean_requires_double_confirmation(self) -> None:
        """Delete the requested directories only after both confirmations."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("yes\ndelete\n")

        exit_code = run_cli(
            ["full-clean", "--path", "/tmp/custom-vms", "--path", "/tmp/custom-config"],
            service=service,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            service.full_clean_paths,
            (str(Path("/tmp/custom-vms")), str(Path("/tmp/custom-config"))),
        )
        self.assertIn("will be permanently deleted", stdout.getvalue())
        self.assertIn("Removed directories:", stdout.getvalue())

    def test_full_clean_cancels_without_second_confirmation(self) -> None:
        """Cancel the cleanup when the second confirmation does not match."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("yes\nno\n")

        exit_code = run_cli(
            ["full-clean", "--path", "/tmp/custom-vms"],
            service=service,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
        )

        self.assertEqual(exit_code, 1)
        self.assertIsNone(service.full_clean_paths)
        self.assertIn("Full clean cancelled.", stdout.getvalue())

    def test_full_clean_cancels_on_end_of_input(self) -> None:
        """Cancel the cleanup when no confirmation can be read."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("")

        exit_code = run_cli(
            ["full-clean", "--path", "/tmp/custom-vms"],
            service=service,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
        )

        self.assertEqual(exit_code, 1)
        self.assertIsNone(service.full_clean_paths)
        self.assertIn("Full clean cancelled.", stdout.getvalue())

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
        self.assertEqual(service.import_args, ("~/Downloads/test.ova", None))
        output = stdout.getvalue()
        self.assertIn("Importing [", output)
        self.assertIn("100%", output)
        self.assertIn("Imported virtual machines:", output)
        self.assertEqual("", stderr.getvalue())

    def test_import_command_accepts_custom_vm_name(self) -> None:
        """Pass the optional VM name through to the service layer."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["import", "~/Downloads/test.ova", "renamed-vm"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.import_args, ("~/Downloads/test.ova", "renamed-vm"))
        self.assertIn("- renamed-vm (/Others)", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_docs_command_opens_bundled_documentation(self) -> None:
        """Launch the packaged documentation viewer command."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("pysnap.cli.app.open_bundled_documentation") as open_docs:
            exit_code = run_cli(["docs"], service=FakeService(), stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        open_docs.assert_called_once_with(browser=None)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_docs_command_accepts_explicit_browser(self) -> None:
        """Pass an explicit browser executable to the docs helper."""
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("pysnap.cli.app.open_bundled_documentation") as open_docs:
            exit_code = run_cli(
                ["docs", "--browser", "/usr/bin/chromium"],
                service=FakeService(),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        open_docs.assert_called_once_with(browser="/usr/bin/chromium")
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_protosettings_command_registers_vm(self) -> None:
        """Register a VM for proto-settings through the service layer."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["protosettings", "base-vm"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.proto_settings_vm, "base-vm")
        self.assertIn("Registered proto-settings VM: base-vm", stdout.getvalue())
        self.assertIn("Configured proto-settings VMs: base-vm", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_plug_command_configures_vm_for_connection(self) -> None:
        """Prepare one VM for PySnap serial-console connections."""
        service = FakeService()
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = run_cli(
            ["plug", "base-vm"],
            service=service,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.plugged_vm, "base-vm")
        self.assertIn("Name: base-vm", stdout.getvalue())
        self.assertIn("Serial port: 2345", stdout.getvalue())
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
        self.assertIn("stopping-vm (state: Stopping ; 2347 ; /Lab)", output)
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
