"""VirtualBox client abstraction built on top of ``VBoxManage``."""

from __future__ import annotations

import subprocess
from typing import Protocol, Sequence

from pysnap.core.models import ImportCandidate, VMInfo, VMReference
from pysnap.errors import CommandExecutionError, PySnapError
from pysnap.vbox.parsers import (
    parse_extra_data,
    parse_import_candidates,
    parse_list_vms,
    parse_machine_readable,
    parse_snapshot_names,
    split_groups,
)

class RunnerProtocol(Protocol):
    """Describe the command runner used by the client."""

    def run(self, arguments: Sequence[str]) -> str:
        """Execute a command and return its standard output.

        :param arguments: Arguments passed to ``VBoxManage``.
        :returns: Standard output.
        """


class SubprocessRunner:
    """Run ``VBoxManage`` commands using :mod:`subprocess`."""

    def __init__(self, executable: str = "VBoxManage") -> None:
        """Initialize the subprocess runner.

        :param executable: VirtualBox command line executable.
        """
        self.executable = executable

    def run(self, arguments: Sequence[str]) -> str:
        """Execute ``VBoxManage`` and return standard output.

        :param arguments: Arguments passed to ``VBoxManage``.
        :returns: Standard output text.
        :raises CommandExecutionError: If the command exits unsuccessfully.
        """
        command = [self.executable, *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
            )
        except FileNotFoundError as error:
            raise CommandExecutionError(command, "", str(error)) from error

        if completed.returncode != 0:
            raise CommandExecutionError(command, completed.stdout, completed.stderr)
        return completed.stdout


