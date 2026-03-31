"""Application service layer for PySnap."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Callable, ContextManager, Iterator
from uuid import uuid4

from pysnap.config.protosettings import ProtoSettingsStore
from pysnap.core.models import (
    ImportCandidate,
    IntegrationTestResult,
    VMGroup,
    VMInfo,
    VMMonitorRecord,
)
from pysnap.errors import (
    CommandExecutionError,
    PySnapError,
    VMDependencyError,
    VMNotFoundError,
)
from pysnap.runtime.sessions import SessionRegistry
from pysnap.terminal.transport import serial_connection_probe
from pysnap.vbox.client import VBoxManageClient

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


class PySnapService:
    """Provide the main PySnap business operations."""

    SNAPSHOT_NAME = "pysnap-base"
    DEFAULT_SERIAL_TCP_PORT = 1024
    DEFAULT_START_TIMEOUT = 30.0
    DEFAULT_STOP_TIMEOUT = 60.0
    DEFAULT_INTEGRATION_STOP_TIMEOUT = 180.0
    STOPPED_STATES = {"poweroff", "saved", "aborted"}
    PAUSED_STATES = {"paused"}
    ERROR_STATES = {"gurumeditation", "stuck"}

    def __init__(
        self,
        client: VBoxManageClient | None = None,
        session_registry: SessionRegistry | None = None,
        serial_probe_factory: SerialProbeFactory | None = None,
        proto_settings_store: ProtoSettingsStore | None = None,
    ) -> None:
        """Initialize the service.

        :param client: Optional VirtualBox client implementation.
        """
        self.client = client or VBoxManageClient()
        self.session_registry = session_registry or SessionRegistry()
        self.serial_probe_factory = serial_probe_factory or serial_connection_probe
        self.proto_settings_store = proto_settings_store or ProtoSettingsStore()

    def import_image(
        self,
        image_path: str,
        progress_callback: ImportProgressCallback | None = None,
    ) -> list[VMInfo]:
        """Import an OVA or OVF image into VirtualBox.

        :param image_path: Path to the OVA or OVF appliance.
        :param progress_callback: Optional import progress callback.
        :returns: Imported VM information objects.
        :raises PySnapError: If the file is invalid or import does not create VMs.
        """
        image = Path(image_path).expanduser().resolve()
        if image.suffix.lower() not in {".ova", ".ovf"}:
            raise PySnapError("Only .ova and .ovf images can be imported.")
        if not image.exists():
            raise PySnapError(f'Image "{image}" does not exist.')

        planned_imports = self.client.dry_run_import(str(image))
        if not planned_imports:
            raise PySnapError("The appliance dry run did not expose any virtual systems.")

        normalized_imports = [
            ImportCandidate(
                vsys_index=item.vsys_index,
                vm_name=item.vm_name,
                group=normalize_group_name(item.group),
                requires_eula_accept=item.requires_eula_accept,
            )
            for item in planned_imports
        ]

        before_names = {vm.name for vm in self.client.list_vms()}
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
            raise PySnapError("The appliance import finished, but no imported VMs were detected.")

        imported_infos: list[VMInfo] = []
        for vm_name in imported_names:
            self.client.set_metadata(
                vm_name,
                {
                    "pysnap/managed": "true",
                    "pysnap/kind": "base",
                    "pysnap/source": str(image),
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
        :raises PySnapError: If the appliance is invalid or the test fails.
        """
        image = Path(image_path).expanduser().resolve()
        if image.suffix.lower() not in {".ova", ".ovf"}:
            raise PySnapError("Only .ova and .ovf images can be used for integration tests.")
        if not image.exists():
            raise PySnapError(f'Image "{image}" does not exist.')

        planned_imports = self.client.dry_run_import(str(image))
        if len(planned_imports) != 1:
            raise PySnapError(
                "Integration test expects an appliance containing exactly one VM."
            )

        token = name_token or uuid4().hex[:8]
        imported_vm_name = f"pysnap-it-{token}-base"
        clone_names = (
            f"pysnap-it-{token}-clone-a",
            f"pysnap-it-{token}-clone-b",
            f"pysnap-it-{token}-clone-c",
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
                    "pysnap/managed": "true",
                    "pysnap/kind": "base",
                    "pysnap/source": str(image),
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
                raise PySnapError(
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
        """Ensure that a VM is ready for an interactive serial connection.

        :param vm_name: VM name.
        :param timeout: Optional startup timeout in seconds.
        :returns: Updated VM information.
        :raises PySnapError: If the VM cannot be connected.
        """
        vm_info = self._require_vm(vm_name)
        if vm_info.serial_port is None:
            raise PySnapError(
                f'Virtual machine "{vm_name}" does not have a TCP-backed serial port.'
            )
        if self.session_registry.get_live_session(vm_name) is not None:
            raise PySnapError(
                f'Virtual machine "{vm_name}" already has an active terminal session.'
            )

        raw_state = (vm_info.vm_state or "").lower()
        if raw_state == "running":
            return vm_info
        if raw_state in self.PAUSED_STATES:
            raise PySnapError(f'Virtual machine "{vm_name}" is paused.')
        if raw_state in self.ERROR_STATES:
            raise PySnapError(
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
        :raises PySnapError: If the VM is not in a stoppable state.
        """
        vm_info = self._require_vm(vm_name)
        display_state = self.get_monitor_state_label(vm_name)

        if display_state == "Stopped":
            raise PySnapError(f'Virtual machine "{vm_name}" is not running.')
        if display_state == "Changing":
            raise PySnapError(
                f'Virtual machine "{vm_name}" is changing state; try again later.'
            )
        if display_state not in {"Working", "Active"}:
            raise PySnapError(
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
        stoppable_names = [
            record.name
            for record in self.list_monitored_vms()
            if record.display_state in {"Working", "Active"}
        ]
        return self._stop_runtime_vm_names(stoppable_names, timeout=timeout)

    def show_vm(self, vm_name: str) -> VMInfo:
        """Return details for a single VM.

        :param vm_name: Name of the VM to inspect.
        :returns: Detailed VM information.
        :raises VMNotFoundError: If the VM is absent.
        """
        self._require_vm(vm_name)
        return self.client.get_vm_info(vm_name)

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
        :raises PySnapError: If invalid parameters are supplied.
        """
        base_info = self._require_vm(base_vm)
        if self._vm_exists(clone_vm):
            raise PySnapError(f'Virtual machine "{clone_vm}" already exists.')
        if serial_port is not None and not 1 <= serial_port <= 65535:
            raise PySnapError("Serial TCP port must be between 1 and 65535.")
        if len(networks) > 3:
            raise PySnapError("At most three internal network names can be provided.")
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

        self.client.configure_serial_port(clone_vm, serial_port)
        if networks:
            self.client.configure_internal_networks(clone_vm, networks)

        self.client.set_metadata(
            clone_vm,
            {
                "pysnap/managed": "true",
                "pysnap/kind": "clone",
                "pysnap/parent": base_vm,
            },
        )
        if self.proto_settings_store.contains(base_vm):
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
        """Persist one base VM name in the proto-settings store.

        :param vm_name: Base VM name to register.
        :returns: Updated configured VM names.
        :raises VMNotFoundError: If the VM does not exist.
        """
        self._require_vm(vm_name)
        return self.proto_settings_store.add_vm_name(vm_name)

    def _allocate_serial_port(self) -> int:
        """Allocate the next TCP port for automatic ``UART1`` configuration.

        The automatic sequence starts at ``1024`` when no existing VM currently
        exposes a TCP-backed serial port.

        :returns: The next TCP port to assign.
        :raises PySnapError: If the TCP port range is exhausted.
        """
        used_ports = [
            info.serial_port
            for info in self._collect_vm_infos()
            if info.serial_port is not None
        ]
        next_port = (max(used_ports) + 1) if used_ports else self.DEFAULT_SERIAL_TCP_PORT
        if next_port > 65535:
            raise PySnapError("No free serial TCP ports are available.")
        return next_port

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
        except PySnapError as error:
            cleanup_errors.append(str(error))

        try:
            self._delete_with_retries(pending)
            return cleanup_errors
        except PySnapError as error:
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
        self.proto_settings_store.remove_vm_names([vm_name])

    def erase_group(self, group_name: str) -> list[str]:
        """Erase all VMs from a single group.

        :param group_name: Group to erase.
        :returns: Names of removed VMs.
        :raises PySnapError: If the group is empty or contains blocked base VMs.
        """
        normalized_group = normalize_group_name(group_name)
        all_infos = self._collect_vm_infos()
        selected = [info.name for info in all_infos if normalized_group in info.groups]
        if not selected:
            raise PySnapError(f'Group "{normalized_group}" does not contain any VMs.')

        blocked = self._find_external_dependents(selected, all_infos)
        if blocked:
            raise PySnapError(
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
        :raises PySnapError: If the timeout expires.
        """
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            vm_info = self.client.get_vm_info(vm_name)
            raw_state = (vm_info.vm_state or "").lower()
            if raw_state in acceptable_states:
                return vm_info
            sleep(0.5)
        states = ", ".join(sorted(acceptable_states))
        raise PySnapError(
            f'Timed out while waiting for "{vm_name}" to {action_description} '
            f"(expected states: {states})."
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
        :raises PySnapError: If the monitor output does not match expectations.
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
                raise PySnapError(
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
            raise PySnapError(
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
            except PySnapError:
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

    def _display_state(self, raw_state: str, has_live_session: bool) -> str:
        """Map a raw VirtualBox state to a user-facing monitor label.

        :param raw_state: Raw VirtualBox state.
        :param has_live_session: Whether PySnap currently has a live session.
        :returns: User-facing state label.
        """
        normalized = raw_state.lower()
        if normalized == "running":
            return "Working" if has_live_session else "Active"
        if normalized in self.STOPPED_STATES:
            return "Stopped"
        if normalized in self.PAUSED_STATES:
            return "Paused"
        if normalized in self.ERROR_STATES:
            return "Error"
        return "Changing"

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
        :raises PySnapError: If the requested set cannot be fully removed.
        """
        remaining = list(dict.fromkeys(vm_names))
        if not remaining:
            return

        infos = {info.name: info for info in self._collect_vm_infos()}
        while remaining:
            ready = self._deletion_wave(remaining, infos)
            if not ready:
                unresolved = ", ".join(sorted(remaining))
                raise PySnapError(
                    "Unable to erase all requested VMs. "
                    f"No deletable dependency wave found for: {unresolved}."
                )

            failures = self._run_parallel_vm_actions(ready, self.client.delete_vm)
            deleted_names = [vm_name for vm_name in ready if vm_name not in failures]
            if not deleted_names:
                details = "; ".join(
                    f'{name}: {message}' for name, message in sorted(failures.items())
                )
                raise PySnapError(f"Unable to erase all requested VMs. {details}")

            self.proto_settings_store.remove_vm_names(deleted_names)
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
        :raises PySnapError: If one or more VMs cannot be stopped.
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
            raise PySnapError("Unable to stop all requested VMs. " + " ".join(details))

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
                except (CommandExecutionError, PySnapError) as error:
                    failures[vm_name] = str(error)
        return failures

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
