"""VirtualBox client abstraction built on top of ``VBoxManage``."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable, Protocol, Sequence

from pysnap.core.models import (
    ImportCandidate,
    SerialPortConfiguration,
    VMInfo,
    VMReference,
)
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
        pass

    def run_streaming(
        self,
        arguments: Sequence[str],
        output_callback: Callable[[str], None],
    ) -> str:
        """Execute a command and stream textual output chunks.

        :param arguments: Arguments passed to ``VBoxManage``.
        :param output_callback: Callback for output chunks.
        :returns: Combined command output.
        """
        pass


class SubprocessRunner(RunnerProtocol):
    """Run ``VBoxManage`` commands using :mod:`subprocess`."""

    MACOS_APP_BUNDLE_EXECUTABLE = Path(
        "/Applications/VirtualBox.app/Contents/MacOS/VBoxManage"
    )
    WINDOWS_INSTALL_DIRECTORY_ENV_VARS = (
        "VBOX_MSI_INSTALL_PATH",
        "VBOX_INSTALL_PATH",
    )
    WINDOWS_DEFAULT_EXECUTABLES = (
        r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
        r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
    )

    def __init__(self, executable: str | None = None) -> None:
        """Initialize the subprocess runner.

        The runner first checks an explicit executable path, then the
        ``VBOXMANAGE_EXECUTABLE`` environment variable, then ``PATH``.
        On macOS it also falls back to the standard VirtualBox app-bundle path.
        On Windows it checks the MSI install directory and the default
        ``Program Files`` locations.

        :param executable: Optional VirtualBox command line executable.
        """
        self.executable = self._resolve_executable(executable)

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

    def run_streaming(
        self,
        arguments: Sequence[str],
        output_callback: Callable[[str], None],
    ) -> str:
        """Execute ``VBoxManage`` and stream merged output chunks.

        :param arguments: Arguments passed to ``VBoxManage``.
        :param output_callback: Callback for output chunks.
        :returns: Combined command output.
        :raises CommandExecutionError: If the command exits unsuccessfully.
        """
        command = [self.executable, *arguments]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as error:
            raise CommandExecutionError(command, "", str(error)) from error

        assert process.stdout is not None
        chunks: list[str] = []
        while True:
            chunk = process.stdout.read(1)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            chunks.append(text)
            output_callback(text)

        process.wait()
        output = "".join(chunks)
        if process.returncode != 0:
            raise CommandExecutionError(command, output, "")
        return output

    def _resolve_executable(self, executable: str | None) -> str:
        """Resolve the executable path for ``VBoxManage``.

        :param executable: Explicit executable override, if any.
        :returns: Resolved executable path or command name.
        """
        requested = executable or os.environ.get("VBOXMANAGE_EXECUTABLE") or "VBoxManage"
        explicit_path = Path(requested).expanduser()
        if self._looks_like_filesystem_path(requested) and explicit_path.is_file():
            return str(explicit_path)

        resolved = shutil.which(requested)
        if resolved:
            return resolved

        if sys.platform == "win32":
            for candidate in self._iter_windows_fallback_executables():
                if Path(candidate).is_file():
                    return candidate

        if sys.platform == "darwin" and self.MACOS_APP_BUNDLE_EXECUTABLE.is_file():
            return str(self.MACOS_APP_BUNDLE_EXECUTABLE)

        return requested

    def _looks_like_filesystem_path(self, value: str) -> bool:
        """Return whether a command value looks like a filesystem path.

        :param value: Requested command value.
        :returns: ``True`` when the value should be checked as a path.
        """
        separators = {os.sep}
        if os.altsep:
            separators.add(os.altsep)
        return value.startswith("~") or any(separator in value for separator in separators)

    def _iter_windows_fallback_executables(self) -> tuple[str, ...]:
        """Return standard Windows candidates for ``VBoxManage.exe``.

        :returns: Candidate executable paths in resolution order.
        """
        candidates: list[str] = []
        for variable in self.WINDOWS_INSTALL_DIRECTORY_ENV_VARS:
            install_location = os.environ.get(variable)
            if not install_location:
                continue
            candidates.append(self._normalize_windows_vboxmanage_path(install_location))
        candidates.extend(self.WINDOWS_DEFAULT_EXECUTABLES)
        return tuple(dict.fromkeys(candidates))

    def _normalize_windows_vboxmanage_path(self, install_location: str) -> str:
        """Normalize one Windows VirtualBox install location to ``VBoxManage.exe``.

        :param install_location: Directory or executable path reported by the OS.
        :returns: Full ``VBoxManage.exe`` path candidate.
        """
        normalized = install_location.strip().strip('"')
        if normalized.lower().endswith(".exe"):
            return normalized
        return normalized.rstrip("\\/") + r"\VBoxManage.exe"


class VBoxManageClient:
    """Expose the subset of VirtualBox operations required by PySnap."""

    IMPORT_PROGRESS_PATTERN = re.compile(r"(?P<percent>\d{1,3})%")
    DMI_SYSTEM_VENDOR_KEY = "VBoxInternal/Devices/pcbios/0/Config/DmiSystemVendor"
    DMI_SYSTEM_SKU_KEY = "VBoxInternal/Devices/pcbios/0/Config/DmiSystemSKU"

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

    def list_running_vms(self) -> list[VMReference]:
        """List all currently running VMs.

        :returns: Parsed references for running VMs.
        """
        return parse_list_vms(self.runner.run(["list", "runningvms"]))

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
            vm_state=properties.get("VMState", "") or None,
            parent_name=metadata.get("pysnap/parent") or None,
            managed=metadata.get("pysnap/managed") == "true",
            metadata=metadata,
        )

    def get_serial_port_configuration(self, vm_name: str) -> SerialPortConfiguration:
        """Read the raw ``UART1`` configuration of a VM.

        :param vm_name: VM name to inspect.
        :returns: Parsed ``UART1`` configuration.
        """
        properties = self._get_vm_properties(vm_name)
        uart1 = (properties.get("uart1", "") or "").strip()
        uartmode1 = (properties.get("uartmode1", "") or "").strip()
        if not uart1 or uart1.lower() == "off":
            return SerialPortConfiguration(enabled=False)

        if not uartmode1:
            return SerialPortConfiguration(enabled=True)

        mode_parts = [part.strip() for part in uartmode1.split(",") if part.strip()]
        if not mode_parts:
            return SerialPortConfiguration(enabled=True)

        mode = mode_parts[0].lower()
        port = None
        if mode == "tcpserver" and len(mode_parts) >= 2:
            try:
                port = int(mode_parts[1])
            except ValueError:
                port = None
        return SerialPortConfiguration(enabled=True, mode=mode, port=port)

    def dry_run_import(self, image_path: str) -> list[ImportCandidate]:
        """Run an appliance import dry run.

        :param image_path: Appliance path.
        :returns: Import candidates discovered by VirtualBox.
        """
        output = self.runner.run(["import", image_path, "--dry-run"])
        return parse_import_candidates(output)

    def import_appliance(
        self,
        image_path: str,
        candidates: list[ImportCandidate],
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        """Import an appliance using the supplied dry-run candidates.

        :param image_path: Appliance path.
        :param candidates: Normalized import candidates.
        :param progress_callback: Optional import progress callback.
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
        if progress_callback is None:
            self.runner.run(arguments)
            return

        progress_callback(0)
        last_percent = 0
        buffer = ""

        def handle_output(chunk: str) -> None:
            nonlocal buffer, last_percent
            buffer = (buffer + chunk)[-128:]
            matches = list(self.IMPORT_PROGRESS_PATTERN.finditer(buffer))
            if not matches:
                return
            percent = min(int(matches[-1].group("percent")), 100)
            if percent > last_percent:
                last_percent = percent
                progress_callback(percent)

        self.runner.run_streaming(arguments, handle_output)
        if last_percent < 100:
            progress_callback(100)

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

    def start_vm_headless(self, vm_name: str) -> None:
        """Start a VM in headless mode.

        :param vm_name: VM name.
        """
        self.runner.run(["startvm", vm_name, "--type=headless"])

    def stop_vm_acpi(self, vm_name: str) -> None:
        """Request a graceful ACPI shutdown for a running VM.

        :param vm_name: VM name.
        """
        self.runner.run(["controlvm", vm_name, "acpipowerbutton"])

    def get_vm_state(self, vm_name: str) -> str:
        """Return the current VirtualBox runtime state of a VM.

        :param vm_name: VM name.
        :returns: Raw VirtualBox state string.
        """
        return (self._get_vm_properties(vm_name).get("VMState", "") or "").lower()

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
        """Configure clone networking without disabling inherited adapters.

        :param vm_name: VM name.
        :param networks: Internal network names mapped onto ``NIC2``-``NIC4``.
        :raises PySnapError: If more than three networks are provided.
        """
        if len(networks) > 3:
            raise PySnapError("At most three internal network names can be provided.")

        arguments: list[str] = ["modifyvm", vm_name, "--nic1", "nat"]
        for index, network_name in enumerate(networks, start=2):
            arguments.extend([f"--nic{index}", "intnet", f"--intnet{index}", network_name])
        self.runner.run(arguments)

    def set_metadata(self, vm_name: str, metadata: dict[str, str]) -> None:
        """Persist PySnap metadata in VirtualBox extra data.

        :param vm_name: VM name.
        :param metadata: Extra data entries to set.
        """
        for key, value in metadata.items():
            self.runner.run(["setextradata", vm_name, key, value])

    def configure_dmi_system_information(
        self,
        vm_name: str,
        system_vendor: str,
        system_sku: str,
    ) -> None:
        """Persist DMI system information for one VM.

        :param vm_name: VM name.
        :param system_vendor: DMI system vendor value.
        :param system_sku: DMI system SKU value.
        """
        self.runner.run(
            ["setextradata", vm_name, self.DMI_SYSTEM_VENDOR_KEY, system_vendor]
        )
        self.runner.run(["setextradata", vm_name, self.DMI_SYSTEM_SKU_KEY, system_sku])

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
