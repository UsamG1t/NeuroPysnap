"""Unit tests for the VirtualBox client."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pysnap.core.models import ImportCandidate
from pysnap.vbox.client import SubprocessRunner, VBoxManageClient


class FakeRunner:
    """Collect issued VBoxManage commands and return configured outputs."""

    def __init__(self, outputs: dict[tuple[str, ...], str] | None = None) -> None:
        """Initialize the fake runner.

        :param outputs: Mapping of commands to command output.
        """
        self.outputs = outputs or {}
        self.commands: list[tuple[str, ...]] = []

    def run(self, arguments: tuple[str, ...] | list[str]) -> str:
        """Record a command and return the configured output.

        :param arguments: VBoxManage command arguments.
        :returns: Configured command output.
        """
        command = tuple(arguments)
        self.commands.append(command)
        return self.outputs.get(command, "")


class VBoxManageClientTests(unittest.TestCase):
    """Verify VirtualBox client behavior."""

    def test_subprocess_runner_uses_macos_bundle_path_when_not_in_path(self) -> None:
        """Resolve the documented macOS VBoxManage bundle path automatically."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pysnap.vbox.client.shutil.which", return_value=None),
            patch("pysnap.vbox.client.sys.platform", "darwin"),
            patch.object(
                SubprocessRunner.MACOS_APP_BUNDLE_EXECUTABLE.__class__,
                "is_file",
                return_value=True,
            ),
        ):
            runner = SubprocessRunner()

        self.assertEqual(
            runner.executable,
            "/Applications/VirtualBox.app/Contents/MacOS/VBoxManage",
        )

    def test_subprocess_runner_prefers_environment_override(self) -> None:
        """Honor an explicit environment override for VBoxManage."""
        with (
            patch.dict(
                os.environ,
                {"VBOXMANAGE_EXECUTABLE": "/custom/VBoxManage"},
                clear=True,
            ),
            patch("pysnap.vbox.client.shutil.which", return_value=None),
            patch("pysnap.vbox.client.Path.is_file", return_value=True),
        ):
            runner = SubprocessRunner()

        self.assertEqual(runner.executable, "/custom/VBoxManage")

    def test_configure_serial_port_uses_uart1_tcpserver(self) -> None:
        """Configure UART1 as a TCP server on the requested host port."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)

        client.configure_serial_port("srv", 2345)

        self.assertEqual(
            runner.commands,
            [
                (
                    "modifyvm",
                    "srv",
                    "--uart1",
                    "0x3F8",
                    "4",
                    "--uartmode1",
                    "tcpserver",
                    "2345",
                )
            ],
        )

    def test_get_vm_info_reads_serial_tcp_port(self) -> None:
        """Read the TCP port configured in UART1 machine-readable properties."""
        runner = FakeRunner(
            outputs={
                ("showvminfo", "srv", "--machinereadable"): (
                    'name="srv"\n'
                    'UUID="uuid-srv"\n'
                    'groups="/Lab"\n'
                    'VMState="running"\n'
                    'uart1="0x3F8,4"\n'
                    'uartmode1="tcpserver,2345"\n'
                ),
                ("getextradata", "srv", "enumerate"): "No extra data items configured.\n",
            }
        )
        client = VBoxManageClient(runner=runner)

        vm_info = client.get_vm_info("srv")

        self.assertEqual(vm_info.serial_port, 2345)
        self.assertEqual(vm_info.vm_state, "running")

    def test_start_vm_headless_uses_headless_runtime(self) -> None:
        """Start the VM through the VirtualBox headless runtime."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)

        client.start_vm_headless("srv")

        self.assertEqual(runner.commands, [("startvm", "srv", "--type=headless")])

    def test_stop_vm_acpi_uses_power_button(self) -> None:
        """Stop the VM through an ACPI power button event."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)

        client.stop_vm_acpi("srv")

        self.assertEqual(runner.commands, [("controlvm", "srv", "acpipowerbutton")])

    def test_import_appliance_passes_vmname_and_group(self) -> None:
        """Pass renamed VM metadata to ``VBoxManage import``."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)

        client.import_appliance(
            "/tmp/test.ova",
            [
                ImportCandidate(
                    vsys_index=0,
                    vm_name="renamed-base",
                    group="/Lab",
                    requires_eula_accept=True,
                )
            ],
        )

        self.assertEqual(
            runner.commands,
            [
                (
                    "import",
                    "/tmp/test.ova",
                    "--vsys",
                    "0",
                    "--vmname",
                    "renamed-base",
                    "--group",
                    "/Lab",
                    "--vsys",
                    "0",
                    "--eula",
                    "accept",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
