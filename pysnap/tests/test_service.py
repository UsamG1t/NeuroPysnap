"""Unit tests for the PySnap service layer."""

from __future__ import annotations

import io
import threading
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from pysnap.config.protosettings import ProtoSettingsStore
from pysnap.core.models import ImportCandidate, VMInfo, VMMonitorRecord, VMReference
from pysnap.core.models import SerialPortConfiguration
from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError, VMDependencyError
from pysnap.runtime.sessions import SessionRecord


def _minimal_ovf(vm_names: list[str]) -> str:
    """Build a small OVF descriptor for one or more virtual systems."""
    systems = "\n".join(
        (
            f'  <VirtualSystem ovf:id="{vm_name}">\n'
            "    <Info>A virtual machine</Info>\n"
            f"    <Name>{vm_name}</Name>\n"
            "  </VirtualSystem>"
        )
        for vm_name in vm_names
    )
    return (
        '<?xml version="1.0"?>\n'
        '<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" '
        'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">\n'
        f"{systems}\n"
        "</Envelope>\n"
    )


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

    @contextmanager
    def register(self, vm_name: str, serial_port: int):
        """Register a temporary live session for one VM."""
        record = SessionRecord(
            vm_name=vm_name,
            serial_port=serial_port,
            pid=1,
            attached_at="2026-03-31T00:00:00+00:00",
        )
        self.live_sessions[vm_name] = record
        try:
            yield record
        finally:
            self.live_sessions.pop(vm_name, None)


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
        self.serial_configurations: dict[str, SerialPortConfiguration] = {}

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

    def get_serial_port_configuration(self, vm_name: str) -> SerialPortConfiguration:
        """Return the configured raw UART1 state."""
        if vm_name in self.serial_configurations:
            return self.serial_configurations[vm_name]
        current = self.infos[vm_name]
        if current.serial_port is not None:
            return SerialPortConfiguration(
                enabled=True,
                mode="tcpserver",
                port=current.serial_port,
            )
        return SerialPortConfiguration(enabled=False)

    def import_appliance(
        self,
        image_path: str,
        candidates: list[ImportCandidate],
        progress_callback=None,
    ) -> None:
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
        self.serial_configurations[vm_name] = SerialPortConfiguration(
            enabled=True,
            mode="tcpserver",
            port=serial_port,
        )
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

    def configure_dmi_system_information(
        self,
        vm_name: str,
        system_vendor: str,
        system_sku: str,
    ) -> None:
        """Record DMI system information configuration."""
        self.calls.append(
            ("configure_dmi_system_information", vm_name, system_vendor, system_sku)
        )

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
        if not self.state_sequences.get(vm_name):
            self.state_sequences[vm_name] = ["running"]

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
        if not self.state_sequences.get(vm_name):
            self.state_sequences[vm_name] = ["poweroff"]


