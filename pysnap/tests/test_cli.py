"""Unit tests for the PySnap CLI."""

from __future__ import annotations

import io
import unittest

from pysnap.cli.app import run_cli
from pysnap.core.models import IntegrationTestResult, VMGroup, VMInfo


class FakeService:
    """Provide a controllable fake service for CLI tests."""

    def __init__(self) -> None:
        """Initialize fake outputs and call tracking."""
        self.clone_args: tuple | None = None

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

    def import_image(self, image_path: str) -> list[VMInfo]:
        """Return a static import result."""
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
        )

    def erase_vm(self, vm_name: str) -> None:
        """Pretend to erase one VM."""

    def erase_group(self, group_name: str) -> list[str]:
        """Pretend to erase a group."""
        return ["base-vm", "clone-vm"]

    def erase_all(self) -> list[str]:
        """Pretend to erase all VMs."""
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
        self.assertIn("Deletion order:", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
