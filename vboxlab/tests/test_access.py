"""Tests for the access model: COM1 console or ssh."""

from __future__ import annotations

from dataclasses import replace
import io
import tempfile
import unittest

from vboxlab.cli.app import run_cli
from vboxlab.core.access import (
    ACCESS_KEY,
    LAB_KEY,
    SSH_USER_KEY,
    parse_ssh_forwarding,
    resolve_access,
    ssh_forwarding_rule,
    validate_access_mode,
    validate_lab_flag,
    validate_ssh_user,
)
from vboxlab.core.models import ComPortSetting, NicSetting, VMHardware, VMInfo, VMReference
from vboxlab.core.service import VBoxLabService
from vboxlab.errors import VBoxLabError
from vboxlab.tests.test_service import FakeClient, FakeSessionRegistry
from vboxlab.tests.test_vbox_client import FakeRunner
from vboxlab.vbox.client import VBoxManageClient


def _info(name: str, **changes) -> VMInfo:
    return VMInfo(name=name, uuid=f"uuid-{name}", groups=("/Lab",), **changes)


class ResolveAccessTests(unittest.TestCase):
    """Verify the choice between COM1 and ssh."""

    def test_reads_the_access_stored_in_the_vm(self) -> None:
        """Use the stored access and lab property; ssh without a stored access."""
        cases = [
            (_info("vm"), "ssh", False, False),
            (_info("clone", parent_name="base"), "ssh", False, False),
            (_info("vm", metadata={ACCESS_KEY: "ssh"}), "ssh", True, False),
            (_info("vm", metadata={ACCESS_KEY: " COM "}), "com", True, False),
            (_info("vm", metadata={ACCESS_KEY: "nonsense"}), "ssh", False, False),
            (_info("vm", metadata={ACCESS_KEY: "com", LAB_KEY: "true"}), "com", True, True),
            (_info("vm", metadata={LAB_KEY: "yes"}), "ssh", False, False),
        ]
        for info, mode, configured, lab in cases:
            with self.subTest(metadata=info.metadata):
                access = resolve_access(info)
                self.assertEqual((access.mode, access.configured, access.lab), (mode, configured, lab))

    def test_reports_connection_details(self) -> None:
        """Carry ports and the ssh user, root by default."""
        access = resolve_access(_info("vm", serial_port=2326, ssh_port=2400))
        self.assertEqual((access.serial_port, access.ssh_port, access.ssh_user), (2326, 2400, "root"))
        custom = resolve_access(_info("vm", metadata={SSH_USER_KEY: "student"}))
        self.assertEqual(custom.ssh_user, "student")

    def test_validates_values(self) -> None:
        """Accept com, ssh, yes/no for lab and plain user names only."""
        self.assertEqual(validate_access_mode(" SSH "), "ssh")
        self.assertEqual((validate_lab_flag(" Yes"), validate_lab_flag("no")), (True, False))
        with self.assertRaisesRegex(VBoxLabError, 'lab: invalid value "true"; use yes or no'):
            validate_lab_flag("true")
        self.assertEqual(validate_ssh_user("st_ud-ent.1"), "st_ud-ent.1")
        for bad in ("serial", "", "auto"):
            with self.subTest(value=bad), self.assertRaisesRegex(VBoxLabError, "access: invalid"):
                validate_access_mode(bad)
        for bad in ("", "1user", "a b", "root;rm", "x" * 33):
            with self.subTest(value=bad), self.assertRaisesRegex(VBoxLabError, "ssh-user: invalid"):
                validate_ssh_user(bad)

    def test_forwarding_rules(self) -> None:
        """Build and recognize the VBoxLab ssh rule."""
        self.assertEqual(ssh_forwarding_rule(2400), "vboxlab-ssh,tcp,127.0.0.1,2400,,22")
        self.assertEqual(parse_ssh_forwarding("vboxlab-ssh,tcp,127.0.0.1,2400,,22"), 2400)
        self.assertIsNone(parse_ssh_forwarding("web,tcp,,8080,,80"))
        self.assertIsNone(parse_ssh_forwarding("vboxlab-ssh,udp,,2400,,22"))


