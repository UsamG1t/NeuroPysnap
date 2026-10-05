"""Tests for ``pysnap set``: serial ports and network adapters."""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from pysnap.cli.app import run_cli
from pysnap.config.protosettings import ProtoSettingsStore
from pysnap.core.models import ComPortSetting, NicSetting, VMHardware, VMInfo, VMReference
from pysnap.core.service import PySnapService
from pysnap.core.settings import host_pipe_name, host_pipe_path, parse_set_options
from pysnap.errors import PySnapError
from pysnap.tests.test_service import FakeClient
from pysnap.tests.test_vbox_client import FakeRunner
from pysnap.vbox.client import VBoxManageClient

OFF = ComPortSetting(mode="off")
NO_NIC = NicSetting(attachment="none")
SKU_KEY = VBoxManageClient.DMI_SYSTEM_SKU_KEY


class SettingsClient(FakeClient):
    """Fake client that also keeps serial ports and adapters."""

    def __init__(self) -> None:
        super().__init__()
        self.hardware: dict[str, VMHardware] = {}
        self.extra_data: dict[str, dict[str, str]] = {}

    def add_vm(self, name: str, *, state: str = "poweroff", com=(), nics=()) -> None:
        """Register a VM with the given COM ports and adapters."""
        com_ports = tuple(com) + (OFF,) * (4 - len(com))
        adapters = tuple(nics) + (NO_NIC,) * (4 - len(nics))
        self.references[name] = VMReference(name=name, uuid=f"uuid-{name}")
        self.hardware[name] = VMHardware(com_ports=com_ports, nics=adapters)
        self.infos[name] = VMInfo(name=name, uuid=f"uuid-{name}", groups=("/Lab",), vm_state=state)
        self._sync_ports(name)

    def _sync_ports(self, name: str) -> None:
        ports = tuple(
            setting.port for setting in self.hardware[name].com_ports
            if setting.mode == "tcpserver" and setting.port is not None
        )
        com1 = self.hardware[name].com_ports[0]
        self.infos[name] = replace(
            self.infos[name],
            serial_tcp_ports=ports,
            serial_port=com1.port if com1.mode == "tcpserver" else None,
        )

    def get_vm_hardware(self, vm_name: str) -> VMHardware:
        return self.hardware[vm_name]

    def apply_vm_hardware(self, vm_name, com_ports, nics) -> None:
        self.calls.append(("apply_vm_hardware", vm_name, dict(com_ports), dict(nics)))
        current = self.hardware[vm_name]
        com = list(current.com_ports)
        adapters = list(current.nics)
        for number, setting in com_ports.items():
            com[number - 1] = setting
        for number, setting in nics.items():
            adapters[number - 1] = setting
        self.hardware[vm_name] = VMHardware(com_ports=tuple(com), nics=tuple(adapters))
        self._sync_ports(vm_name)

    def get_metadata(self, vm_name: str) -> dict[str, str]:
        return dict(self.extra_data.get(vm_name, {}))

    def set_dmi_system_sku(self, vm_name: str, system_sku: str) -> None:
        self.calls.append(("set_dmi_system_sku", vm_name, system_sku))
        self.extra_data.setdefault(vm_name, {})[SKU_KEY] = system_sku


