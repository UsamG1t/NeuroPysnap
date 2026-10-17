"""Domain models used by PySnap."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pysnap.core.access import AccessInfo


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
    ssh_port: int | None = None

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
    access_mode: str = "com"
    ssh_port: int | None = None


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

    :param com_ports: Settings of every serial port the VM can have (COM1-COM4).
    :param nics: Settings of every network adapter the VM can have; their
        number depends on the chipset (8 for PIIX3, 36 for ICH9).
    """

    com_ports: tuple[ComPortSetting, ...]
    nics: tuple[NicSetting, ...]


@dataclass(frozen=True)
class SshKeyResult:
    """Represent the outcome of ``pysnap ssh-key``.

    :param vm_name: VM name.
    :param users: Guest users whose login with the PySnap key works.
    :param ssh_user: User that PySnap uses for ssh after the setup.
    :param changed_user: Whether ``ssh_user`` was switched to make ssh work.
    :param already_installed: Whether the key worked before anything was done.
    """

    vm_name: str
    users: tuple[str, ...]
    ssh_user: str
    changed_user: bool = False
    already_installed: bool = False


@dataclass(frozen=True)
class VMSettingsResult:
    """Represent the outcome of ``pysnap set``.

    :param vm_name: VM name.
    :param vm_state: Raw VirtualBox state.
    :param hardware: Serial ports and network adapters after the change.
    :param warnings: Notes about removed TCP servers and similar effects.
    :param access: How PySnap reaches the guest after the change.
    """

    vm_name: str
    vm_state: str | None
    hardware: VMHardware
    warnings: tuple[str, ...] = ()
    access: AccessInfo | None = None