class ClientTests(unittest.TestCase):
    """Verify VBoxManage calls for ssh forwarding."""

    def test_reads_the_rule_and_its_adapter(self) -> None:
        """Find the rule after the natnetN line of its adapter."""
        output = (
            'name="vm"\nUUID="u"\nnic1="intnet"\nnic2="nat"\nnatnet2="nat"\n'
            'Forwarding(0)="web,tcp,,8080,,80"\nForwarding(1)="vboxlab-ssh,tcp,127.0.0.1,2400,,22"\n'
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
            ("modifyvm", "vm", "--natpf1", "vboxlab-ssh,tcp,127.0.0.1,2400,,22"),
            ("controlvm", "vm", "natpf2", "vboxlab-ssh,tcp,127.0.0.1,2401,,22"),
            ("modifyvm", "vm", "--natpf1", "delete", "vboxlab-ssh"),
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
        self.client = AccessClient()
        self.service = VBoxLabService(
            client=self.client,
            session_registry=FakeSessionRegistry(),
        )
        self.service._is_host_tcp_port_available = lambda port: True

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _calls(self, name: str) -> list[tuple]:
        return [call for call in self.client.calls if call[0] == name]

    def test_set_changes_access_lab_and_user_in_any_state(self) -> None:
        """Store access, lab and user without stopping the VM when no port changes."""
        self.client.add("vm", state="running", serial_port=2326, ssh_port=2400)
        result = self.service.set_vm_settings("vm", ["access=ssh", "lab=yes", "ssh-user=student"])
        self.assertEqual(
            (result.access.mode, result.access.configured, result.access.lab, result.access.ssh_user),
            ("ssh", True, True, "student"),
        )
        result = self.service.set_vm_settings("vm", ["lab=no"])
        self.assertFalse(result.access.lab)
        self.assertEqual(self._calls("delete_metadata"), [("delete_metadata", "vm", LAB_KEY)])
        with self.assertRaisesRegex(VBoxLabError, "must be stopped"):
            self.service.set_vm_settings("vm", ["access=ssh", "nic2=x"])

    def test_set_warns_about_ssh_vms_without_nat(self) -> None:
        """Tell that ssh needs a NAT adapter."""
        self.client.add("vm")
        self.client.adapters["vm"] = (NicSetting("intnet", "x"),)
        result = self.service.set_vm_settings("vm", ["ssh-user=student"])
        self.assertIn("has no NAT adapter", result.warnings[0])

    def _copy_rules_on_clone(self) -> None:
        """Let a clone copy the ssh rule of its base, as VirtualBox does."""
        original = self.client.clone_linked

        def clone_linked(base_vm, clone_vm, group, snapshot_name):
            original(base_vm, clone_vm, group, snapshot_name)
            if base_vm in self.client.ssh_rules:
                self.client.ssh_rules[clone_vm] = self.client.ssh_rules[base_vm]
                self.client.infos[clone_vm] = replace(self.client.infos[clone_vm], ssh_port=2400)

        self.client.clone_linked = clone_linked

    def test_clone_of_an_ssh_base_forwards_its_own_port(self) -> None:
        """Networks on NIC2-NIC4, the base rule dropped and a new port forwarded."""
        self.client.add("base", ssh_port=2400, metadata={ACCESS_KEY: "ssh", SSH_USER_KEY: "student"})
        self.client.snapshot_names["base"] = "snap"
        self._copy_rules_on_clone()
        info = self.service.clone_vm("base", "first", serial_port=2500, networks=("left",))
        self.assertIn(("configure_serial_port", "first", 2500), self.client.calls)
        self.assertIn(("configure_internal_networks", "first", ("left",)), self.client.calls)
        self.assertIn(("remove_ssh_forwarding", "first", 1), self.client.calls)
        self.assertEqual(self._calls("add_ssh_forwarding"), [("add_ssh_forwarding", "first", 1, 2501, False)])
        self.assertEqual(info.ssh_port, 2501)
        self.assertEqual((info.metadata[ACCESS_KEY], info.metadata[SSH_USER_KEY]), ("ssh", "student"))
        self.assertNotIn(LAB_KEY, info.metadata)
        self.assertEqual(self._calls("configure_dmi_system_information"), [])

    def test_clone_of_a_base_without_access_forwards_nothing(self) -> None:
        """Keep a clone of a clean import without stored access and forwarded port."""
        self.client.add("clean", ssh_port=2400)
        self.client.snapshot_names["clean"] = "snap"
        self._copy_rules_on_clone()
        info = self.service.clone_vm("clean", "first", serial_port=2500)
        self.assertIn(("remove_ssh_forwarding", "first", 1), self.client.calls)
        self.assertEqual(self._calls("add_ssh_forwarding"), [])
        self.assertIsNone(info.ssh_port)
        self.assertNotIn(ACCESS_KEY, info.metadata)
        self.assertFalse(self.service.get_vm_access("first").configured)

    def test_clone_of_com_bases_keeps_com1(self) -> None:
        """Copy COM1 access, and the lab property with its DMI data for lab VMs only."""
        self.client.add("lab", metadata={ACCESS_KEY: "com", LAB_KEY: "true"})
        self.client.add("serial", metadata={ACCESS_KEY: "com"})
        self.client.snapshot_names.update(lab="s", serial="s")
        self.service.clone_vm("lab", "a", serial_port=2500, networks=("left",))
        self.service.clone_vm("serial", "b", serial_port=2501, networks=("left",))
        self.assertIn(("configure_internal_networks", "a", ("left",)), self.client.calls)
        self.assertIn(("configure_internal_networks", "b", ("left",)), self.client.calls)
        self.assertEqual(self._calls("add_ssh_forwarding"), [])
        self.assertEqual(
            [call[1] for call in self._calls("configure_dmi_system_information")], ["a"]
        )
        self.assertEqual(self.client.infos["a"].metadata[LAB_KEY], "true")
        self.assertNotIn(LAB_KEY, self.client.infos["b"].metadata)
        for name in ("a", "b"):
            access = self.service.get_vm_access(name)
            self.assertEqual((access.mode, access.configured), ("com", True))

    def test_clone_needs_a_nat_adapter_the_networks_keep(self) -> None:
        """Refuse before cloning when ssh has no NAT adapter or networks would replace it."""
        self.client.add("isolated", metadata={ACCESS_KEY: "ssh"})
        self.client.adapters["isolated"] = (NicSetting("intnet", "x"),)
        with self.assertRaisesRegex(VBoxLabError, "no NAT adapter; run vboxlab set isolated nic1=nat"):
            self.service.clone_vm("isolated", "a")
        self.client.add("second", metadata={ACCESS_KEY: "ssh"})
        self.client.adapters["second"] = (NicSetting("none"), NicSetting("nat"))
        with self.assertRaisesRegex(VBoxLabError, "would replace NIC2, the NAT adapter"):
            self.service.clone_vm("second", "b", networks=("left",))
        self.assertEqual(self._calls("clone_linked"), [])
        self.client.snapshot_names["second"] = "s"
        self.service.clone_vm("second", "c", serial_port=2600)
        self.assertEqual(self._calls("add_ssh_forwarding"), [("add_ssh_forwarding", "c", 2, 2601, False)])

    def test_monitor_and_port_allocation_know_ssh_ports(self) -> None:
        """Show ssh VMs with their port and keep their ports out of new allocations."""
        self.client.add("vm", state="running", serial_port=2326, ssh_port=2327)
        self.client.add("fresh")
        records = self.service.list_monitored_vms()
        self.assertEqual((records[0].access_mode, records[0].ssh_port), ("ssh", 2327))
        self.assertEqual(self.service.set_vm_settings("fresh", ["access=ssh"]).access.ssh_port, 2328)


class CommandTests(unittest.TestCase):
    """Verify the access shown by show and monitor."""

    def _run(self, *arguments: str) -> str:
        client = AccessClient()
        client.add("vm", state="running", serial_port=2326, ssh_port=2400,
                   metadata={SSH_USER_KEY: "student"})
        service = VBoxLabService(
            client=client,
            session_registry=FakeSessionRegistry(),
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
