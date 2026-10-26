"""Tests for the access model: COM1 console or ssh."""

from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest

from vboxlab.cli.app import run_cli
from vboxlab.config.protosettings import ProtoSettingsStore
from vboxlab.core.access import (
    ACCESS_KEY,
    SSH_USER_KEY,
    parse_ssh_forwarding,
    resolve_access,
    ssh_forwarding_rule,
    validate_access_mode,
    validate_ssh_user,
)
from vboxlab.core.models import ComPortSetting, NicSetting, VMHardware, VMInfo, VMReference
from vboxlab.core.service import PySnapService
from vboxlab.errors import PySnapError
from vboxlab.tests.test_service import FakeClient, FakeSessionRegistry
from vboxlab.tests.test_vbox_client import FakeRunner
from vboxlab.vbox.client import VBoxManageClient


def _info(name: str, **changes) -> VMInfo:
    return VMInfo(name=name, uuid=f"uuid-{name}", groups=("/Lab",), **changes)


class ResolveAccessTests(unittest.TestCase):
    """Verify the choice between COM1 and ssh."""

    def test_chooses_by_protosettings_and_override(self) -> None:
        """Use COM1 for registered bases and their clones, ssh otherwise."""
        proto = {"base"}
        cases = [
            (_info("base"), "com", "protosettings"),
            (_info("clone", parent_name="base"), "com", "protosettings"),
            (_info("other"), "ssh", "default"),
            (_info("other", parent_name="unregistered"), "ssh", "default"),
            (_info("base", metadata={ACCESS_KEY: "ssh"}), "ssh", "override"),
            (_info("other", metadata={ACCESS_KEY: "com"}), "com", "override"),
            (_info("other", metadata={ACCESS_KEY: "nonsense"}), "ssh", "default"),
        ]
        for info, mode, source in cases:
            with self.subTest(name=info.name, metadata=info.metadata):
                access = resolve_access(info, proto)
                self.assertEqual((access.mode, access.source), (mode, source))

    def test_reports_connection_details(self) -> None:
        """Carry ports and the ssh user, root by default."""
        access = resolve_access(_info("vm", serial_port=2326, ssh_port=2400), ())
        self.assertEqual((access.serial_port, access.ssh_port, access.ssh_user), (2326, 2400, "root"))
        custom = resolve_access(_info("vm", metadata={SSH_USER_KEY: "student"}), ())
        self.assertEqual(custom.ssh_user, "student")

    def test_validates_values(self) -> None:
        """Accept com, ssh, auto and plain user names only."""
        self.assertEqual(validate_access_mode(" SSH "), "ssh")
        self.assertEqual(validate_access_mode("auto"), "auto")
        self.assertEqual(validate_ssh_user("st_ud-ent.1"), "st_ud-ent.1")
        for bad in ("serial", ""):
            with self.subTest(value=bad), self.assertRaisesRegex(PySnapError, "access: invalid"):
                validate_access_mode(bad)
        for bad in ("", "1user", "a b", "root;rm", "x" * 33):
            with self.subTest(value=bad), self.assertRaisesRegex(PySnapError, "ssh-user: invalid"):
                validate_ssh_user(bad)

    def test_forwarding_rules(self) -> None:
        """Build and recognize the PySnap ssh rule."""
        self.assertEqual(ssh_forwarding_rule(2400), "pysnap-ssh,tcp,127.0.0.1,2400,,22")
        self.assertEqual(parse_ssh_forwarding("pysnap-ssh,tcp,127.0.0.1,2400,,22"), 2400)
        self.assertIsNone(parse_ssh_forwarding("web,tcp,,8080,,80"))
        self.assertIsNone(parse_ssh_forwarding("pysnap-ssh,udp,,2400,,22"))


class ClientTests(unittest.TestCase):
    """Verify VBoxManage calls for ssh forwarding."""

    def test_reads_the_rule_and_its_adapter(self) -> None:
        """Find the rule after the natnetN line of its adapter."""
        output = (
            'name="vm"\nUUID="u"\nnic1="intnet"\nnic2="nat"\nnatnet2="nat"\n'
            'Forwarding(0)="web,tcp,,8080,,80"\nForwarding(1)="pysnap-ssh,tcp,127.0.0.1,2400,,22"\n'
        )
        client = VBoxManageClient(runner=FakeRunner({("showvminfo", "vm", "--machinereadable"): output}))
        self.assertEqual(client.get_ssh_forwarding("vm"), (2, 2400))
        self.assertEqual(client.get_vm_info("vm").ssh_port, 2400)
        empty = VBoxManageClient(runner=FakeRunner({("showvminfo", "vm", "--machinereadable"): 'name="vm"\n'}))
        self.assertIsNone(empty.get_ssh_forwarding("vm"))
        self.assertIsNone(empty.get_vm_info("vm").ssh_port)

    def test_adds_and_removes_rules(self) -> None:
        """Use modifyvm for stopped VMs and controlvm for running ones."""
        runner = FakeRunner()
        client = VBoxManageClient(runner=runner)
        client.add_ssh_forwarding("vm", 1, 2400, running=False)
        client.add_ssh_forwarding("vm", 2, 2401, running=True)
        client.remove_ssh_forwarding("vm", 1)
        client.delete_metadata("vm", ACCESS_KEY)
        self.assertEqual(runner.commands, [
            ("modifyvm", "vm", "--natpf1", "pysnap-ssh,tcp,127.0.0.1,2400,,22"),
            ("controlvm", "vm", "natpf2", "pysnap-ssh,tcp,127.0.0.1,2401,,22"),
            ("modifyvm", "vm", "--natpf1", "delete", "pysnap-ssh"),
            ("setextradata", "vm", ACCESS_KEY),
        ])


