"""Application service layer for PySnap."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pysnap.core.models import ImportCandidate, IntegrationTestResult, VMGroup, VMInfo
from pysnap.errors import (
    CommandExecutionError,
    PySnapError,
    VMDependencyError,
    VMNotFoundError,
)
from pysnap.vbox.client import VBoxManageClient


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

    def __init__(self, client: VBoxManageClient | None = None) -> None:
        """Initialize the service.

        :param client: Optional VirtualBox client implementation.
        """
        self.client = client or VBoxManageClient()

    def import_image(self, image_path: str) -> list[VMInfo]:
        """Import an OVA or OVF image into VirtualBox.

        :param image_path: Path to the OVA or OVF appliance.
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
        self.client.import_appliance(str(image), normalized_imports)
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
        and removes all created VMs one by one.

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

            for vm_name in clone_names:
                self.erase_vm(vm_name)
                deleted_vm_names.append(vm_name)
            self.erase_vm(imported_vm_name)
            deleted_vm_names.append(imported_vm_name)

            return IntegrationTestResult(
                machines=tuple(machines),
                deleted_vm_names=tuple(deleted_vm_names),
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
        return self.client.get_vm_info(clone_vm)

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

        try:
            self._delete_with_retries(pending)
            return []
        except PySnapError as error:
            return [str(error)]

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
        """Delete several VMs, retrying when parent dependencies block removal.

        :param vm_names: Names of the VMs to delete.
        :raises PySnapError: If the requested set cannot be fully removed.
        """
        remaining = list(dict.fromkeys(vm_names))
        if not remaining:
            return

        infos = {info.name: info for info in self._collect_vm_infos()}
        failures: dict[str, str] = {}

        while remaining:
            progress = False
            remaining.sort(key=lambda name: self._deletion_depth(name, infos), reverse=True)
            next_remaining: list[str] = []

            for vm_name in remaining:
                try:
                    self.client.delete_vm(vm_name)
                    progress = True
                    failures.pop(vm_name, None)
                except CommandExecutionError as error:
                    next_remaining.append(vm_name)
                    failures[vm_name] = str(error)

            if not progress:
                details = "; ".join(
                    f'{name}: {message}' for name, message in sorted(failures.items())
                )
                raise PySnapError(f"Unable to erase all requested VMs. {details}")

            remaining = next_remaining

    def _deletion_depth(self, vm_name: str, infos: dict[str, VMInfo]) -> int:
        """Calculate the parent-chain depth used for deletion ordering.

        :param vm_name: VM name to evaluate.
        :param infos: Mapping of VM names to information objects.
        :returns: Parent-chain depth.
        """
        info = infos.get(vm_name)
        if info is None or not info.parent_name:
            return 0
        return 1 + self._deletion_depth(info.parent_name, infos)
