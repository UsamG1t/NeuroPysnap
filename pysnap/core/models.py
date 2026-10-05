"""Domain models used by PySnap."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VMReference:
    """Represent a lightweight VirtualBox VM reference."""

    name: str
    uuid: str


@dataclass(frozen=True)
class SerialPortConfiguration:
    """Represent the raw ``UART1`` configuration of one VM."""

    enabled: bool
    mode: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class ImportCandidate:
    """Represent a VM discovered in an appliance import dry run."""

    vsys_index: int
    vm_name: str
    group: str
    requires_eula_accept: bool = False


@dataclass(frozen=True)
class VMInfo:
    """Represent the VM information required by the CLI."""

    name: str
    uuid: str
    groups: tuple[str, ...]
    serial_port: int | None = None
    vm_state: str | None = None
    parent_name: str | None = None
    managed: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    serial_tcp_ports: tuple[int, ...] = ()

    @property
    def primary_group(self) -> str:
        """Return the primary VM group.

        :returns: Primary group name or ``/Others`` when unavailable.
        """
        return self.groups[0] if self.groups else "/Others"


@dataclass(frozen=True)
class VMGroup:
    """Represent a VM group and its members."""

    name: str
    vm_names: tuple[str, ...]


@dataclass(frozen=True)
class IntegrationTestResult:
    """Represent the outcome of an integration test run."""

    machines: tuple[VMInfo, ...]
    deleted_vm_names: tuple[str, ...]
    monitor_records: tuple[VMMonitorRecord, ...] = ()


@dataclass(frozen=True)
class VMMonitorRecord:
    """Represent a compact monitor record for a VM."""

    name: str
    display_state: str
    serial_port: int | None
    group: str
    raw_state: str


@dataclass(frozen=True)
class ComPortSetting:
    """Represent the configuration of one serial (COM) port.

    :param mode: ``"off"``, ``"tcpserver"``, ``"server"`` or ``"client"``
        (host pipe), or another raw VirtualBox mode such as ``"file"``.
    :param port: TCP port of a ``tcpserver`` port.
    :param path: Host pipe path of a ``server`` or ``client`` port.
    :param detail: Raw VirtualBox value of other modes.
    """

    mode: str
    port: int | None = None
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class NicSetting:
    """Represent the attachment of one network adapter.

    :param attachment: ``"none"``, ``"nat"`` or ``"intnet"``, or another raw
        VirtualBox attachment such as ``"bridged"``.
    :param network: Internal network name of an ``intnet`` adapter.
    """

    attachment: str
    network: str | None = None


@dataclass(frozen=True)
class VMHardware:
    """Represent the serial ports and network adapters of one VM.

    :param com_ports: Settings of COM1-COM4.
    :param nics: Settings of NIC1-NIC4.
    """

    com_ports: tuple[ComPortSetting, ...]
    nics: tuple[NicSetting, ...]


@dataclass(frozen=True)
class VMSettingsResult:
    """Represent the outcome of ``pysnap set``.

    :param vm_name: VM name.
    :param vm_state: Raw VirtualBox state.
    :param hardware: Serial ports and network adapters after the change.
    :param warnings: Notes about removed TCP servers and similar effects.
    """

    vm_name: str
    vm_state: str | None
    hardware: VMHardware
    warnings: tuple[str, ...] = ()
