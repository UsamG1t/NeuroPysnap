"""Application service layer for VBoxLab."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import shutil
import socket
from time import monotonic, sleep
from typing import Callable, ContextManager, Iterator, Sequence
from uuid import uuid4

from vboxlab.core.appliance import read_appliance_vm_names
from vboxlab.core.models import (
    ComPortSetting,
    ImportCandidate,
    IntegrationTestResult,
    VMGroup,
    VMHardware,
    VMInfo,
    VMMonitorRecord,
    SshKeyResult,
    VMSettingsResult,
)
from vboxlab.core.access import (
    ACCESS_KEY,
    DEFAULT_SSH_USER,
    LAB_KEY,
    SSH_USER_KEY,
    AccessInfo,
    resolve_access,
    validate_ssh_user,
)
from vboxlab.ssh import client as ssh_client
from vboxlab.ssh.client import SshTarget
from vboxlab.ssh.keys import SshKeyStore
from vboxlab.core.settings import ComPortRequest, com_setting_from_request, parse_set_options
from vboxlab.errors import (
    CommandExecutionError,
    VBoxLabError,
    VMDependencyError,
    VMNotFoundError,
)
from vboxlab.runtime.sessions import SessionRegistry
from vboxlab.terminal.transport import serial_connection_probe
from vboxlab.vbox.client import VBoxManageClient

SerialProbeFactory = Callable[[str, int], ContextManager[object]]
ImportProgressCallback = Callable[[int], None]


def normalize_group_name(group: str | None) -> str:
    """Normalize a VirtualBox group name.

    :param group: Raw group name.
    :returns: Group name that always starts with ``/``.
    """
    if not group or group == "/":
        return "/Others"
    return group if group.startswith("/") else f"/{group}"


class VBoxLabService:
    """Provide the main VBoxLab business operations."""

    SNAPSHOT_NAME = "vboxlab-base"
    DEFAULT_SERIAL_TCP_PORT = 1024
    DEFAULT_START_TIMEOUT = 30.0
    DEFAULT_STOP_TIMEOUT = 60.0
    DEFAULT_PAUSE_TIMEOUT = 30.0
    DEFAULT_SSH_WAIT_TIMEOUT = 180.0
    DEFAULT_INTEGRATION_STOP_TIMEOUT = 180.0
    STOPPED_STATES = {"poweroff", "saved", "aborted"}
    STOPPING_STATES = {"stopping"}
    PAUSED_STATES = {"paused"}
    ERROR_STATES = {"gurumeditation", "stuck"}

    def __init__(
        self,
        client: VBoxManageClient | None = None,
        session_registry: SessionRegistry | None = None,
        serial_probe_factory: SerialProbeFactory | None = None,
        ssh_key_store: SshKeyStore | None = None,
    ) -> None:
        """Initialize the service.

        :param client: Optional VirtualBox client implementation.
        :param ssh_key_store: Optional VBoxLab ssh key store.
        """
        self.client = client or VBoxManageClient()
        self.session_registry = session_registry or SessionRegistry()
        self.serial_probe_factory = serial_probe_factory or serial_connection_probe
        self.ssh_key_store = ssh_key_store or SshKeyStore()

    def import_image(
        self,
        image_path: str,
        vm_name: str | None = None,
        progress_callback: ImportProgressCallback | None = None,
    ) -> list[VMInfo]:
        """Import an OVA or OVF image into VirtualBox.

        :param image_path: Path to the OVA or OVF appliance.
        :param vm_name: Optional custom name for a single imported VM.
        :param progress_callback: Optional import progress callback.
        :returns: Imported VM information objects.
        :raises VBoxLabError: If the file is invalid or import does not create VMs.
        """
        image = Path(image_path).expanduser().resolve()
        if image.suffix.lower() not in {".ova", ".ovf"}:
            raise VBoxLabError("Only .ova and .ovf images can be imported.")
        if not image.exists():
            raise VBoxLabError(f'Image "{image}" does not exist.')

        declared_vm_names = read_appliance_vm_names(image)
        planned_imports = self.client.dry_run_import(str(image))
        if not planned_imports:
            raise VBoxLabError("The appliance dry run did not expose any virtual systems.")
        if vm_name is not None and len(planned_imports) != 1:
            raise VBoxLabError(
                "Custom VMName can only be used when the appliance contains exactly one VM."
            )
        if len(declared_vm_names) != len(planned_imports):
            raise VBoxLabError(
                "The appliance descriptor VM count does not match the VirtualBox dry run."
            )

        normalized_imports = [
            ImportCandidate(
                vsys_index=item.vsys_index,
                vm_name=vm_name if vm_name is not None else item.vm_name,
                group=normalize_group_name(item.group),
                requires_eula_accept=item.requires_eula_accept,
            )
            for item in planned_imports
        ]

        before_names = {vm.name for vm in self.client.list_vms()}
        final_candidate_names = (
            [vm_name]
            if vm_name is not None
            else list(declared_vm_names)
        )
        colliding_names = sorted(name for name in final_candidate_names if name in before_names)
        if colliding_names:
            names_text = ", ".join(colliding_names)
            if vm_name is None and len(colliding_names) == 1 and len(declared_vm_names) == 1:
                raise VBoxLabError(
                    f'Virtual machine "{colliding_names[0]}" already exists. '
                    "Use the optional VMName parameter to choose a different name."
                )
            if vm_name is not None and len(colliding_names) == 1:
                raise VBoxLabError(
                    f'Virtual machine "{colliding_names[0]}" already exists. '
                    "Choose a different VMName."
                )
            raise VBoxLabError(
                f"Virtual machine names already exist: {names_text}. "
                "Import aborted before creating duplicate VM names."
            )

        self.client.import_appliance(
            str(image),
            normalized_imports,
            progress_callback=progress_callback,
        )
        after_references = self.client.list_vms()
        after_names = {vm.name for vm in after_references}
        imported_names = sorted(after_names - before_names)

        if not imported_names:
            imported_names = [
                item.vm_name for item in normalized_imports if item.vm_name in after_names
            ]

        if not imported_names:
            raise VBoxLabError("The appliance import finished, but no imported VMs were detected.")

        imported_infos: list[VMInfo] = []
        for vm_name in imported_names:
            self.client.set_metadata(
                vm_name,
                {
                    "vboxlab/managed": "true",
                    "vboxlab/kind": "base",
                    "vboxlab/source": str(image),
                },
            )
            imported_infos.append(self.client.get_vm_info(vm_name))
        return imported_infos

    def run_integration_test(
        self,
        image_path: str,
        name_token: str | None = None,
    ) -> IntegrationTestResult:
        """Run an end-to-end integration scenario on an appliance.

        The scenario imports one appliance VM, creates three linked clones
        connected in a triangle of internal networks, captures machine details,
        validates runtime monitoring by attaching to one clone and starting a
        second clone headlessly, and removes all created VMs one by one.

        :param image_path: Path to the appliance used for the test.
        :param name_token: Optional deterministic suffix for generated VM names.
        :returns: Integration test result with machine info and deletion order.
        :raises VBoxLabError: If the appliance is invalid or the test fails.
        """
        image = Path(image_path).expanduser().resolve()
        if image.suffix.lower() not in {".ova", ".ovf"}:
            raise VBoxLabError("Only .ova and .ovf images can be used for integration tests.")
        if not image.exists():
            raise VBoxLabError(f'Image "{image}" does not exist.')

        planned_imports = self.client.dry_run_import(str(image))
        if len(planned_imports) != 1:
            raise VBoxLabError(
                "Integration test expects an appliance containing exactly one VM."
            )

        token = name_token or uuid4().hex[:8]
        imported_vm_name = f"vboxlab-it-{token}-base"
        clone_names = (
            f"vboxlab-it-{token}-clone-a",
            f"vboxlab-it-{token}-clone-b",
            f"vboxlab-it-{token}-clone-c",
        )

        renamed_import = ImportCandidate(
            vsys_index=planned_imports[0].vsys_index,
            vm_name=imported_vm_name,
            group=normalize_group_name(planned_imports[0].group),
            requires_eula_accept=planned_imports[0].requires_eula_accept,
        )

        created_vm_names: list[str] = []
        machines: list[VMInfo] = []
        monitor_records: list[VMMonitorRecord] = []
        deleted_vm_names: list[str] = []

        try:
            self.client.import_appliance(str(image), [renamed_import])
            self.client.set_metadata(
                imported_vm_name,
                {
                    "vboxlab/managed": "true",
                    "vboxlab/kind": "base",
                    "vboxlab/source": str(image),
                    # The scenario checks the COM1 console of protocol images.
                    ACCESS_KEY: "com",
                },
            )
            created_vm_names.append(imported_vm_name)

            self.clone_vm(
                base_vm=imported_vm_name,
                clone_vm=clone_names[0],
                networks=("intnet", "virtnet"),
            )
            created_vm_names.append(clone_names[0])

            self.clone_vm(
                base_vm=imported_vm_name,
                clone_vm=clone_names[1],
                networks=("intnet", "deepnet"),
            )
            created_vm_names.append(clone_names[1])

            self.clone_vm(
                base_vm=imported_vm_name,
                clone_vm=clone_names[2],
                networks=("deepnet", "virtnet"),
            )
            created_vm_names.append(clone_names[2])

            machines = [self.show_vm(vm_name) for vm_name in created_vm_names]
            monitor_records = self._run_integration_runtime_checks(
                connected_vm_name=clone_names[0],
                active_vm_name=clone_names[1],
            )
            self._stop_integration_runtime_vms(
                (clone_names[0], clone_names[1]),
                timeout=self.DEFAULT_INTEGRATION_STOP_TIMEOUT,
            )

            for vm_name in clone_names:
                self.erase_vm(vm_name)
                deleted_vm_names.append(vm_name)
            self.erase_vm(imported_vm_name)
            deleted_vm_names.append(imported_vm_name)

            return IntegrationTestResult(
                machines=tuple(machines),
                deleted_vm_names=tuple(deleted_vm_names),
                monitor_records=tuple(monitor_records),
            )
        except Exception as error:
            cleanup_errors = self._cleanup_integration_vms(created_vm_names, deleted_vm_names)
            if cleanup_errors:
                cleanup_message = "; ".join(cleanup_errors)
                raise VBoxLabError(
                    f"Integration test failed: {error}. Cleanup issues: {cleanup_message}"
                ) from error
            raise

    def list_groups(self) -> list[VMGroup]:
        """List all VM groups and their members.

        :returns: Group information sorted by group name.
        """
        groups: dict[str, list[str]] = {}
        for vm_info in self._collect_vm_infos():
            for group in vm_info.groups or ("/Others",):
                groups.setdefault(group, []).append(vm_info.name)

        return [
            VMGroup(name=group_name, vm_names=tuple(sorted(vm_names)))
            for group_name, vm_names in sorted(groups.items())
        ]

    def list_monitored_vms(self) -> list[VMMonitorRecord]:
        """List VMs that are running or changing state in a compact form.

        :returns: Monitor records sorted by VM name.
        """
        live_sessions = self.session_registry.list_live_sessions()
        records: list[VMMonitorRecord] = []
        for vm_info in self._collect_vm_infos():
            display_state = self._display_state(
                raw_state=(vm_info.vm_state or "").lower(),
                has_live_session=vm_info.name in live_sessions,
            )
            if display_state == "Stopped":
                continue
            records.append(
                VMMonitorRecord(
                    name=vm_info.name,
                    display_state=display_state,
                    serial_port=vm_info.serial_port,
                    group=vm_info.primary_group,
                    raw_state=(vm_info.vm_state or "").lower(),
                    access_mode=resolve_access(vm_info).mode,
                    ssh_port=vm_info.ssh_port,
                )
            )
        return sorted(records, key=lambda record: record.name)

    def get_monitor_state_label(self, vm_name: str) -> str:
        """Return the user-facing runtime label for one VM.

        :param vm_name: VM name.
        :returns: User-facing monitor label.
        """
        vm_info = self._require_vm(vm_name)
        has_live_session = self.session_registry.get_live_session(vm_name) is not None
        return self._display_state(
            raw_state=(vm_info.vm_state or "").lower(),
            has_live_session=has_live_session,
        )

    def prepare_vm_connection(
        self,
        vm_name: str,
        timeout: float | None = None,
    ) -> VMInfo:
        """Ensure that a VM is ready for an interactive connection (COM1 or ssh).

        :param vm_name: VM name.
        :param timeout: Optional startup timeout in seconds.
        :returns: Updated VM information.
        :raises VBoxLabError: If the VM cannot be connected.
        """
        vm_info = self._require_vm(vm_name)
        access = self._access_of(vm_info)
        if access.mode == "ssh" and access.ssh_port is None:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is reached through ssh but has no forwarded '
                f"ssh port; run vboxlab plug {vm_name}."
            )
        if access.mode == "com" and vm_info.serial_port is None:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" does not have a TCP-backed serial port.'
            )
        if self.session_registry.get_live_session(vm_name) is not None:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" already has an active terminal session.'
            )

        raw_state = (vm_info.vm_state or "").lower()
        if raw_state == "running":
            return vm_info
        if raw_state in self.PAUSED_STATES:
            if access.mode == "ssh":
                # A paused guest cannot answer the ssh login.
                raise VBoxLabError(
                    f'Virtual machine "{vm_name}" is paused; ssh needs a running guest. '
                    f"Run vboxlab resume {vm_name}."
                )
            return vm_info
        if raw_state in self.ERROR_STATES:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is in an error state: {raw_state}.'
            )

        effective_timeout = timeout or self.DEFAULT_START_TIMEOUT
        if raw_state in self.STOPPED_STATES:
            self.client.start_vm_headless(vm_name)

        return self._wait_for_vm_state(
            vm_name=vm_name,
            acceptable_states={"running"},
            timeout=effective_timeout,
            action_description="become ready for connection",
        )

    def stop_runtime_vm(
        self,
        vm_name: str,
        timeout: float | None = None,
    ) -> None:
        """Request a graceful stop for one running VM.

        :param vm_name: VM name.
        :param timeout: Optional timeout in seconds.
        :raises VBoxLabError: If the VM is not in a stoppable state.
        """
        vm_info = self._require_vm(vm_name)
        display_state = self.get_monitor_state_label(vm_name)

        if display_state == "Stopped":
            raise VBoxLabError(f'Virtual machine "{vm_name}" is not running.')
        if display_state == "Stopping":
            raise VBoxLabError(f'Virtual machine "{vm_name}" is already stopping.')
        if display_state == "Changing":
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is changing state; try again later.'
            )
        if display_state == "Paused":
            # A paused guest cannot handle the ACPI power button.
            self._resume_vm_and_wait(vm_name, timeout)
            display_state = "Active"
        if display_state not in {"Working", "Active"}:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" cannot be stopped from state {display_state}.'
            )

        self.client.stop_vm_acpi(vm_name)
        self._wait_for_vm_state(
            vm_name=vm_name,
            acceptable_states=self.STOPPED_STATES,
            timeout=timeout or self.DEFAULT_STOP_TIMEOUT,
            action_description="stop gracefully",
        )

    def stop_all_runtime_vms(self, timeout: float | None = None) -> list[str]:
        """Stop all currently running VMs that are in a normal running state.

        :param timeout: Optional per-VM timeout in seconds.
        :returns: Names of VMs that received a stop request and stopped.
        """
        records = self.list_monitored_vms()
        paused_names = [record.name for record in records if record.display_state == "Paused"]
        resume_failures = self._run_parallel_vm_actions(
            paused_names, lambda vm_name: self._resume_vm_and_wait(vm_name, timeout)
        )
        if resume_failures:
            raise VBoxLabError(
                "Unable to resume paused VMs before stopping them: "
                + "; ".join(f"{name}: {message}" for name, message in sorted(resume_failures.items()))
            )
        stoppable_names = sorted(
            [record.name for record in records if record.display_state in {"Working", "Active"}]
            + paused_names
        )
        return self._stop_runtime_vm_names(stoppable_names, timeout=timeout)

    def pause_runtime_vm(self, vm_name: str, timeout: float | None = None) -> None:
        """Pause one running VM; it stays in memory until it is resumed.

        :param vm_name: VM name.
        :param timeout: Optional timeout in seconds.
        :raises VBoxLabError: If the VM is not running.
        """
        display_state = self.get_monitor_state_label(vm_name)
        if display_state == "Paused":
            raise VBoxLabError(f'Virtual machine "{vm_name}" is already paused.')
        if display_state not in {"Working", "Active"}:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" cannot be paused from state {display_state}.'
            )
        self.client.pause_vm(vm_name)
        self._wait_for_vm_state(
            vm_name=vm_name,
            acceptable_states=self.PAUSED_STATES,
            timeout=timeout or self.DEFAULT_PAUSE_TIMEOUT,
            action_description="pause",
        )

    def resume_runtime_vm(self, vm_name: str, timeout: float | None = None) -> None:
        """Resume one paused VM.

        :param vm_name: VM name.
        :param timeout: Optional timeout in seconds.
        :raises VBoxLabError: If the VM is not paused.
        """
        display_state = self.get_monitor_state_label(vm_name)
        if display_state != "Paused":
            raise VBoxLabError(f'Virtual machine "{vm_name}" is not paused ({display_state}).')
        self._resume_vm_and_wait(vm_name, timeout)

    def pause_all_runtime_vms(self, timeout: float | None = None) -> list[str]:
        """Pause all running VMs.

        :param timeout: Optional per-VM timeout in seconds.
        :returns: Names of the paused VMs.
        :raises VBoxLabError: If one or more VMs cannot be paused.
        """
        names = [
            record.name
            for record in self.list_monitored_vms()
            if record.display_state in {"Working", "Active"}
        ]
        return self._change_vm_states(
            names,
            self.client.pause_vm,
            self.PAUSED_STATES,
            timeout or self.DEFAULT_PAUSE_TIMEOUT,
            "pause",
        )

    def resume_all_runtime_vms(self, timeout: float | None = None) -> list[str]:
        """Resume all paused VMs.

        :param timeout: Optional per-VM timeout in seconds.
        :returns: Names of the resumed VMs.
        :raises VBoxLabError: If one or more VMs cannot be resumed.
        """
        names = [
            record.name for record in self.list_monitored_vms() if record.display_state == "Paused"
        ]
        return self._change_vm_states(
            names,
            self.client.resume_vm,
            {"running"},
            timeout or self.DEFAULT_PAUSE_TIMEOUT,
            "resume",
        )

    def _resume_vm_and_wait(self, vm_name: str, timeout: float | None) -> None:
        """Resume a paused VM and wait until it runs."""
        self.client.resume_vm(vm_name)
        self._wait_for_vm_state(
            vm_name=vm_name,
            acceptable_states={"running"},
            timeout=timeout or self.DEFAULT_PAUSE_TIMEOUT,
            action_description="resume",
        )

    def _change_vm_states(
        self,
        vm_names: list[str],
        request: Callable[[str], object],
        acceptable_states: set[str],
        timeout: float,
        action: str,
    ) -> list[str]:
        """Request a state change for several VMs in parallel and wait for it.

        :param vm_names: VM names.
        :param request: Client call that requests the change.
        :param acceptable_states: States that complete the change.
        :param timeout: Per-VM timeout in seconds.
        :param action: Verb used in messages, such as ``"pause"``.
        :returns: Sorted names of the changed VMs.
        :raises VBoxLabError: If one or more VMs did not change.
        """
        failures = self._run_parallel_vm_actions(vm_names, request)
        requested = [name for name in vm_names if name not in failures]
        failures.update(
            self._run_parallel_vm_actions(
                requested,
                lambda vm_name: self._wait_for_vm_state(
                    vm_name=vm_name,
                    acceptable_states=acceptable_states,
                    timeout=timeout,
                    action_description=action,
                ),
            )
        )
        if failures:
            raise VBoxLabError(
                f"Unable to {action} all requested VMs: "
                + "; ".join(f"{name}: {message}" for name, message in sorted(failures.items()))
            )
        return sorted(vm_names)

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return details for a single VM.

        :param vm_name: Name of the VM to inspect.
        :returns: Detailed VM information.
        :raises VMNotFoundError: If the VM is absent.
        """
        self._require_vm(vm_name)
        return self.client.get_vm_info(vm_name)

    def plug_vm(self, vm_name: str) -> VMInfo:
        """Configure one VM for VBoxLab connections.

        A VM reached through COM1 gets ``UART1`` as a TCP server: if it already
        is one, the VM is returned unchanged; otherwise VBoxLab assigns a free
        TCP port and enables the same ``UART1`` tcpserver mode used by the
        imported protocol images. A VM reached through ssh gets a NAT rule that
        forwards a free host port to port 22 of the guest.

        :param vm_name: VM name to configure.
        :returns: Updated VM information.
        :raises VBoxLabError: If the VM cannot be configured safely.
        """
        vm_info = self._require_vm(vm_name)
        if self._access_of(vm_info).mode == "ssh":
            return self._plug_ssh(vm_name, vm_info)
        serial_config = self.client.get_serial_port_configuration(vm_name)
        if serial_config.mode == "tcpserver" and serial_config.port is not None:
            return self.client.get_vm_info(vm_name)
        if serial_config.enabled and serial_config.mode not in {None, "tcpserver"}:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" already uses UART1 mode '
                f'"{serial_config.mode}".'
            )

        raw_state = (vm_info.vm_state or "").lower()
        if raw_state not in self.STOPPED_STATES:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" must be stopped before UART1 can be '
                "plugged as a TCP server."
            )

        serial_port = self._allocate_serial_port(require_host_availability=True)
        self.client.configure_serial_port(vm_name, serial_port)
        return self.client.get_vm_info(vm_name)

    def _first_nat_adapter(self, vm_name: str) -> int | None:
        """Return the number of the first NAT adapter of a VM, if any."""
        hardware = self.client.get_vm_hardware(vm_name)
        return next(
            (number for number, nic in enumerate(hardware.nics, start=1) if nic.attachment == "nat"),
            None,
        )

    def _plug_ssh(self, vm_name: str, vm_info: VMInfo) -> VMInfo:
        """Forward a free host port to the guest ssh server.

        :param vm_name: VM name.
        :param vm_info: Current VM information.
        :returns: Updated VM information.
        :raises VBoxLabError: Without a NAT adapter or in a transitional state.
        """
        if vm_info.ssh_port is not None:
            return vm_info
        adapter = self._first_nat_adapter(vm_name)
        if adapter is None:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is reached through ssh but has no NAT adapter; '
                f"run vboxlab set {vm_name} nic1=nat first."
            )
        raw_state = (vm_info.vm_state or "").lower()
        if raw_state in self.STOPPED_STATES:
            running = False
        elif raw_state in {"running", *self.PAUSED_STATES}:
            running = True
        else:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is changing state; try again later.'
            )
        port = self._allocate_serial_port(require_host_availability=True)
        self.client.add_ssh_forwarding(vm_name, adapter, port, running=running)
        return self.client.get_vm_info(vm_name)

    def set_vm_settings(self, vm_name: str, options: Sequence[str]) -> VMSettingsResult:
        """Change the serial ports and network adapters of a stopped VM.

        Without options the current settings are returned unchanged. All
        options are validated before one ``modifyvm`` call applies them. For
        proto-settings clones the DMI system SKU is rebuilt from the new COM1
        port and the internal networks of NIC2-NIC4.

        :param vm_name: VM name.
        :param options: ``comN=VALUE`` and ``nicN=VALUE`` options.
        :returns: Settings after the change and warnings.
        :raises VBoxLabError: For invalid options, a VM that is not stopped or
            TCP ports in use.
        """
        request = parse_set_options(options)
        vm_info = self._require_vm(vm_name)
        current = self.client.get_vm_hardware(vm_name)
        if request.empty:
            return VMSettingsResult(
                vm_name, vm_info.vm_state, current, access=self._access_of(vm_info)
            )
        for number in request.com_ports:
            if number > len(current.com_ports):
                raise VBoxLabError(
                    f"COM{number}: VirtualBox offers COM1-COM{len(current.com_ports)}."
                )
        for number in request.nics:
            if number > len(current.nics):
                raise VBoxLabError(
                    f'NIC{number}: virtual machine "{vm_name}" offers NIC1-NIC{len(current.nics)} '
                    "with its chipset."
                )
        if request.hardware_changes and (vm_info.vm_state or "").lower() not in self.STOPPED_STATES:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" must be stopped before its serial ports '
                f"and network adapters can be changed; run vboxlab stop {vm_name}."
            )

        com_settings = self._resolve_com_requests(vm_name, current, request.com_ports)
        if request.access is not None:
            if request.access == "auto":
                self.client.delete_metadata(vm_name, ACCESS_KEY)
            else:
                self.client.set_metadata(vm_name, {ACCESS_KEY: request.access})
        if request.ssh_user is not None:
            self.client.set_metadata(vm_name, {SSH_USER_KEY: request.ssh_user})
        access = self.get_vm_access(vm_name)

        warnings = []
        for number, setting in sorted(com_settings.items()):
            before = current.com_ports[number - 1]
            if before.mode == "tcpserver" and setting.mode != "tcpserver":
                note = f"COM{number} no longer serves TCP port {before.port}"
                if number == 1 and access.mode == "com":
                    note += "; vboxlab connect uses COM1 and cannot reach this VM"
                warnings.append(note + ".")

        updated = current
        if request.hardware_changes:
            self.client.apply_vm_hardware(vm_name, com_settings, request.nics)
            updated = self.client.get_vm_hardware(vm_name)
            self._update_proto_system_sku(vm_name, updated)
        access = self.get_vm_access(vm_name)
        if access.mode == "ssh" and not any(nic.attachment == "nat" for nic in updated.nics):
            warnings.append(
                "The VM is reached through ssh but has no NAT adapter; set one, for "
                "example nic1=nat, and run vboxlab plug."
            )
        return VMSettingsResult(vm_name, vm_info.vm_state, updated, tuple(warnings), access)

    def ssh_target(self, vm_name: str, user: str | None = None) -> SshTarget:
        """Return where and as whom to reach a running VM through ssh.

        :param vm_name: VM name.
        :param user: Guest user; the ssh user of the VM by default.
        :returns: Connection target.
        :raises VBoxLabError: Without a forwarded ssh port or a running VM.
        """
        info = self._require_vm(vm_name)
        access = self._access_of(info)
        if access.ssh_port is None:
            hint = (
                f"run vboxlab plug {vm_name}"
                if access.mode == "ssh"
                else f"it is reached through COM1; run vboxlab set {vm_name} access=ssh "
                f"and vboxlab plug {vm_name}"
            )
            raise VBoxLabError(f'Virtual machine "{vm_name}" has no forwarded ssh port; {hint}.')
        if (info.vm_state or "").lower() != "running":
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is not running; ssh needs a running guest.'
            )
        return SshTarget(
            vm_name=vm_name,
            vm_uuid=info.uuid,
            port=access.ssh_port,
            user=validate_ssh_user(user) if user else access.ssh_user,
        )

    def prepare_ssh_terminal(
        self,
        vm_name: str,
        *,
        timeout: float | None = None,
        notify: Callable[[str], None] | None = None,
        installer=None,
        poll_interval: float = 2.0,
    ) -> SshTarget:
        """Wait for the guest ssh server and make sure the VBoxLab key works.

        A freshly started guest needs time to boot, so a missing ssh server is
        retried until ``timeout``. A refused key starts the interactive key
        setup of :meth:`setup_ssh_key` in the current terminal.

        :param vm_name: Name of a running VM reached through ssh.
        :param timeout: Seconds to wait for the ssh server.
        :param notify: Callback for progress messages.
        :param installer: Interactive installer, for tests.
        :param poll_interval: Seconds between attempts.
        :returns: Connection target for the ssh user of the VM.
        :raises VBoxLabError: When no ssh server answers in time or the key
            cannot be installed.
        """
        notify = notify or (lambda message: None)
        deadline = monotonic() + (timeout or self.DEFAULT_SSH_WAIT_TIMEOUT)
        announced = False
        while True:
            target = self.ssh_target(vm_name)
            check = ssh_client.check_key_access(target, self.ssh_key_store)
            if check.status == "ok":
                return target
            if check.status == "denied":
                notify(
                    f'The VBoxLab ssh key is not installed for {target.user} in "{vm_name}". '
                    "Installing it now (see vboxlab ssh-key --help)."
                )
                result = self.setup_ssh_key(vm_name, installer=installer)
                if result.changed_user:
                    notify(f"ssh user of {vm_name} set to {result.ssh_user}.")
                return self.ssh_target(vm_name)
            if monotonic() >= deadline:
                raise VBoxLabError(
                    f'No ssh server answered on port {target.port} of "{vm_name}": '
                    f"{check.message or 'connection failed'}."
                )
            if not announced:
                notify(f'Waiting for the ssh server of "{vm_name}" (Ctrl-C stops)...')
                announced = True
            sleep(poll_interval)

    def setup_ssh_key(
        self,
        vm_name: str,
        user: str | None = None,
        *,
        console: bool = False,
        installer=None,
        console_wait: float = 10.0,
    ) -> SshKeyResult:
        """Install the VBoxLab key in the guest so that ssh needs no password.

        By default ssh logs in with the password of ``user`` (the ssh user of
        the VM) in the current terminal, adds the key for that user and, when
        the root password is given in the guest, for root as well. With
        ``console`` the key line is typed on the VM screen instead, for a
        shell that is logged in there. When the ssh user of the VM cannot use
        the key but ``user`` can, the ssh user is switched to ``user``.

        :param vm_name: VM name.
        :param user: Guest user for the password login.
        :param console: Type the command on the VM screen.
        :param installer: Interactive installer, for tests.
        :param console_wait: Seconds to wait for a typed command to work.
        :returns: Users that can log in with the key afterwards.
        :raises VBoxLabError: When no ssh server answers or nothing worked.
        """
        target = self.ssh_target(vm_name, user)
        access = self.get_vm_access(vm_name)
        store = self.ssh_key_store
        store.ensure_key()

        before = ssh_client.check_key_access(target, store)
        if before.status == "unreachable":
            raise VBoxLabError(
                f'No ssh server answered on port {target.port} of "{vm_name}": '
                f"{before.message or 'connection failed'}."
            )
        already = before.status == "ok"
        if not already:
            if console:
                script = ssh_client.install_script(store.public_key_text(), with_root=False)
                self.client.type_on_console(vm_name, " " + script)
                deadline = monotonic() + console_wait
                while ssh_client.check_key_access(target, store).status != "ok" and monotonic() < deadline:
                    sleep(0.5)
            else:
                (installer or ssh_client.install_key_interactively)(target, store)

        candidates = list(dict.fromkeys([target.user, access.ssh_user, DEFAULT_SSH_USER]))
        working = tuple(
            name for name in candidates
            if ssh_client.check_key_access(replace(target, user=name), store).status == "ok"
        )
        if not working:
            raise VBoxLabError(
                f'The VBoxLab key was not installed in "{vm_name}"; the guest refused it '
                f"for {', '.join(candidates)}. Many systems refuse a root password over "
                f"ssh: log in as an ordinary user with --user NAME, or type the key on "
                f"the VM screen with --console."
            )
        ssh_user, changed = access.ssh_user, False
        if ssh_user not in working:
            ssh_user, changed = working[0], True
            self.client.set_metadata(vm_name, {SSH_USER_KEY: ssh_user})
        return SshKeyResult(vm_name, working, ssh_user, changed, already)

    def get_vm_access(self, vm_name: str) -> AccessInfo:
        """Return how VBoxLab reaches the guest of a VM.

        :param vm_name: VM name.
        :returns: Access mode, its source and the connection details.
        """
        return self._access_of(self.client.get_vm_info(vm_name))

    def _access_of(self, info: VMInfo) -> AccessInfo:
        """Return the access description of known VM information."""
        return resolve_access(info)

    def clone_vm(
        self,
        base_vm: str,
        clone_vm: str,
        serial_port: int | None = None,
        networks: tuple[str, ...] = (),
    ) -> VMInfo:
        """Create a linked clone and configure it.

        :param base_vm: Name of the source VM.
        :param clone_vm: Name of the linked clone to create.
        :param serial_port: Optional TCP port assigned to ``UART1``.
        :param networks: Up to three internal network names.
        :returns: Detailed information about the created clone.
        :raises VBoxLabError: If invalid parameters are supplied.
        """
        base_info = self._require_vm(base_vm)
        if self._vm_exists(clone_vm):
            raise VBoxLabError(f'Virtual machine "{clone_vm}" already exists.')
        if serial_port is not None and not 1 <= serial_port <= 65535:
            raise VBoxLabError("Serial TCP port must be between 1 and 65535.")
        if len(networks) > 3:
            raise VBoxLabError("At most three internal network names can be provided.")
        base_access = self._access_of(base_info)
        # Only a base VM that stores ssh access passes a forwarded port on.
        ssh_clone = base_access.configured and base_access.mode == "ssh"
        nat_adapter = None
        if ssh_clone:
            nat_adapter = self._first_nat_adapter(base_vm)
            if nat_adapter is None:
                raise VBoxLabError(
                    f'Base VM "{base_vm}" is reached through ssh but has no NAT adapter; '
                    f"run vboxlab set {base_vm} nic1=nat first."
                )
            if 2 <= nat_adapter <= len(networks) + 1:
                raise VBoxLabError(
                    f'The networks would replace NIC{nat_adapter}, the NAT adapter that '
                    f'forwards ssh to "{base_vm}"; give fewer networks or move the NAT '
                    f"adapter to NIC1 with vboxlab set."
                )
        if serial_port is None:
            serial_port = self._allocate_serial_port()

        snapshot_name = self.client.get_current_snapshot_name(base_vm)
        if snapshot_name is None:
            snapshot_name = self.SNAPSHOT_NAME
            self.client.take_snapshot(base_vm, snapshot_name)

        self.client.clone_linked(
            base_vm=base_vm,
            clone_vm=clone_vm,
            group=base_info.primary_group,
            snapshot_name=snapshot_name,
        )

        # Every clone gets COM1 as a TCP server, also as a spare console of
        # ssh clones; the networks always go to NIC2-NIC4 and NIC1 keeps what
        # the base VM has.
        self.client.configure_serial_port(clone_vm, serial_port)
        self.client.configure_internal_networks(clone_vm, networks)
        # The clone copies the ssh rule of its base VM; its host port belongs
        # to the base, so the copy is removed.
        inherited = self.client.get_ssh_forwarding(clone_vm)
        if inherited is not None:
            self.client.remove_ssh_forwarding(clone_vm, inherited[0])
        if ssh_clone:
            ssh_port = self._allocate_serial_port(
                require_host_availability=True, reserved={serial_port}
            )
            self.client.add_ssh_forwarding(clone_vm, nat_adapter, ssh_port, running=False)

        clone_metadata = {
            "vboxlab/managed": "true",
            "vboxlab/kind": "clone",
            "vboxlab/parent": base_vm,
        }
        # The clone keeps the access, the lab property and the ssh user.
        for key in (ACCESS_KEY, LAB_KEY, SSH_USER_KEY):
            if key in base_info.metadata:
                clone_metadata[key] = base_info.metadata[key]
        self.client.set_metadata(clone_vm, clone_metadata)
        if base_access.lab:
            self.client.configure_dmi_system_information(
                clone_vm,
                system_vendor=clone_vm,
                system_sku=self._build_proto_system_sku(
                    serial_port=serial_port,
                    networks=networks,
                ),
            )
        return self.client.get_vm_info(clone_vm)

    def register_proto_settings_vm(self, vm_name: str) -> tuple[str, ...]:
        """Mark one base VM as a lab VM reached through COM1.

        :param vm_name: Base VM name to mark.
        :returns: The names of all lab VMs.
        :raises VMNotFoundError: If the VM does not exist.
        """
        self._require_vm(vm_name)
        self.client.set_metadata(vm_name, {LAB_KEY: "true", ACCESS_KEY: "com"})
        return tuple(
            sorted(info.name for info in self._collect_vm_infos() if resolve_access(info).lab)
        )

    def _allocate_serial_port(
        self,
        require_host_availability: bool = False,
        reserved: set[int] | frozenset[int] = frozenset(),
    ) -> int:
        """Allocate the next TCP port for an automatic serial TCP server.

        The automatic sequence starts at ``1024`` when no existing VM currently
        exposes a TCP-backed serial port. Ports of all serial ports of all VMs
        count as used.

        :param require_host_availability: Whether the chosen TCP port must also
            be available on the host system.
        :param reserved: Further ports that must not be chosen.
        :returns: The next TCP port to assign.
        :raises VBoxLabError: If the TCP port range is exhausted.
        """
        used_ports = set(reserved)
        for info in self._collect_vm_infos():
            used_ports.update(self._vm_tcp_ports(info))
        next_port = (max(used_ports) + 1) if used_ports else self.DEFAULT_SERIAL_TCP_PORT
        while next_port <= 65535:
            if next_port in used_ports:
                next_port += 1
                continue
            if require_host_availability and not self._is_host_tcp_port_available(next_port):
                next_port += 1
                continue
            return next_port
        raise VBoxLabError("No free serial TCP ports are available.")

    def _resolve_com_requests(
        self,
        vm_name: str,
        current: VMHardware,
        requests: dict[int, ComPortRequest],
    ) -> dict[int, ComPortSetting]:
        """Turn requested serial ports into settings with checked TCP ports.

        ``auto`` keeps the TCP port of a port that already is a TCP server.

        :param vm_name: VM name.
        :param current: Current settings of the VM.
        :param requests: Requested changes by port number.
        :returns: New settings by port number.
        :raises VBoxLabError: When a TCP port is used by another VM, by another
            port of this VM or on the host.
        """
        other_ports: dict[int, str] = {}
        for info in self._collect_vm_infos():
            if info.name != vm_name:
                for port in self._vm_tcp_ports(info):
                    other_ports.setdefault(port, info.name)
        own_ports = {
            setting.port
            for setting in current.com_ports
            if setting.mode == "tcpserver" and setting.port is not None
        }
        taken = {
            setting.port
            for number, setting in enumerate(current.com_ports, start=1)
            if number not in requests and setting.mode == "tcpserver" and setting.port is not None
        }

        resolved: dict[int, ComPortSetting] = {}
        for number, request in sorted(requests.items()):
            if request.mode != "tcpserver" or request.port is None:
                continue
            port = request.port
            if port in other_ports:
                raise VBoxLabError(
                    f'COM{number}: TCP port {port} is already used by virtual machine '
                    f'"{other_ports[port]}".'
                )
            if port in taken:
                raise VBoxLabError(
                    f"COM{number}: TCP port {port} is already used by another COM port of this VM."
                )
            if port not in own_ports and not self._is_host_tcp_port_available(port):
                raise VBoxLabError(f"COM{number}: TCP port {port} is busy on this computer.")
            taken.add(port)
            resolved[number] = com_setting_from_request(request, port)

        for number, request in sorted(requests.items()):
            if number in resolved:
                continue
            port = None
            if request.mode == "tcpserver":
                before = current.com_ports[number - 1]
                if before.mode == "tcpserver" and before.port is not None and before.port not in taken:
                    port = before.port
                else:
                    port = self._allocate_serial_port(
                        require_host_availability=True,
                        reserved=taken | set(other_ports),
                    )
                taken.add(port)
            resolved[number] = com_setting_from_request(request, port)
        return resolved

    def _update_proto_system_sku(self, vm_name: str, hardware: VMHardware) -> None:
        """Rebuild the DMI system SKU of a proto-settings clone.

        The SKU is ``port<COM1 port>.<NIC2>.<NIC3>.<NIC4>`` with the internal
        network names; adapters without one leave an empty place and empty
        places at the end are dropped. When COM1 is no TCP server, the
        highest TCP port of the other COM ports is used, and without any the
        port part of the previous SKU is kept.

        :param vm_name: VM name.
        :param hardware: Settings after the change.
        """
        previous = self.client.get_metadata(vm_name).get(VBoxManageClient.DMI_SYSTEM_SKU_KEY)
        if not previous:
            return
        com1 = hardware.com_ports[0]
        tcp_ports = [
            setting.port
            for setting in hardware.com_ports
            if setting.mode == "tcpserver" and setting.port is not None
        ]
        if com1.mode == "tcpserver" and com1.port is not None:
            port_part = f"port{com1.port}"
        elif tcp_ports:
            port_part = f"port{max(tcp_ports)}"
        else:
            port_part = previous.split(".", 1)[0]
        names = [
            (nic.network or "") if nic.attachment == "intnet" else ""
            for nic in hardware.nics[1:4]
        ]
        while names and not names[-1]:
            names.pop()
        system_sku = ".".join([port_part, *names])
        if system_sku != previous:
            self.client.set_dmi_system_sku(vm_name, system_sku)

    @staticmethod
    def _vm_tcp_ports(info: VMInfo) -> tuple[int, ...]:
        """Return the host TCP ports of a VM: serial TCP servers and ssh forwarding."""
        ports = info.serial_tcp_ports or (
            (info.serial_port,) if info.serial_port is not None else ()
        )
        return ports + ((info.ssh_port,) if info.ssh_port is not None else ())

    def _cleanup_integration_vms(
        self,
        created_vm_names: list[str],
        deleted_vm_names: list[str],
    ) -> list[str]:
        """Perform best-effort cleanup for integration-test VMs.

        :param created_vm_names: VM names created during the integration run.
        :param deleted_vm_names: VM names already deleted successfully.
        :returns: Cleanup error messages, if any.
        """
        pending = [name for name in created_vm_names if name not in deleted_vm_names]
        if not pending:
            return []

        cleanup_errors: list[str] = []
        try:
            self._stop_integration_runtime_vms(
                tuple(pending),
                timeout=self.DEFAULT_INTEGRATION_STOP_TIMEOUT,
            )
        except VBoxLabError as error:
            cleanup_errors.append(str(error))

        try:
            self._delete_with_retries(pending)
            return cleanup_errors
        except VBoxLabError as error:
            cleanup_errors.append(str(error))
            return cleanup_errors

    def erase_vm(self, vm_name: str) -> None:
        """Erase one VM when no dependent clones exist.

        :param vm_name: Name of the VM to remove.
        :raises VMDependencyError: If dependent clones still exist.
        """
        self._require_vm(vm_name)
        dependents = self._find_managed_dependents(vm_name)
        if dependents:
            raise VMDependencyError(vm_name, dependents)
        self.client.delete_vm(vm_name)

    def erase_group(self, group_name: str) -> list[str]:
        """Erase all VMs from a single group.

        :param group_name: Group to erase.
        :returns: Names of removed VMs.
        :raises VBoxLabError: If the group is empty or contains blocked base VMs.
        """
        normalized_group = normalize_group_name(group_name)
        all_infos = self._collect_vm_infos()
        selected = [info.name for info in all_infos if normalized_group in info.groups]
        if not selected:
            raise VBoxLabError(f'Group "{normalized_group}" does not contain any VMs.')

        blocked = self._find_external_dependents(selected, all_infos)
        if blocked:
            raise VBoxLabError(
                f'Cannot erase group "{normalized_group}" because clones outside the '
                f"group depend on it: {', '.join(sorted(blocked))}."
            )

        self._delete_with_retries(selected)
        return sorted(selected)

    def erase_all(self) -> list[str]:
        """Erase all registered VirtualBox VMs.

        :returns: Names of removed VMs.
        """
        vm_names = [vm.name for vm in self.client.list_vms()]
        self._delete_with_retries(vm_names)
        return sorted(vm_names)

    def erase_clones(self, group_name: str | None = None) -> list[str]:
        """Erase all managed clone VMs, optionally limited to one group.

        Clones are recognized through the ``vboxlab/kind`` and ``vboxlab/parent``
        metadata written by :meth:`clone_vm`, so no separate clone registry has
        to be kept in sync with VirtualBox.

        :param group_name: Optional group that limits the deleted clones.
        :returns: Names of removed clone VMs.
        :raises VBoxLabError: If the group is empty or clones outside the
            selection still depend on the selected clones.
        """
        all_infos = self._collect_vm_infos()
        candidates = all_infos
        if group_name is not None:
            normalized_group = normalize_group_name(group_name)
            candidates = [info for info in all_infos if normalized_group in info.groups]
            if not candidates:
                raise VBoxLabError(
                    f'Group "{normalized_group}" does not contain any VMs.'
                )

        selected = [info.name for info in candidates if self._is_clone_info(info)]
        if not selected:
            return []

        blocked = self._find_external_dependents(selected, all_infos)
        if blocked:
            raise VBoxLabError(
                "Cannot erase the selected clones because clones outside the "
                f"selection depend on them: {', '.join(sorted(blocked))}."
            )

        self._delete_with_retries(selected)
        return sorted(selected)

    def is_clone_vm(self, vm_name: str) -> bool:
        """Return whether one VM was created as a managed linked clone.

        :param vm_name: VM name to check.
        :returns: ``True`` when the VM is a managed clone.
        :raises VMNotFoundError: If the VM does not exist.
        """
        return self._is_clone_info(self._require_vm(vm_name))

    def full_clean(self, paths: Sequence[Path]) -> list[str]:
        """Delete VirtualBox data directories from the host file system.

        :param paths: Directories to remove recursively.
        :returns: Paths of directories that were removed.
        :raises VBoxLabError: If one or more directories cannot be removed.
        """
        removed: list[str] = []
        failures: list[str] = []
        for path in paths:
            target = Path(path).expanduser()
            if not target.exists():
                continue
            try:
                shutil.rmtree(target)
            except OSError as error:
                failures.append(f"{target}: {error}")
                continue
            removed.append(str(target))

        if failures:
            raise VBoxLabError(
                "Unable to remove all requested directories. " + "; ".join(failures)
            )
        return removed

    def _collect_vm_infos(self) -> list[VMInfo]:
        """Collect detailed information for all VMs.

        :returns: Detailed VM information objects.
        """
        return [self.client.get_vm_info(vm.name) for vm in self.client.list_vms()]

    def _wait_for_vm_state(
        self,
        vm_name: str,
        acceptable_states: set[str],
        timeout: float,
        action_description: str,
    ) -> VMInfo:
        """Wait until a VM reaches one of the acceptable VirtualBox states.

        :param vm_name: VM name.
        :param acceptable_states: States considered successful.
        :param timeout: Timeout in seconds.
        :param action_description: Human-readable action description.
        :returns: Updated VM information once the state is acceptable.
        :raises VBoxLabError: If the timeout expires.
        """
        deadline = monotonic() + timeout
        last_query_error: CommandExecutionError | None = None
        while monotonic() < deadline:
            try:
                raw_state = self.client.get_vm_state(vm_name)
            except CommandExecutionError as error:
                last_query_error = error
                if acceptable_states.issubset(self.STOPPED_STATES) and not self._is_vm_listed_as_running(
                    vm_name
                ):
                    return self._build_state_only_vm_info(vm_name, "poweroff")
                sleep(0.5)
                continue

            if raw_state in acceptable_states:
                try:
                    return self.client.get_vm_info(vm_name)
                except CommandExecutionError as error:
                    last_query_error = error
                    if raw_state in self.STOPPED_STATES:
                        return self._build_state_only_vm_info(vm_name, raw_state)
                    sleep(0.5)
                    continue
            sleep(0.5)
        states = ", ".join(sorted(acceptable_states))
        details = ""
        if last_query_error is not None:
            details = f" Last VBoxManage error: {last_query_error}"
        raise VBoxLabError(
            f'Timed out while waiting for "{vm_name}" to {action_description} '
            f"(expected states: {states}).{details}"
        )

    def _run_integration_runtime_checks(
        self,
        connected_vm_name: str,
        active_vm_name: str,
    ) -> list[VMMonitorRecord]:
        """Run runtime checks for the integration scenario.

        :param connected_vm_name: VM attached through a serial terminal probe.
        :param active_vm_name: VM started without an attached session.
        :returns: Monitor records captured during the runtime check.
        :raises VBoxLabError: If the monitor output does not match expectations.
        """
        with self._integration_terminal_attachment(connected_vm_name):
            self.prepare_vm_connection(active_vm_name)
            monitor_records = self.list_monitored_vms()

        monitor_states = {
            record.name: record.display_state for record in monitor_records
        }
        expected_states = {
            connected_vm_name: "Working",
            active_vm_name: "Active",
        }
        for vm_name, expected_state in expected_states.items():
            observed_state = monitor_states.get(vm_name)
            if observed_state != expected_state:
                raise VBoxLabError(
                    f'Integration monitor expected "{vm_name}" to be {expected_state}, '
                    f"but observed {observed_state or 'no entry'}."
                )
        return monitor_records

    @contextmanager
    def _integration_terminal_attachment(self, vm_name: str) -> Iterator[VMInfo]:
        """Attach a non-interactive serial probe for integration testing.

        :param vm_name: VM attached through the terminal probe.
        :yields: VM information for the attached VM.
        """
        vm_info = self.prepare_vm_connection(vm_name)
        if vm_info.serial_port is None:
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" does not have a TCP-backed serial port.'
            )

        with self.serial_probe_factory("localhost", vm_info.serial_port):
            with self.session_registry.register(vm_name, vm_info.serial_port):
                yield vm_info

    def _stop_integration_runtime_vms(
        self,
        vm_names: tuple[str, ...],
        timeout: float | None = None,
    ) -> None:
        """Best-effort stop for VMs started during the integration scenario.

        :param vm_names: VM names to stop if they are currently running.
        :param timeout: Optional per-VM graceful stop timeout.
        """
        stoppable_names: list[str] = []
        for vm_name in vm_names:
            if not self._vm_exists(vm_name):
                continue
            try:
                display_state = self.get_monitor_state_label(vm_name)
            except VBoxLabError:
                continue
            if display_state in {"Working", "Active"}:
                stoppable_names.append(vm_name)

        self._stop_runtime_vm_names(stoppable_names, timeout=timeout)

    def _require_vm(self, vm_name: str) -> VMInfo:
        """Return a VM or raise an error if it does not exist.

        :param vm_name: VM name to resolve.
        :returns: Detailed VM information.
        :raises VMNotFoundError: If the VM does not exist.
        """
        if not self._vm_exists(vm_name):
            raise VMNotFoundError(vm_name)
        return self.client.get_vm_info(vm_name)

    def _vm_exists(self, vm_name: str) -> bool:
        """Check whether a VM exists.

        :param vm_name: VM name to look up.
        :returns: ``True`` when the VM exists.
        """
        return any(vm.name == vm_name for vm in self.client.list_vms())

    def _is_clone_info(self, info: VMInfo) -> bool:
        """Return whether VM information describes a managed linked clone.

        :param info: VM information to inspect.
        :returns: ``True`` when the VM carries clone metadata.
        """
        if info.metadata.get("vboxlab/kind") == "clone":
            return True
        return info.parent_name is not None

    def _display_state(self, raw_state: str, has_live_session: bool) -> str:
        """Map a raw VirtualBox state to a user-facing monitor label.

        :param raw_state: Raw VirtualBox state.
        :param has_live_session: Whether VBoxLab currently has a live session.
        :returns: User-facing state label.
        """
        normalized = raw_state.lower()
        if normalized == "running":
            return "Working" if has_live_session else "Active"
        if normalized in self.STOPPING_STATES:
            return "Stopping"
        if normalized in self.STOPPED_STATES:
            return "Stopped"
        if normalized in self.PAUSED_STATES:
            return "Paused"
        if normalized in self.ERROR_STATES:
            return "Error"
        return "Changing"

    def _is_vm_listed_as_running(self, vm_name: str) -> bool:
        """Return whether VirtualBox still reports a VM as running.

        :param vm_name: VM name to check.
        :returns: ``True`` when the VM still appears in ``runningvms``.
        """
        try:
            return any(vm.name == vm_name for vm in self.client.list_running_vms())
        except CommandExecutionError:
            return True

    def _build_state_only_vm_info(self, vm_name: str, vm_state: str) -> VMInfo:
        """Build a minimal VM info object when state polling is all that is available.

        :param vm_name: VM name.
        :param vm_state: Raw VirtualBox state string.
        :returns: Minimal VM information for the requested state.
        """
        reference = next((vm for vm in self.client.list_vms() if vm.name == vm_name), None)
        return VMInfo(
            name=vm_name,
            uuid=reference.uuid if reference is not None else "",
            groups=(),
            vm_state=vm_state,
        )

    def _find_managed_dependents(self, vm_name: str) -> list[str]:
        """Find all managed clone descendants of a VM.

        :param vm_name: Ancestor VM name.
        :returns: Descendant VM names.
        """
        all_infos = self._collect_vm_infos()
        descendants: set[str] = set()
        queue = [vm_name]

        while queue:
            parent_name = queue.pop(0)
            for info in all_infos:
                if info.parent_name == parent_name and info.name not in descendants:
                    descendants.add(info.name)
                    queue.append(info.name)

        return sorted(descendants)

    def _find_external_dependents(
        self, selected_names: list[str], all_infos: list[VMInfo]
    ) -> list[str]:
        """Find selected VMs that still have descendants outside the selection.

        :param selected_names: Names selected for deletion.
        :param all_infos: All VM information objects.
        :returns: Names of selected VMs that are still referenced outside the selection.
        """
        selected_set = set(selected_names)
        blocked: set[str] = set()
        for info in all_infos:
            if info.parent_name in selected_set and info.name not in selected_set:
                blocked.add(info.parent_name)
        return sorted(blocked)

    def _delete_with_retries(self, vm_names: list[str]) -> None:
        """Delete several VMs in parallel waves that respect dependencies.

        :param vm_names: Names of the VMs to delete.
        :raises VBoxLabError: If the requested set cannot be fully removed.
        """
        remaining = list(dict.fromkeys(vm_names))
        if not remaining:
            return

        infos = {info.name: info for info in self._collect_vm_infos()}
        while remaining:
            ready = self._deletion_wave(remaining, infos)
            if not ready:
                unresolved = ", ".join(sorted(remaining))
                raise VBoxLabError(
                    "Unable to erase all requested VMs. "
                    f"No deletable dependency wave found for: {unresolved}."
                )

            failures = self._run_parallel_vm_actions(ready, self.client.delete_vm)
            deleted_names = [vm_name for vm_name in ready if vm_name not in failures]
            if not deleted_names:
                details = "; ".join(
                    f'{name}: {message}' for name, message in sorted(failures.items())
                )
                raise VBoxLabError(f"Unable to erase all requested VMs. {details}")

            remaining = [vm_name for vm_name in remaining if vm_name not in deleted_names]

    def _stop_runtime_vm_names(
        self,
        vm_names: list[str],
        timeout: float | None = None,
    ) -> list[str]:
        """Stop several running VMs in parallel.

        :param vm_names: Names of VMs to stop.
        :param timeout: Optional per-VM timeout in seconds.
        :returns: VM names that were successfully stopped.
        :raises VBoxLabError: If one or more VMs cannot be stopped.
        """
        if not vm_names:
            return []

        effective_timeout = timeout or self.DEFAULT_STOP_TIMEOUT
        request_failures = self._run_parallel_vm_actions(vm_names, self.client.stop_vm_acpi)
        requested_names = [vm_name for vm_name in vm_names if vm_name not in request_failures]
        wait_failures = self._run_parallel_vm_actions(
            requested_names,
            lambda vm_name: self._wait_for_vm_state(
                vm_name=vm_name,
                acceptable_states=self.STOPPED_STATES,
                timeout=effective_timeout,
                action_description="stop gracefully",
            ),
        )

        if request_failures or wait_failures:
            details: list[str] = []
            if request_failures:
                details.append(
                    "ACPI request failures: "
                    + "; ".join(
                        f'{name}: {message}'
                        for name, message in sorted(request_failures.items())
                    )
                )
            if wait_failures:
                details.append(
                    "shutdown wait failures: "
                    + "; ".join(
                        f'{name}: {message}'
                        for name, message in sorted(wait_failures.items())
                    )
                )
            raise VBoxLabError("Unable to stop all requested VMs. " + " ".join(details))

        return requested_names

    def _deletion_wave(
        self,
        remaining: list[str],
        infos: dict[str, VMInfo],
    ) -> list[str]:
        """Return the next set of VMs that can be deleted together.

        A VM is ready when none of the remaining VMs still depends on it.

        :param remaining: Remaining VM names to delete.
        :param infos: Mapping of VM names to detailed information.
        :returns: VM names that can be deleted in parallel.
        """
        remaining_set = set(remaining)
        blocked_parents = {
            info.parent_name
            for vm_name in remaining
            if (info := infos.get(vm_name)) is not None
            and info.parent_name in remaining_set
        }
        return [vm_name for vm_name in remaining if vm_name not in blocked_parents]

    def _run_parallel_vm_actions(
        self,
        vm_names: list[str],
        action: Callable[[str], object],
    ) -> dict[str, str]:
        """Execute one action for multiple VMs in parallel.

        :param vm_names: VM names passed to the action.
        :param action: Action executed once per VM name.
        :returns: Mapping of failed VM names to error messages.
        """
        if not vm_names:
            return {}

        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(32, len(vm_names))) as executor:
            future_to_name = {
                executor.submit(action, vm_name): vm_name for vm_name in vm_names
            }
            for future in as_completed(future_to_name):
                vm_name = future_to_name[future]
                try:
                    future.result()
                except (CommandExecutionError, VBoxLabError) as error:
                    failures[vm_name] = str(error)
        return failures

    def _is_host_tcp_port_available(self, port: int) -> bool:
        """Return whether a host TCP port is currently available.

        :param port: TCP port number to probe.
        :returns: ``True`` when the port can be bound locally.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("", port))
            except OSError:
                return False
        return True

    def _build_proto_system_sku(
        self,
        serial_port: int,
        networks: tuple[str, ...],
    ) -> str:
        """Build the DMI system SKU used for proto-settings clones.

        :param serial_port: TCP port assigned to the clone.
        :param networks: Internal network names assigned to the clone.
        :returns: DMI system SKU value.
        """
        suffix = ".".join(networks)
        return f"port{serial_port}.{suffix}" if suffix else f"port{serial_port}"
