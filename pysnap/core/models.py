"""Domain models used by PySnap."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VMReference:
    """Represent a lightweight VirtualBox VM reference."""

    name: str
    uuid: str


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