class VBoxManageClient:
    """Expose the subset of VirtualBox operations required by PySnap."""

    def __init__(self, runner: RunnerProtocol | None = None) -> None:
        """Initialize the VirtualBox client.

        :param runner: Command runner implementation.
        """
        self.runner = runner or SubprocessRunner()

    def list_vms(self) -> list[VMReference]:
        """List all registered VMs.

        :returns: Parsed VM references.
        """
        return parse_list_vms(self.runner.run(["list", "vms"]))

    def get_vm_info(self, vm_name: str) -> VMInfo:
        """Read detailed information about a VM.

        :param vm_name: VM name to inspect.
        :returns: Detailed VM information.
        """
        properties = self._get_vm_properties(vm_name)
        metadata = {
            key: value
            for key, value in self.get_metadata(vm_name).items()
            if key.startswith("pysnap/")
        }
        return VMInfo(
            name=properties.get("name", vm_name),
            uuid=properties.get("UUID", ""),
            groups=split_groups(properties.get("groups", "")),
            serial_port=self._parse_serial_port(properties),
            parent_name=metadata.get("pysnap/parent") or None,
            managed=metadata.get("pysnap/managed") == "true",
            metadata=metadata,
        )

    def dry_run_import(self, image_path: str) -> list[ImportCandidate]:
        """Run an appliance import dry run.

        :param image_path: Appliance path.
        :returns: Import candidates discovered by VirtualBox.
        """
        output = self.runner.run(["import", image_path, "--dry-run"])
        return parse_import_candidates(output)

    def import_appliance(
        self, image_path: str, candidates: list[ImportCandidate]
    ) -> None:
        """Import an appliance using the supplied dry-run candidates.

        :param image_path: Appliance path.
        :param candidates: Normalized import candidates.
        """
        arguments: list[str] = ["import", image_path]
        for candidate in candidates:
            arguments.extend(
                [
                    "--vsys",
                    str(candidate.vsys_index),
                    "--vmname",
                    candidate.vm_name,
                    "--group",
                    candidate.group,
                ]
            )
            if candidate.requires_eula_accept:
                arguments.extend(
                    ["--vsys", str(candidate.vsys_index), "--eula", "accept"]
                )
        self.runner.run(arguments)

    def get_current_snapshot_name(self, vm_name: str) -> str | None:
        """Return a snapshot name suitable for linked cloning.

        :param vm_name: VM name to inspect.
        :returns: Snapshot name, if any.
        """
        snapshots = parse_snapshot_names(self._get_vm_properties(vm_name))
        return snapshots[0] if snapshots else None

    def take_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        """Create a snapshot for a VM.

        :param vm_name: VM name.
        :param snapshot_name: Snapshot name to create.
        """
        self.runner.run(["snapshot", vm_name, "take", snapshot_name])

    def clone_linked(
        self, base_vm: str, clone_vm: str, group: str, snapshot_name: str
    ) -> None:
        """Create and register a linked clone.

        :param base_vm: Source VM name.
        :param clone_vm: Clone VM name.
        :param group: Group assigned to the clone.
        :param snapshot_name: Snapshot used as clone source.
        """
        self.runner.run(
            [
                "clonevm",
                base_vm,
                "--name",
                clone_vm,
                "--groups",
                group,
                "--snapshot",
                snapshot_name,
                "--options",
                "link",
                "--register",
            ]
        )

    def configure_serial_port(self, vm_name: str, serial_port: int) -> None:
        """Configure ``UART1`` as a TCP server on the provided host port.

        :param vm_name: VM name.
        :param serial_port: Host TCP port exposed through ``UART1``.
        :raises PySnapError: If the port number is unsupported.
        """
        if not 1 <= serial_port <= 65535:
            raise PySnapError("Serial TCP port must be between 1 and 65535.")

        self.runner.run(
            [
                "modifyvm",
                vm_name,
                "--uart1",
                "0x3F8",
                "4",
                "--uartmode1",
                "tcpserver",
                str(serial_port),
            ]
        )

    def configure_internal_networks(
        self, vm_name: str, networks: Sequence[str]
    ) -> None:
        """Configure up to three internal network adapters.

        :param vm_name: VM name.
        :param networks: Internal network names.
        :raises PySnapError: If more than three networks are provided.
        """
        if len(networks) > 3:
            raise PySnapError("At most three internal network names can be provided.")

        arguments: list[str] = ["modifyvm", vm_name]
        for index in range(1, 4):
            if index <= len(networks):
                arguments.extend(
                    [f"--nic{index}", "intnet", f"--intnet{index}", networks[index - 1]]
                )
            else:
                arguments.extend([f"--nic{index}", "none"])
        self.runner.run(arguments)

    def set_metadata(self, vm_name: str, metadata: dict[str, str]) -> None:
        """Persist PySnap metadata in VirtualBox extra data.

        :param vm_name: VM name.
        :param metadata: Extra data entries to set.
        """
        for key, value in metadata.items():
            self.runner.run(["setextradata", vm_name, key, value])

    def get_metadata(self, vm_name: str) -> dict[str, str]:
        """Read all extra data entries for a VM.

        :param vm_name: VM name.
        :returns: Extra data mapping.
        """
        return parse_extra_data(self.runner.run(["getextradata", vm_name, "enumerate"]))

    def delete_vm(self, vm_name: str) -> None:
        """Delete and unregister a VM.

        :param vm_name: VM name.
        """
        self.runner.run(["unregistervm", vm_name, "--delete"])

    def _get_vm_properties(self, vm_name: str) -> dict[str, str]:
        """Read machine-readable VM properties.

        :param vm_name: VM name.
        :returns: Parsed machine-readable properties.
        """
        return parse_machine_readable(
            self.runner.run(["showvminfo", vm_name, "--machinereadable"])
        )

    def _parse_serial_port(self, properties: dict[str, str]) -> int | None:
        """Extract the TCP port configured for ``UART1``.

        :param properties: Parsed machine-readable VM properties.
        :returns: TCP port or ``None`` when not configured as a TCP server.
        """
        uart1 = properties.get("uart1", "")
        uartmode1 = properties.get("uartmode1", "")
        if not uart1 or uart1.lower() == "off" or not uartmode1:
            return None
        mode_parts = [part.strip() for part in uartmode1.split(",") if part.strip()]
        if len(mode_parts) < 2 or mode_parts[0].lower() != "tcpserver":
            return None
        try:
            return int(mode_parts[1])
        except ValueError:
            return None