class ParseOptionsTests(unittest.TestCase):
    """Verify ``KEY=VALUE`` parsing."""

    def test_parses_ports_and_adapters(self) -> None:
        """Accept every documented value."""
        request = parse_set_options([
            "com1=auto", "COM2=2330", "com3=server:link", "com4=client:link-2",
            "nic1=nat", "nic2=left", "nic3=off", "nic4=Deep Net",
        ])
        self.assertEqual(request.com_ports[1].mode, "tcpserver")
        self.assertIsNone(request.com_ports[1].port)
        self.assertEqual(request.com_ports[2].port, 2330)
        self.assertEqual((request.com_ports[3].mode, request.com_ports[3].pipe_name), ("server", "link"))
        self.assertEqual(request.com_ports[4].mode, "client")
        self.assertEqual(request.nics[1], NicSetting("nat"))
        self.assertEqual(request.nics[2], NicSetting("intnet", "left"))
        self.assertEqual(request.nics[3], NicSetting("none"))
        self.assertEqual(request.nics[4], NicSetting("intnet", "Deep Net"))
        self.assertTrue(parse_set_options([]).empty)

    def test_rejects_invalid_options(self) -> None:
        """Explain unknown keys, repeated keys and bad values."""
        cases = {
            "com5=auto": "comN=VALUE",
            "nic2": "comN=VALUE",
            "speed=1": "comN=VALUE",
            "com1=70000": "between 1 and 65535",
            "com1=0": "between 1 and 65535",
            "com2=server:": "invalid pipe name",
            "com2=server:a/b": "invalid pipe name",
            "com2=pipe:x": "invalid value",
            "nic2=": "internal network name",
        }
        for option, message in cases.items():
            with self.subTest(option=option), self.assertRaisesRegex(PySnapError, message):
                parse_set_options([option])
        with self.assertRaisesRegex(PySnapError, "COM1 is given more than once"):
            parse_set_options(["com1=auto", "com1=off"])
        with self.assertRaisesRegex(PySnapError, "NIC2 is given more than once"):
            parse_set_options(["nic2=a", "NIC2=b"])

    def test_builds_host_pipe_paths(self) -> None:
        """Use the pipe format of the host system and read names back."""
        self.assertEqual(host_pipe_path("link", windows=True), "\\\\.\\pipe\\pysnap-link")
        posix = host_pipe_path("link", windows=False)
        self.assertEqual(Path(posix).name, "pysnap-link")
        self.assertTrue(Path(posix).is_absolute())
        self.assertEqual(host_pipe_name("\\\\.\\pipe\\pysnap-link"), "link")
        self.assertEqual(host_pipe_name(posix), "link")
        self.assertIsNone(host_pipe_name("/tmp/other"))


class ClientTests(unittest.TestCase):
    """Verify reading and writing settings through VBoxManage."""

    def test_reads_ports_and_adapters(self) -> None:
        """Parse every COM mode and adapter attachment."""
        runner = FakeRunner({
            ("showvminfo", "first", "--machinereadable"): (
                'name="first"\n'
                'uart1="0x03f8,4"\nuartmode1="tcpserver,2326"\n'
                'uart2="0x02f8,3"\nuartmode2="server,/tmp/pysnap-link"\n'
                'uart3="0x03e8,4"\nuartmode3="file,/tmp/log"\n'
                'uart4="off"\n'
                'nic1="nat"\nnic2="intnet"\nintnet2="left"\nnic3="none"\nnic4="bridged"\n'
            )
        })
        hardware = VBoxManageClient(runner=runner).get_vm_hardware("first")
        self.assertEqual(hardware.com_ports, (
            ComPortSetting("tcpserver", port=2326),
            ComPortSetting("server", path="/tmp/pysnap-link"),
            ComPortSetting("file", detail="/tmp/log"),
            OFF,
        ))
        self.assertEqual(hardware.nics, (
            NicSetting("nat"), NicSetting("intnet", "left"), NO_NIC, NicSetting("bridged"),
        ))

    def test_lists_tcp_ports_of_all_serial_ports(self) -> None:
        """Report TCP servers on any COM port in the VM information."""
        runner = FakeRunner({
            ("showvminfo", "first", "--machinereadable"): (
                'name="first"\nuart1="0x03f8,4"\nuartmode1="tcpserver,2326"\n'
                'uart2="0x02f8,3"\nuartmode2="tcpserver,2400"\n'
            ),
        })
        info = VBoxManageClient(runner=runner).get_vm_info("first")
        self.assertEqual(info.serial_tcp_ports, (2326, 2400))
        self.assertEqual(info.serial_port, 2326)

    def test_applies_all_changes_in_one_command(self) -> None:
        """Build one modifyvm call with standard COM resources."""
        runner = FakeRunner()
        VBoxManageClient(runner=runner).apply_vm_hardware(
            "first",
            {
                1: ComPortSetting("tcpserver", port=2326),
                2: ComPortSetting("client", path="/tmp/pysnap-link"),
                4: OFF,
            },
            {1: NicSetting("nat"), 2: NicSetting("intnet", "left"), 3: NO_NIC},
        )
        self.assertEqual(runner.commands, [(
            "modifyvm", "first",
            "--uart1", "0x3F8", "4", "--uartmode1", "tcpserver", "2326",
            "--uart2", "0x2F8", "3", "--uartmode2", "client", "/tmp/pysnap-link",
            "--uart4", "off",
            "--nic1", "nat", "--nic2", "intnet", "--intnet2", "left", "--nic3", "none",
        )])