class AccessClient(FakeClient):
    """Fake client with metadata removal and per-VM adapters."""

    def __init__(self) -> None:
        super().__init__()
        self.adapters: dict[str, tuple[NicSetting, ...]] = {}

    def add(self, name: str, state: str = "poweroff", **changes) -> None:
        self.references[name] = VMReference(name=name, uuid=f"uuid-{name}")
        self.infos[name] = _info(name, vm_state=state, **changes)
        if changes.get("ssh_port") is not None:
            self.ssh_rules[name] = (1, changes["ssh_port"])

    def get_vm_hardware(self, vm_name: str) -> VMHardware:
        nics = self.adapters.get(vm_name, (NicSetting("nat"),))
        return VMHardware(com_ports=(ComPortSetting("off"),) * 4,
                          nics=nics + (NicSetting("none"),) * (8 - len(nics)))

    def delete_metadata(self, vm_name: str, key: str) -> None:
        self.calls.append(("delete_metadata", vm_name, key))
        metadata = dict(self.infos[vm_name].metadata)
        metadata.pop(key, None)
        self.infos[vm_name] = replace(self.infos[vm_name], metadata=metadata)


class ServiceTests(unittest.TestCase):
    """Verify plug, set, clone and monitor for ssh VMs."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.store = ProtoSettingsStore(path=Path(self._temp_dir.name) / ".ptotosettings")
        self.client = AccessClient()
        self.service = PySnapService(
            client=self.client,
            session_registry=FakeSessionRegistry(),
            proto_settings_store=self.store,
        )
        self.service._is_host_tcp_port_available = lambda port: True

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _calls(self, name: str) -> list[tuple]:
        return [call for call in self.client.calls if call[0] == name]

    def test_plug_forwards_ssh_for_ssh_vms(self) -> None:
        """Add a rule on the NAT adapter with modifyvm or controlvm."""
        self.client.add("other", serial_port=2326)
        self.client.add("stopped")
        self.client.add("running", state="running")
        self.client.adapters["running"] = (NicSetting("intnet", "x"), NicSetting("nat"))
        self.assertEqual(self.service.plug_vm("stopped").ssh_port, 2327)
        self.assertEqual(self.service.plug_vm("running").ssh_port, 2328)
        self.assertEqual(self._calls("add_ssh_forwarding"), [
            ("add_ssh_forwarding", "stopped", 1, 2327, False),
            ("add_ssh_forwarding", "running", 2, 2328, True),
        ])
        self.assertEqual(self._calls("configure_serial_port"), [])

    def test_plug_keeps_an_existing_rule_and_explains_problems(self) -> None:
        """Leave existing rules alone; refuse without NAT or while changing state."""
        self.client.add("ready", ssh_port=2400)
        self.assertEqual(self.service.plug_vm("ready").ssh_port, 2400)
        self.assertEqual(self._calls("add_ssh_forwarding"), [])

        self.client.add("isolated")
        self.client.adapters["isolated"] = (NicSetting("intnet", "x"),)
        with self.assertRaisesRegex(PySnapError, "no NAT adapter; run pysnap set isolated nic1=nat"):
            self.service.plug_vm("isolated")
        self.client.add("busy", state="starting")
        with self.assertRaisesRegex(PySnapError, "changing state"):
            self.service.plug_vm("busy")

    def test_plug_keeps_com_vms_on_com1(self) -> None:
        """Configure COM1 for protosettings VMs as before."""
        self.client.add("proto")
        self.store.add_vm_name("proto")
        self.service.plug_vm("proto")
        self.assertEqual(len(self._calls("configure_serial_port")), 1)
        self.assertEqual(self._calls("add_ssh_forwarding"), [])

    def test_set_changes_access_and_user_in_any_state(self) -> None:
        """Store the override and the user without stopping the VM."""
        self.client.add("vm", state="running")
        result = self.service.set_vm_settings("vm", ["access=com", "ssh-user=student"])
        self.assertEqual((result.access.mode, result.access.source, result.access.ssh_user),
                         ("com", "override", "student"))
        result = self.service.set_vm_settings("vm", ["access=auto"])
        self.assertEqual((result.access.mode, result.access.source), ("ssh", "default"))
        self.assertEqual(self._calls("delete_metadata"), [("delete_metadata", "vm", ACCESS_KEY)])
        with self.assertRaisesRegex(PySnapError, "must be stopped"):
            self.service.set_vm_settings("vm", ["access=ssh", "nic2=x"])

    def test_set_warns_about_ssh_vms_without_nat(self) -> None:
        """Tell that ssh needs a NAT adapter."""
        self.client.add("vm")
        self.client.adapters["vm"] = (NicSetting("intnet", "x"),)
        result = self.service.set_vm_settings("vm", ["ssh-user=student"])
        self.assertIn("has no NAT adapter", result.warnings[0])

    def test_clone_of_an_ordinary_base_is_an_ssh_vm(self) -> None:
        """Keep NIC1 on NAT, drop the base rule and forward a new port."""
        self.client.add("base", ssh_port=2400, metadata={SSH_USER_KEY: "student"})
        self.client.snapshot_names["base"] = "snap"
        original = self.client.clone_linked

        def clone_linked(base_vm, clone_vm, group, snapshot_name):
            original(base_vm, clone_vm, group, snapshot_name)
            self.client.ssh_rules[clone_vm] = self.client.ssh_rules[base_vm]
            self.client.infos[clone_vm] = replace(self.client.infos[clone_vm], ssh_port=2400)

        self.client.clone_linked = clone_linked
        info = self.service.clone_vm("base", "first", serial_port=2500)
        self.assertIn(("configure_internal_networks", "first", (), True), self.client.calls)
        self.assertIn(("remove_ssh_forwarding", "first", 1), self.client.calls)
        self.assertEqual(self._calls("add_ssh_forwarding"), [("add_ssh_forwarding", "first", 1, 2501, False)])
        self.assertEqual(info.ssh_port, 2501)
        self.assertEqual(info.metadata[SSH_USER_KEY], "student")
        self.assertEqual(self.service.get_vm_access("first").mode, "ssh")

    def test_clone_of_a_com_base_stays_on_com1(self) -> None:
        """Use the old layout without ssh for protosettings and com overrides."""
        self.client.add("proto")
        self.client.add("serial", metadata={ACCESS_KEY: "com"})
        self.client.snapshot_names.update(proto="s", serial="s")
        self.store.add_vm_name("proto")
        self.service.clone_vm("proto", "a", networks=("left",))
        self.service.clone_vm("serial", "b", networks=("left",))
        self.assertIn(("configure_internal_networks", "a", ("left",), True), self.client.calls)
        self.assertIn(("configure_internal_networks", "b", ("left",), False), self.client.calls)
        self.assertEqual(self._calls("add_ssh_forwarding"), [])
        self.assertEqual(self.client.infos["b"].metadata[ACCESS_KEY], "com")
        self.assertEqual(self.service.get_vm_access("b").source, "override")

    def test_monitor_and_port_allocation_know_ssh_ports(self) -> None:
        """Show ssh VMs with their port and keep their ports out of new allocations."""
        self.client.add("vm", state="running", serial_port=2326, ssh_port=2327)
        self.client.add("fresh")
        records = self.service.list_monitored_vms()
        self.assertEqual((records[0].access_mode, records[0].ssh_port), ("ssh", 2327))
        self.assertEqual(self.service.plug_vm("fresh").ssh_port, 2328)


class CommandTests(unittest.TestCase):
    """Verify the access shown by show and monitor."""

    def _run(self, *arguments: str) -> str:
        client = AccessClient()
        client.add("vm", state="running", serial_port=2326, ssh_port=2400,
                   metadata={SSH_USER_KEY: "student"})
        with tempfile.TemporaryDirectory() as temp_dir:
            service = PySnapService(
                client=client,
                session_registry=FakeSessionRegistry(),
                proto_settings_store=ProtoSettingsStore(path=Path(temp_dir) / ".ptotosettings"),
            )
            output = io.StringIO()
            run_cli(list(arguments), service=service, stdout=output, stderr=io.StringIO())
        return output.getvalue()

    def test_show_and_monitor(self) -> None:
        """Print the ssh target and the ssh port column."""
        self.assertIn("Access: ssh student@127.0.0.1:2400 (default)", self._run("show", "vm"))
        self.assertEqual(self._run("monitor"), "vm (state: Active ; ssh:2400 ; /Lab)\n")


if __name__ == "__main__":
    unittest.main()
