"""Unit tests for the VirtualBox client."""

from __future__ import annotations

import unittest

from pysnap.core.models import ImportCandidate
from pysnap.vbox.client import VBoxManageClient


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
                    'uart1="0x3F8,4"\n'
                    'uartmode1="tcpserver,2345"\n'
                ),
                ("getextradata", "srv", "enumerate"): "No extra data items configured.\n",
            }
        )
        client = VBoxManageClient(runner=runner)

        vm_info = client.get_vm_info("srv")

        self.assertEqual(vm_info.serial_port, 2345)

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