class ServiceTests(unittest.TestCase):
    """Verify PySnap service behavior."""

    def setUp(self) -> None:
        """Create an isolated proto-settings store for every test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proto_settings_store = ProtoSettingsStore(
            path=Path(self.temp_dir.name) / ".ptotosettings"
        )

    def tearDown(self) -> None:
        """Clean up the temporary proto-settings store."""
        self.temp_dir.cleanup()

    def make_service(
        self,
        client: FakeClient,
        session_registry: FakeSessionRegistry | None = None,
        serial_probe_factory=None,
    ) -> PySnapService:
        """Create a service instance with the isolated proto-settings store."""
        return PySnapService(
            client=client,
            session_registry=session_registry,
            serial_probe_factory=serial_probe_factory,
            proto_settings_store=self.proto_settings_store,
        )

    def make_appliance(
        self,
        vm_names: list[str],
        suffix: str = ".ova",
    ) -> tempfile.NamedTemporaryFile:
        """Create a temporary OVF or OVA appliance for service tests."""
        appliance = tempfile.NamedTemporaryFile(suffix=suffix)
        descriptor = _minimal_ovf(vm_names).encode("utf-8")
        if suffix == ".ovf":
            appliance.write(descriptor)
            appliance.flush()
            return appliance

        ovf_name = f"{Path(appliance.name).stem}.ovf"
        with tarfile.open(appliance.name, "w") as archive:
            info = tarfile.TarInfo(name=ovf_name)
            info.size = len(descriptor)
            archive.addfile(info, fileobj=io.BytesIO(descriptor))
        appliance.flush()
        return appliance

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

        service = self.make_service(client=client)
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

        service = self.make_service(client=client)
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

        service = self.make_service(client=client)
        clone_info = service.clone_vm(base_vm="base-vm", clone_vm="clone-vm")

        self.assertEqual(clone_info.serial_port, 1024)
        self.assertIn(("configure_serial_port", "clone-vm", 1024), client.calls)

    def test_clone_vm_rejects_duplicate_clone_name(self) -> None:
        """Reject clone names that already exist in VirtualBox."""
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
            managed=True,
        )

        service = self.make_service(client=client)
        with self.assertRaises(PySnapError):
            service.clone_vm(base_vm="base-vm", clone_vm="clone-vm")

    def test_import_image_accepts_custom_name_for_single_vm_appliance(self) -> None:
        """Rename a single imported appliance VM through the optional VMName."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="original-vm", group="/Lab")
        ]

        service = self.make_service(client=client)
        with self.make_appliance(["original-vm"]) as appliance:
            imported = service.import_image(appliance.name, vm_name="renamed-vm")

        self.assertEqual([item.name for item in imported], ["renamed-vm"])
        self.assertIn(
            (
                "import_appliance",
                appliance.name,
                (
                    ImportCandidate(
                        vsys_index=0,
                        vm_name="renamed-vm",
                        group="/Lab",
                        requires_eula_accept=False,
                    ),
                ),
            ),
            client.calls,
        )

    def test_import_image_rejects_duplicate_default_name_with_recommendation(self) -> None:
        """Abort import when the final default VM name already exists."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="existing-vm", group="/Lab")
        ]
        client.references["existing-vm"] = VMReference("existing-vm", "uuid-existing")
        client.infos["existing-vm"] = VMInfo(
            name="existing-vm",
            uuid="uuid-existing",
            groups=("/Lab",),
            managed=True,
        )

        service = self.make_service(client=client)
        with self.make_appliance(["existing-vm"]) as appliance:
            with self.assertRaises(PySnapError) as context:
                service.import_image(appliance.name)

        self.assertIn("Use the optional VMName parameter", str(context.exception))
        import_calls = [call for call in client.calls if call[0] == "import_appliance"]
        self.assertEqual(import_calls, [])

    def test_import_image_rejects_duplicate_custom_name(self) -> None:
        """Abort import when the requested custom VM name already exists."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="original-vm", group="/Lab")
        ]
        client.references["renamed-vm"] = VMReference("renamed-vm", "uuid-existing")
        client.infos["renamed-vm"] = VMInfo(
            name="renamed-vm",
            uuid="uuid-existing",
            groups=("/Lab",),
            managed=True,
        )

        service = self.make_service(client=client)
        with self.make_appliance(["original-vm"]) as appliance:
            with self.assertRaises(PySnapError) as context:
                service.import_image(appliance.name, vm_name="renamed-vm")

        self.assertIn('Virtual machine "renamed-vm" already exists.', str(context.exception))
        import_calls = [call for call in client.calls if call[0] == "import_appliance"]
        self.assertEqual(import_calls, [])

    def test_import_image_rejects_custom_name_for_multi_vm_appliance(self) -> None:
        """Require exactly one VM when a custom import name is supplied."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="vm-a", group="/Lab"),
            ImportCandidate(vsys_index=1, vm_name="vm-b", group="/Lab"),
        ]

        service = self.make_service(client=client)
        with self.make_appliance(["vm-a", "vm-b"]) as appliance:
            with self.assertRaises(PySnapError) as context:
                service.import_image(appliance.name, vm_name="renamed-vm")

        self.assertIn("Custom VMName can only be used", str(context.exception))

    def test_import_image_rejects_collision_even_when_dry_run_suggests_renamed_vm(self) -> None:
        """Reject imports that VirtualBox would auto-rename after a collision."""
        client = FakeClient()
        client.import_candidates = [
            ImportCandidate(vsys_index=0, vm_name="existing-vm 1", group="/Lab")
        ]
        client.references["existing-vm"] = VMReference("existing-vm", "uuid-existing")
        client.infos["existing-vm"] = VMInfo(
            name="existing-vm",
            uuid="uuid-existing",
            groups=("/Lab",),
            managed=True,
        )

        service = self.make_service(client=client)
        with self.make_appliance(["existing-vm"]) as appliance:
            with self.assertRaises(PySnapError) as context:
                service.import_image(appliance.name)

        self.assertIn('Virtual machine "existing-vm" already exists.', str(context.exception))
        self.assertIn("Use the optional VMName parameter", str(context.exception))

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

        service = self.make_service(client=client)
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

        service = self.make_service(client=client)
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
        probe_calls: list[tuple[str, int]] = []

        @contextmanager
        def fake_serial_probe(host: str, port: int):
            """Simulate a successful serial probe connection."""
            probe_calls.append((host, port))
            yield object()

        service = self.make_service(
            client=client,
            session_registry=FakeSessionRegistry(),
            serial_probe_factory=fake_serial_probe,
        )
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
        self.assertEqual(
            probe_calls,
            [("localhost", 1024)],
        )
        monitor_states = {
            record.name: record.display_state for record in result.monitor_records
        }
        self.assertEqual(monitor_states["pysnap-it-case1234-clone-a"], "Working")
        self.assertEqual(monitor_states["pysnap-it-case1234-clone-b"], "Active")
        self.assertIn(("start_vm_headless", "pysnap-it-case1234-clone-a"), client.calls)
        self.assertIn(("start_vm_headless", "pysnap-it-case1234-clone-b"), client.calls)
        self.assertIn(("stop_vm_acpi", "pysnap-it-case1234-clone-a"), client.calls)
        self.assertIn(("stop_vm_acpi", "pysnap-it-case1234-clone-b"), client.calls)

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

        service = self.make_service(client=client, session_registry=registry)
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

        service = self.make_service(client=client, session_registry=FakeSessionRegistry())
        vm_info = service.prepare_vm_connection("srv", timeout=2.0)

        self.assertEqual(vm_info.vm_state, "running")
        self.assertIn(("start_vm_headless", "srv"), client.calls)

    def test_plug_vm_assigns_tcpserver_uart1_to_stopped_vm(self) -> None:
        """Configure a stopped VM for future PySnap serial connections."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            vm_state="poweroff",
        )

        service = self.make_service(client=client)
        service._is_host_tcp_port_available = lambda port: True
        vm_info = service.plug_vm("srv")

        self.assertEqual(vm_info.serial_port, 1024)
        self.assertIn(("configure_serial_port", "srv", 1024), client.calls)

    def test_plug_vm_returns_existing_tcpserver_configuration(self) -> None:
        """Keep an existing tcpserver UART1 configuration unchanged."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            serial_port=2345,
            vm_state="running",
        )
        client.serial_configurations["srv"] = SerialPortConfiguration(
            enabled=True,
            mode="tcpserver",
            port=2345,
        )

        service = self.make_service(client=client)
        vm_info = service.plug_vm("srv")

        self.assertEqual(vm_info.serial_port, 2345)
        configure_calls = [call for call in client.calls if call[0] == "configure_serial_port"]
        self.assertEqual(configure_calls, [])

    def test_plug_vm_rejects_non_tcp_uart_backend(self) -> None:
        """Reject VMs whose UART1 is already bound to another backend."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            vm_state="poweroff",
        )
        client.serial_configurations["srv"] = SerialPortConfiguration(
            enabled=True,
            mode="tcpclient",
        )

        service = self.make_service(client=client)
        with self.assertRaises(PySnapError):
            service.plug_vm("srv")

    def test_plug_vm_requires_stopped_vm_when_reconfiguration_is_needed(self) -> None:
        """Reject live VMs that would need a UART1 reconfiguration."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            vm_state="running",
        )

        service = self.make_service(client=client)
        with self.assertRaises(PySnapError):
            service.plug_vm("srv")

    def test_plug_vm_skips_busy_host_ports(self) -> None:
        """Probe host ports until one is available for tcpserver mode."""
        client = FakeClient()
        client.references["srv"] = VMReference("srv", "uuid-srv")
        client.references["existing"] = VMReference("existing", "uuid-existing")
        client.infos["srv"] = VMInfo(
            name="srv",
            uuid="uuid-srv",
            groups=("/Lab",),
            vm_state="poweroff",
        )
        client.infos["existing"] = VMInfo(
            name="existing",
            uuid="uuid-existing",
            groups=("/Lab",),
            serial_port=1024,
            vm_state="poweroff",
        )

        service = self.make_service(client=client)
        service._is_host_tcp_port_available = lambda port: port >= 1026
        vm_info = service.plug_vm("srv")

        self.assertEqual(vm_info.serial_port, 1026)
        self.assertIn(("configure_serial_port", "srv", 1026), client.calls)

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

        service = self.make_service(client=client, session_registry=FakeSessionRegistry())
        service.stop_runtime_vm("srv", timeout=2.0)

        self.assertIn(("stop_vm_acpi", "srv"), client.calls)

    def test_stop_all_runtime_vms_issues_acpi_requests_in_parallel(self) -> None:
        """Send ACPI shutdown requests for all running VMs before waiting."""

        class ParallelStopClient(FakeClient):
            """Require parallel ACPI stop requests for the first stop wave."""

            def __init__(self) -> None:
                """Initialize synchronization helpers for stop requests."""
                super().__init__()
                self.stop_started: set[str] = set()
                self.stop_lock = threading.Lock()
                self.all_stops_started = threading.Event()

            def stop_vm_acpi(self, vm_name: str) -> None:
                """Require both stop requests to be issued concurrently."""
                super().stop_vm_acpi(vm_name)
                with self.stop_lock:
                    self.stop_started.add(vm_name)
                    if len(self.stop_started) == 2:
                        self.all_stops_started.set()
                if not self.all_stops_started.wait(0.5):
                    raise AssertionError("ACPI stop requests were not issued in parallel.")

        client = ParallelStopClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.references["clone-vm"] = VMReference("clone-vm", "uuid-clone")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            serial_port=2201,
            vm_state="running",
        )
        client.infos["clone-vm"] = VMInfo(
            name="clone-vm",
            uuid="uuid-clone",
            groups=("/Lab",),
            serial_port=2202,
            vm_state="running",
        )

        service = self.make_service(client=client, session_registry=FakeSessionRegistry())
        stopped = service.stop_all_runtime_vms(timeout=2.0)

        self.assertEqual(stopped, ["base-vm", "clone-vm"])
        self.assertEqual(client.stop_started, {"base-vm", "clone-vm"})

    def test_erase_all_deletes_independent_leaves_in_parallel_waves(self) -> None:
        """Delete independent descendants together before touching parents."""

        class ParallelDeleteClient(FakeClient):
            """Require first-wave deletions to start in parallel."""

            def __init__(self) -> None:
                """Initialize synchronization helpers for delete requests."""
                super().__init__()
                self.first_wave = {"clone-a", "clone-b"}
                self.first_wave_started: set[str] = set()
                self.first_wave_lock = threading.Lock()
                self.all_first_wave_started = threading.Event()

            def delete_vm(self, vm_name: str) -> None:
                """Require the initial leaf deletions to run together."""
                if vm_name in self.first_wave:
                    with self.first_wave_lock:
                        self.first_wave_started.add(vm_name)
                        if self.first_wave_started == self.first_wave:
                            self.all_first_wave_started.set()
                    if not self.all_first_wave_started.wait(0.5):
                        raise AssertionError(
                            "Independent leaf deletions were not started in parallel."
                        )
                super().delete_vm(vm_name)

        client = ParallelDeleteClient()
        client.references["base-a"] = VMReference("base-a", "uuid-base-a")
        client.references["clone-a"] = VMReference("clone-a", "uuid-clone-a")
        client.references["base-b"] = VMReference("base-b", "uuid-base-b")
        client.references["clone-b"] = VMReference("clone-b", "uuid-clone-b")
        client.infos["base-a"] = VMInfo(
            name="base-a",
            uuid="uuid-base-a",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-a"] = VMInfo(
            name="clone-a",
            uuid="uuid-clone-a",
            groups=("/Lab",),
            parent_name="base-a",
            managed=True,
        )
        client.infos["base-b"] = VMInfo(
            name="base-b",
            uuid="uuid-base-b",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-b"] = VMInfo(
            name="clone-b",
            uuid="uuid-clone-b",
            groups=("/Lab",),
            parent_name="base-b",
            managed=True,
        )

        service = self.make_service(client=client)
        deleted = service.erase_all()

        self.assertEqual(deleted, ["base-a", "base-b", "clone-a", "clone-b"])
        delete_names = [call[1] for call in client.calls if call[0] == "delete_vm"]
        first_parent_index = min(delete_names.index("base-a"), delete_names.index("base-b"))
        last_clone_index = max(delete_names.index("clone-a"), delete_names.index("clone-b"))
        self.assertLess(last_clone_index, first_parent_index)

    def test_erase_group_deletes_independent_leaves_in_parallel_waves(self) -> None:
        """Delete the selected group in parallel waves while preserving cascade order."""

        class ParallelDeleteClient(FakeClient):
            """Require first-wave group deletions to start in parallel."""

            def __init__(self) -> None:
                """Initialize synchronization helpers for group delete requests."""
                super().__init__()
                self.first_wave = {"clone-a", "clone-b"}
                self.first_wave_started: set[str] = set()
                self.first_wave_lock = threading.Lock()
                self.all_first_wave_started = threading.Event()

            def delete_vm(self, vm_name: str) -> None:
                """Require the selected leaf deletions to run together."""
                if vm_name in self.first_wave:
                    with self.first_wave_lock:
                        self.first_wave_started.add(vm_name)
                        if self.first_wave_started == self.first_wave:
                            self.all_first_wave_started.set()
                    if not self.all_first_wave_started.wait(0.5):
                        raise AssertionError(
                            "Independent group deletions were not started in parallel."
                        )
                super().delete_vm(vm_name)

        client = ParallelDeleteClient()
        client.references["base-a"] = VMReference("base-a", "uuid-base-a")
        client.references["clone-a"] = VMReference("clone-a", "uuid-clone-a")
        client.references["base-b"] = VMReference("base-b", "uuid-base-b")
        client.references["clone-b"] = VMReference("clone-b", "uuid-clone-b")
        client.references["other-vm"] = VMReference("other-vm", "uuid-other")
        client.infos["base-a"] = VMInfo(
            name="base-a",
            uuid="uuid-base-a",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-a"] = VMInfo(
            name="clone-a",
            uuid="uuid-clone-a",
            groups=("/Lab",),
            parent_name="base-a",
            managed=True,
        )
        client.infos["base-b"] = VMInfo(
            name="base-b",
            uuid="uuid-base-b",
            groups=("/Lab",),
            managed=True,
        )
        client.infos["clone-b"] = VMInfo(
            name="clone-b",
            uuid="uuid-clone-b",
            groups=("/Lab",),
            parent_name="base-b",
            managed=True,
        )
        client.infos["other-vm"] = VMInfo(
            name="other-vm",
            uuid="uuid-other",
            groups=("/Other",),
            managed=True,
        )

        service = self.make_service(client=client)
        deleted = service.erase_group("/Lab")

        self.assertEqual(deleted, ["base-a", "base-b", "clone-a", "clone-b"])
        self.assertIn("other-vm", client.references)
        delete_names = [call[1] for call in client.calls if call[0] == "delete_vm"]
        first_parent_index = min(delete_names.index("base-a"), delete_names.index("base-b"))
        last_clone_index = max(delete_names.index("clone-a"), delete_names.index("clone-b"))
        self.assertLess(last_clone_index, first_parent_index)

    def test_register_proto_settings_vm_adds_base_vm_without_duplicates(self) -> None:
        """Persist one base VM in the proto-settings file."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )

        service = self.make_service(client=client)
        first = service.register_proto_settings_vm("base-vm")
        second = service.register_proto_settings_vm("base-vm")

        self.assertEqual(first, ("base-vm",))
        self.assertEqual(second, ("base-vm",))
        self.assertEqual(self.proto_settings_store.list_vm_names(), ("base-vm",))

    def test_build_proto_system_sku_formats_port_and_networks(self) -> None:
        """Build the expected DMI system SKU for proto-settings clones."""
        service = self.make_service(client=FakeClient())

        self.assertEqual(service._build_proto_system_sku(2345, ()), "port2345")
        self.assertEqual(
            service._build_proto_system_sku(2345, ("intnet",)),
            "port2345.intnet",
        )
        self.assertEqual(
            service._build_proto_system_sku(2345, ("intnet", "deepnet")),
            "port2345.intnet.deepnet",
        )
        self.assertEqual(
            service._build_proto_system_sku(2345, ("intnet", "deepnet", "virtnet")),
            "port2345.intnet.deepnet.virtnet",
        )

    def test_clone_vm_applies_proto_settings_for_registered_base(self) -> None:
        """Apply DMI data to clones whose base VM is in proto-settings."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.snapshot_names["base-vm"] = "existing-snapshot"
        self.proto_settings_store.add_vm_name("base-vm")

        service = self.make_service(client=client)
        service.clone_vm(
            base_vm="base-vm",
            clone_vm="clone-vm",
            serial_port=2345,
            networks=("intnet", "deepnet"),
        )

        self.assertIn(
            (
                "configure_dmi_system_information",
                "clone-vm",
                "clone-vm",
                "port2345.intnet.deepnet",
            ),
            client.calls,
        )

    def test_clone_vm_skips_proto_settings_for_unregistered_base(self) -> None:
        """Skip DMI configuration when the base VM is not registered."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        client.snapshot_names["base-vm"] = "existing-snapshot"

        service = self.make_service(client=client)
        service.clone_vm(base_vm="base-vm", clone_vm="clone-vm", serial_port=2345)

        dmi_calls = [
            call for call in client.calls if call[0] == "configure_dmi_system_information"
        ]
        self.assertEqual(dmi_calls, [])

    def test_erase_vm_removes_name_from_proto_settings(self) -> None:
        """Remove erased VM names from the proto-settings file."""
        client = FakeClient()
        client.references["base-vm"] = VMReference("base-vm", "uuid-base")
        client.infos["base-vm"] = VMInfo(
            name="base-vm",
            uuid="uuid-base",
            groups=("/Lab",),
            managed=True,
        )
        self.proto_settings_store.add_vm_name("base-vm")

        service = self.make_service(client=client)
        service.erase_vm("base-vm")

        self.assertEqual(self.proto_settings_store.list_vm_names(), ())


if __name__ == "__main__":
    unittest.main()