class ServiceSettingsTests(unittest.TestCase):
    """Verify validation and effects of ``set``."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.client = SettingsClient()
        self.client.add_vm("first", com=[ComPortSetting("tcpserver", port=2326)], nics=[NicSetting("nat")])
        self.client.add_vm("second", com=[ComPortSetting("tcpserver", port=2327),
                                          ComPortSetting("tcpserver", port=2400)])
        self.service = PySnapService(
            client=self.client,
            proto_settings_store=ProtoSettingsStore(path=Path(self._temp_dir.name) / ".ptotosettings"),
        )
        self.busy_host_ports: set[int] = set()
        self.service._is_host_tcp_port_available = lambda port: port not in self.busy_host_ports

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _applied(self):
        return [call for call in self.client.calls if call[0] == "apply_vm_hardware"]

    def test_shows_settings_without_options(self) -> None:
        """Return the current settings and change nothing, in any state."""
        self.client.infos["first"] = replace(self.client.infos["first"], vm_state="running")
        result = self.service.set_vm_settings("first", [])
        self.assertEqual(result.hardware.com_ports[0].port, 2326)
        self.assertEqual(self._applied(), [])

    def test_changes_adapters_and_pipes(self) -> None:
        """Apply adapters and host pipes in one call."""
        result = self.service.set_vm_settings("first", ["nic2=left", "nic4=right", "com2=server:link"])
        self.assertEqual(result.hardware.nics[1], NicSetting("intnet", "left"))
        self.assertEqual(result.hardware.nics[3], NicSetting("intnet", "right"))
        self.assertEqual(result.hardware.com_ports[1], ComPortSetting("server", path=host_pipe_path("link")))
        self.assertEqual(len(self._applied()), 1)
        self.assertEqual(result.warnings, ())

    def test_requires_a_stopped_vm(self) -> None:
        """Refuse changes on a running or paused VM."""
        for state in ("running", "paused"):
            self.client.infos["first"] = replace(self.client.infos["first"], vm_state=state)
            with self.subTest(state=state), self.assertRaisesRegex(PySnapError, "must be stopped"):
                self.service.set_vm_settings("first", ["nic2=left"])
        self.assertEqual(self._applied(), [])

    def test_validates_tcp_ports(self) -> None:
        """Reject ports of other VMs, of other COM ports and busy host ports."""
        cases = {
            ("com2=2400",): 'used by virtual machine "second"',
            ("com2=2326",): "another COM port of this VM",
            ("com2=3000", "com3=3000"): "another COM port of this VM",
        }
        for options, message in cases.items():
            with self.subTest(options=options), self.assertRaisesRegex(PySnapError, message):
                self.service.set_vm_settings("first", list(options))
        self.busy_host_ports.add(3001)
        with self.assertRaisesRegex(PySnapError, "busy on this computer"):
            self.service.set_vm_settings("first", ["com2=3001"])
        self.assertEqual(self._applied(), [])

    def test_keeps_its_own_port_and_moves_ports_between_com_ports(self) -> None:
        """Allow the VM's own port even if the host reports it busy."""
        self.busy_host_ports.add(2326)
        result = self.service.set_vm_settings("first", ["com1=off", "com2=2326"])
        self.assertEqual(result.hardware.com_ports[1].port, 2326)

    def test_auto_keeps_or_allocates_ports(self) -> None:
        """Keep an existing TCP server and choose free ports for new ones."""
        result = self.service.set_vm_settings("first", ["com1=auto", "com2=auto", "com3=auto"])
        ports = [setting.port for setting in result.hardware.com_ports[:3]]
        self.assertEqual(ports[0], 2326)
        self.assertEqual(ports[1:], [2401, 2402])

    def test_warns_when_a_tcp_server_is_removed(self) -> None:
        """Warn for every removed TCP server and name the connect effect on COM1."""
        self.client.add_vm("third", com=[ComPortSetting("tcpserver", port=2500),
                                         ComPortSetting("tcpserver", port=2501)])
        result = self.service.set_vm_settings("third", ["com1=off", "com2=client:link"])
        self.assertEqual(result.warnings, (
            "COM1 no longer serves TCP port 2500; pysnap connect uses COM1 and cannot reach this VM.",
            "COM2 no longer serves TCP port 2501.",
        ))

    def test_rebuilds_the_proto_settings_sku(self) -> None:
        """Keep the DMI SKU of proto-settings clones in line with the settings."""
        self.client.extra_data["first"] = {SKU_KEY: "port2326.left.right"}
        self.service.set_vm_settings("first", ["com1=2600", "nic2=off", "nic3=deep"])
        self.assertEqual(self.client.extra_data["first"][SKU_KEY], "port2600..deep")

        self.service.set_vm_settings("first", ["com1=off", "nic3=off", "nic4=edge"])
        self.assertEqual(self.client.extra_data["first"][SKU_KEY], "port2600...edge")

        self.service.set_vm_settings("first", ["nic4=off"])
        self.assertEqual(self.client.extra_data["first"][SKU_KEY], "port2600")

    def test_leaves_vms_without_sku_alone(self) -> None:
        """Do not add a DMI SKU to ordinary VMs."""
        self.service.set_vm_settings("first", ["nic2=left"])
        self.assertNotIn(("set_dmi_system_sku",), [call[:1] for call in self.client.calls])

    def test_allocator_avoids_ports_of_every_com_port(self) -> None:
        """Let automatic allocation skip TCP servers on COM2-COM4 too."""
        self.client.add_vm("fresh")
        result = self.service.set_vm_settings("fresh", ["com1=auto"])
        self.assertEqual(result.hardware.com_ports[0].port, 2401)


