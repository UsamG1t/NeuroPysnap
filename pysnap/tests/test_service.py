"""Unit tests for the PySnap service layer."""

from __future__ import annotations

import tempfile
import unittest

from pysnap.core.models import ImportCandidate, VMInfo, VMReference
from pysnap.core.service import PySnapService
from pysnap.errors import VMDependencyError


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

    def list_vms(self) -> list[VMReference]:
        """Return known VM references."""
        return list(self.references.values())

    def get_vm_info(self, vm_name: str) -> VMInfo:
        """Return detailed VM information."""
        return self.infos[vm_name]

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


if __name__ == "__main__":
    unittest.main()
