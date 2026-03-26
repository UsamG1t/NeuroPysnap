"""Output formatters used by the PySnap CLI."""

from __future__ import annotations

from pysnap.core.models import (
    IntegrationTestResult,
    VMGroup,
    VMInfo,
    VMMonitorRecord,
)


def format_groups(groups: list[VMGroup]) -> str:
    """Format a group listing for terminal output.

    :param groups: Group objects to format.
    :returns: Human-readable text output.
    """
    if not groups:
        return "No virtual machines found."

    lines: list[str] = []
    for group in groups:
        lines.append(f"Group: {group.name}")
        for vm_name in group.vm_names:
            lines.append(f"- {vm_name}")
    return "\n".join(lines)


def format_vm_info(vm_info: VMInfo) -> str:
    """Format one VM description for terminal output.

    :param vm_info: VM information to format.
    :returns: Human-readable text output.
    """
    serial_value = str(vm_info.serial_port) if vm_info.serial_port is not None else "none"
    lines = [
        f"Name: {vm_info.name}",
        f"Group: {', '.join(vm_info.groups) if vm_info.groups else '/Others'}",
        f"Serial port: {serial_value}",
    ]
    if vm_info.parent_name:
        lines.append(f"Parent VM: {vm_info.parent_name}")
    return "\n".join(lines)


def format_import_result(imported_vms: list[VMInfo]) -> str:
    """Format the result of an appliance import.

    :param imported_vms: Imported VM information objects.
    :returns: Human-readable text output.
    """
    lines = ["Imported virtual machines:"]
    for vm_info in imported_vms:
        lines.append(f"- {vm_info.name} ({vm_info.primary_group})")
    return "\n".join(lines)


def format_integration_test_result(result: IntegrationTestResult) -> str:
    """Format the result of a completed integration test run.

    :param result: Integration test result to render.
    :returns: Human-readable text output.
    """
    lines = ["Integration test completed successfully.", "", "Machines:"]
    for vm_info in result.machines:
        lines.append(format_vm_info(vm_info))
        lines.append("")
    lines.append("Deletion order:")
    for vm_name in result.deleted_vm_names:
        lines.append(f"- {vm_name}")
    return "\n".join(lines).rstrip()


def format_monitor_records(records: list[VMMonitorRecord]) -> str:
    """Format compact runtime records for the ``monitor`` command.

    :param records: Monitor records to format.
    :returns: Human-readable text output.
    """
    if not records:
        return "No active virtual machines found."
    return "\n".join(
        f"{record.name} (state: {record.display_state} ; "
        f"{record.serial_port if record.serial_port is not None else 'none'} ; "
        f"{record.group})"
        for record in records
    )
