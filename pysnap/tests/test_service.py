"""Unit tests for the PySnap service layer."""

from __future__ import annotations

import tempfile
import unittest

from pysnap.core.models import ImportCandidate, VMInfo, VMReference
from pysnap.core.service import PySnapService
from pysnap.errors import VMDependencyError
from pysnap.runtime.sessions import SessionRecord


class FakeSessionRegistry:
    """Provide an in-memory registry for runtime session tests."""

    def __init__(self, live_sessions: dict[str, SessionRecord] | None = None) -> None:
        """Initialize the fake registry."""
        self.live_sessions = live_sessions or {}

    def list_live_sessions(self) -> dict[str, SessionRecord]:
        """Return configured live sessions."""
        return dict(self.live_sessions)

    def get_live_session(self, vm_name: str) -> SessionRecord | None:
        """Return a configured live session for one VM."""
        return self.live_sessions.get(vm_name)


class FakeClient:
    """Provide a controllable fake VirtualBox client for unit tests."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.references: dict[str, VMReference] = {}
        self.infos: dict[str, VMInfo] = {}
        self.import_candidates: list[ImportCandidate] = []
        self.snapshot_names: dict[str, str | None] = {}
        self.deleted: list[str] = []
        self.calls: list[tuple] = []
        self.state_sequences: dict[str, list[str]] = {}

    def list_vms(self) -> list[VMReference]:
        """Return known VM references."""
        return list(self.references.values())

    def get_vm_info(self, vm_name: str) -> VMInfo:
        """Return detailed VM information."""
        current = self.infos[vm_name]
        if vm_name in self.state_sequences and self.state_sequences[vm_name]:
            next_state = self.state_sequences[vm_name].pop(0)
            current = VMInfo(
                name=current.name,
                uuid=current.uuid,
                groups=current.groups,
                serial_port=current.serial_port,
                vm_state=next_state,
                parent_name=current.parent_name,
                managed=current.managed,
                metadata=current.metadata,
            )
            self.infos[vm_name] = current
        return current

    def dry_run_import(self, image_path: str) -> list[ImportCandidate]:
        """Return configured dry-run candidates."""
        self.calls.append(("dry_run_import", image_path))
        return list(self.import_candidates)

    def import_appliance(self, image_path: str, candidates: list[ImportCandidate]) -> None:
        """Simulate appliance import."""
        self.calls.append(("import_appliance", image_path, tuple(candidates)))
        for candidate in candidates:
            self.references[candidate.vm_name] = VMReference(
                name=candidate.vm_name,
                uuid=f"uuid-{candidate.vm_name}",
            )
            self.infos[candidate.vm_name] = VMInfo(
                name=candidate.vm_name,
                uuid=f"uuid-{candidate.vm_name}",
                groups=(candidate.group,),
                vm_state="poweroff",
            )

    def set_metadata(self, vm_name: str, metadata: dict[str, str]) -> None:
        """Store metadata on the fake VM object."""
        self.calls.append(("set_metadata", vm_name, dict(metadata)))
        current = self.infos[vm_name]
        merged_metadata = dict(current.metadata)
        merged_metadata.update(metadata)
        self.infos[vm_name] = VMInfo(
            name=current.name,
            uuid=current.uuid,
            groups=current.groups,
            serial_port=current.serial_port,
            vm_state=current.vm_state,
            parent_name=merged_metadata.get("pysnap/parent"),
            managed=merged_metadata.get("pysnap/managed") == "true",
            metadata=merged_metadata,
        )

    def get_current_snapshot_name(self, vm_name: str) -> str | None:
        """Return a configured snapshot name."""
        return self.snapshot_names.get(vm_name)

    def take_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        """Record snapshot creation."""
        self.calls.append(("take_snapshot", vm_name, snapshot_name))
        self.snapshot_names[vm_name] = snapshot_name

    def clone_linked(
        self, base_vm: str, clone_vm: str, group: str, snapshot_name: str
    ) -> None:
        """Simulate linked clone creation."""
        self.calls.append(("clone_linked", base_vm, clone_vm, group, snapshot_name))
        self.references[clone_vm] = VMReference(name=clone_vm, uuid=f"uuid-{clone_vm}")
        self.infos[clone_vm] = VMInfo(
            name=clone_vm,
            uuid=f"uuid-{clone_vm}",
            groups=(group,),
            vm_state="poweroff",
        )

    def configure_serial_port(self, vm_name: str, serial_port: int) -> None:
        """Record serial port configuration."""
        self.calls.append(("configure_serial_port", vm_name, serial_port))
        current = self.infos[vm_name]
        self.infos[vm_name] = VMInfo(
            name=current.name,
            uuid=current.uuid,
            groups=current.groups,
            serial_port=serial_port,
            vm_state=current.vm_state,
            parent_name=current.parent_name,
            managed=current.managed,
            metadata=current.metadata,
        )

    def configure_internal_networks(self, vm_name: str, networks: tuple[str, ...]) -> None:
        """Record network configuration."""
        self.calls.append(("configure_internal_networks", vm_name, networks))

    def delete_vm(self, vm_name: str) -> None:
        """Delete a fake VM."""
        self.calls.append(("delete_vm", vm_name))
        self.deleted.append(vm_name)
        self.references.pop(vm_name, None)
        self.infos.pop(vm_name, None)

    def start_vm_headless(self, vm_name: str) -> None:
        """Record a headless start request."""
        self.calls.append(("start_vm_headless", vm_name))
        current = self.infos[vm_name]
        self.infos[vm_name] = VMInfo(
            name=current.name,
            uuid=current.uuid,
            groups=current.groups,
            serial_port=current.serial_port,
            vm_state="starting",
            parent_name=current.parent_name,
            managed=current.managed,
            metadata=current.metadata,
        )
        self.state_sequences.setdefault(vm_name, ["running"])

    def stop_vm_acpi(self, vm_name: str) -> None:
        """Record an ACPI stop request."""
        self.calls.append(("stop_vm_acpi", vm_name))
        current = self.infos[vm_name]
        self.infos[vm_name] = VMInfo(
            name=current.name,
            uuid=current.uuid,
            groups=current.groups,
            serial_port=current.serial_port,
            vm_state="stopping",
            parent_name=current.parent_name,
            managed=current.managed,
            metadata=current.metadata,
        )
        self.state_sequences.setdefault(vm_name, ["poweroff"])


class ServiceTests(unittest.TestCase):
    """Verify PySnap service behavior."""

    def test_clone_vm_uses_snapshot_group_and_metadata(self) -> None:
        """Create a linked clone with inherited group and extra settings."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            metadata={"pysnap/managed": "true"},
            managed=True,
        )
        client.snapshot_names["base-vm"] = "existing-snapshot"

        service = PySnapService(client=client)
        clone_info = service.clone_vm(
            base_vm="base-vm",
            clone_vm="clone-vm",
            serial_port=2345,
            networks=("intnet-a", "intnet-b"),
        )

        self.assertEqual(clone_info.name, "clone-vm")
        self.assertEqual(clone_info.groups, ("/Lab",))
        self.assertEqual(clone_info.serial_port, 2345)
        self.assertEqual(clone_info.parent_name, "base-vm")
        self.assertIn(
            ("clone_linked", "base-vm", "clone-vm", "/Lab", "existing-snapshot"),
            client.calls,
        )
        self.assertIn(("configure_serial_port", "clone-vm", 2345), client.calls)

    def test_clone_vm_auto_assigns_next_serial_port(self) -> None:
        """Assign the next TCP port when ``-p`` is omitted."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.references["existing-a"] = VMReference("existing-a", "uuid-existing-a")
        client.references["existing-b"] = VMReference("existing-b", "uuid-existing-b")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["existing-a"] = VMInfo(
            name="existing-a",
            uuid="uuid-existing-a",
            groups=("/Lab",),
            serial_port=2345,
        )
        client.infos["existing-b"] = VMInfo(
            name="existing-b",
            uuid="uuid-existing-b",
            groups=("/Lab",),
            serial_port=2346,
        )
        client.snapshot_names["base-vm"] = "existing-snapshot"

        service = PySnapService(client=client)
        clone_info = service.clone_vm(base_vm="base-vm", clone_vm="clone-vm")

        self.assertEqual(clone_info.serial_port, 2347)
        self.assertIn(("configure_serial_port", "clone-vm", 2347), client.calls)

    def test_clone_vm_auto_assigns_default_port_when_none_exist(self) -> None:
        """Start automatic serial TCP allocation at port ``1024``."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.snapshot_names["base-vm"] = "existing-snapshot"

        service = PySnapService(client=client)
        clone_info = service.clone_vm(base_vm="base-vm", clone_vm="clone-vm")

        self.assertEqual(clone_info.serial_port, 1024)
        self.assertIn(("configure_serial_port", "clone-vm", 1024), client.calls)

    def test_erase_vm_rejects_managed_descendants(self) -> None:
        """Reject deleting a VM that still has managed descendants."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.references["clone-vm"] = VMReference("clone-vm", "uuid-clone")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-vm"] = VMInfo(
            name="clone-vm",
            uuid="uuid-clone",
            groups=("/Lab",),
            parent_name="base-vm",
            managed=True,
        )

        service = PySnapService(client=client)
        with self.assertRaises(VMDependencyError):
            service.erase_vm("base-vm")

    def test_erase_all_deletes_children_before_parents(self) -> None:
        """Delete descendants before their parents during cascade erase."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.references["clone-vm"] = VMReference("clone-vm", "uuid-clone")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-vm"] = VMInfo(
            name="clone-vm",
            uuid="uuid-clone",
            groups=("/Lab",),
            parent_name="base-vm",
            managed=True,
        )

        service = PySnapService(client=client)
        deleted = service.erase_all()

        self.assertEqual(deleted, ["base-vm", "clone-vm"])
        delete_calls = [call for call in client.calls if call[0] == "delete_vm"]
        self.assertEqual(delete_calls[0], ("delete_vm", "clone-vm"))
        self.assertEqual(delete_calls[1], ("delete_vm", "base-vm"))

    def test_run_integration_test_creates_triangle_and_deletes_vms(self) -> None:
        """Run the integration workflow on a fake appliance."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="original-base", group="/Lab")
        ]

        service = PySnapService(client=client)
        with tempfile.NamedTemporaryFile(suffix=".ova") as appliance:
            result = service.run_integration_test(
                appliance.name,
                name_token="case1234",
            )

        machine_names = [vm_info.name for vm_info in result.machines]
        self.assertEqual(
            machine_names,
            [
                "pysnap-it-case1234-base",
                "pysnap-it-case1234-clone-a",
                "pysnap-it-case1234-clone-b",
                "pysnap-it-case1234-clone-c",
            ],
        )
        self.assertIn(
            (
                "configure_internal_networks",
                "pysnap-it-case1234-clone-a",
                ("intnet", "virtnet"),
            ),
            client.calls,
        )
        self.assertIn(
            (
                "configure_internal_networks",
                "pysnap-it-case1234-clone-b",
                ("intnet", "deepnet"),
            ),
            client.calls,
        )
        self.assertIn(
            (
                "configure_internal_networks",
                "pysnap-it-case1234-clone-c",
                ("deepnet", "virtnet"),
            ),
            client.calls,
        )
        self.assertEqual(
            result.deleted_vm_names,
            (
                "pysnap-it-case1234-clone-a",
                "pysnap-it-case1234-clone-b",
                "pysnap-it-case1234-clone-c",
                "pysnap-it-case1234-base",
            ),
        )

    def test_list_monitored_vms_distinguishes_working_active_and_changing(self) -> None:
        """Map raw VirtualBox runtime states to compact monitor states."""
        client = FakeClient()
        client.references["working-vm"] = VMReference("working-vm", "uuid-working")
        client.references["active-vm"] = VMReference("active-vm", "uuid-active")
        client.references["changing-vm"] = VMReference("changing-vm", "uuid-changing")
        client.infos["working-vm"] = VMInfo(
            name="working-vm",
            uuid="uuid-working",
            groups=("/Lab",),
            serial_port=2201,
            vm_state="running",
        )
        client.infos["active-vm"] = VMInfo(
            name="active-vm",
            uuid="uuid-active",
            groups=("/Lab",),
            serial_port=2202,
            vm_state="running",
        )
        client.infos["changing-vm"] = VMInfo(
            name="changing-vm",
            uuid="uuid-changing",
            groups=("/Lab",),
            serial_port=2203,
            vm_state="starting",
        )
        registry = FakeSessionRegistry(
            live_sessions={
                "working-vm": SessionRecord(
                    vm_name="working-vm",
                    serial_port=2201,
                    pid=1,
                    attached_at="2026-03-26T00:00:00+00:00",
                )
            }
        )

        service = PySnapService(client=client, session_registry=registry)
        records = service.list_monitored_vms()

        states = {record.name: record.display_state for record in records}
        self.assertEqual(states["working-vm"], "Working")
        self.assertEqual(states["active-vm"], "Active")
        self.assertEqual(states["changing-vm"], "Changing")

    def test_prepare_vm_connection_starts_headless_vm(self) -> None:
        """Start a powered-off VM before attaching to its serial console."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            serial_port=2345,
            vm_state="poweroff",
        )

        service = PySnapService(client=client, session_registry=FakeSessionRegistry())
        vm_info = service.prepare_vm_connection("srv", timeout=2.0)

        self.assertEqual(vm_info.vm_state, "running")
        self.assertIn(("start_vm_headless", "srv"), client.calls)

    def test_stop_runtime_vm_uses_acpi_shutdown(self) -> None:
        """Stop one active VM through an ACPI power button request."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            serial_port=2345,
            vm_state="running",
        )

        service = PySnapService(client=client, session_registry=FakeSessionRegistry())
        service.stop_runtime_vm("srv", timeout=2.0)

        self.assertIn(("stop_vm_acpi", "srv"), client.calls)


if __name__ == "__main__":
    unittest.main()