class SetCommandTests(unittest.TestCase):
    """Verify the command line."""

    def test_prints_settings_and_warnings(self) -> None:
        """Show the resulting table and warnings on stderr."""
        client = SettingsClient()
        client.add_vm("first", com=[ComPortSetting("tcpserver", port=2326)], nics=[NicSetting("nat")])
        with tempfile.TemporaryDirectory() as temp_dir:
            service = PySnapService(
                client=client,
                proto_settings_store=ProtoSettingsStore(path=Path(temp_dir) / ".ptotosettings"),
            )
            output, errors = io.StringIO(), io.StringIO()
            code = run_cli(["set", "first", "com1=off", "com2=server:link", "nic2=left"],
                           service=service, stdout=output, stderr=errors)

        self.assertEqual(code, 0)
        self.assertIn("Warning: COM1 no longer serves TCP port 2326", errors.getvalue())
        self.assertEqual(output.getvalue().splitlines(), [
            "first (poweroff)",
            "  COM1  off",
            f'  COM2  pipe server, creates "link" ({host_pipe_path("link")})',
            "  COM3  off",
            "  COM4  off",
            "  NIC1  NAT",
            '  NIC2  internal network "left"',
            "  NIC3  off",
            "  NIC4  off",
        ])

    def test_reports_invalid_options(self) -> None:
        """Fail with the parser message."""
        client = SettingsClient()
        client.add_vm("first")
        output, errors = io.StringIO(), io.StringIO()
        code = run_cli(["set", "first", "com9=auto"], service=PySnapService(client=client),
                       stdout=output, stderr=errors)
        self.assertEqual(code, 1)
        self.assertIn("Error: Invalid option", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
