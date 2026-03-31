"""Parsers for ``VBoxManage`` command output."""

from __future__ import annotations

import re

from pysnap.core.models import ImportCandidate, VMReference

VM_REFERENCE_PATTERN = re.compile(r'^"(?P<name>.*)"\s+\{(?P<uuid>[^}]+)\}$')
EXTRA_DATA_PATTERN = re.compile(r"^Key:\s*(?P<key>.+?),\s*Value:\s*(?P<value>.*)$")
IMPORT_VSYS_PATTERN = re.compile(r"^\s*Virtual system (?P<index>\d+):\s*$")
IMPORT_NAME_PATTERN = re.compile(r'^\s*\d+:\s+Suggested VM name "(?P<name>.*)"\s*$')
IMPORT_GROUP_PATTERN = re.compile(r'^\s*\d+:\s+Suggested VM group "(?P<group>.*)"\s*$')
IMPORT_EULA_PATTERN = re.compile(r"^\s*\d+:\s+End-user license agreement\s*$")


def parse_list_vms(output: str) -> list[VMReference]:
    """Parse ``VBoxManage list vms`` output.

    :param output: Raw command output.
    :returns: Parsed VM references.
    """
    references: list[VMReference] = []
    for line in output.splitlines():
        match = VM_REFERENCE_PATTERN.match(line.strip())
        if match is None:
            continue
        references.append(
            VMReference(name=match.group("name"), uuid=match.group("uuid"))
        )
    return references


def parse_machine_readable(output: str) -> dict[str, str]:
    """Parse ``VBoxManage --machinereadable`` output.

    :param output: Raw command output.
    :returns: Mapping of property names to values.
    """
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = _unquote(value.strip())
    return properties


def parse_extra_data(output: str) -> dict[str, str]:
    """Parse ``VBoxManage getextradata ... enumerate`` output.

    :param output: Raw command output.
    :returns: Mapping of extra data keys to values.
    """
    if "No extra data items configured" in output:
        return {}

    extra_data: dict[str, str] = {}
    for line in output.splitlines():
        match = EXTRA_DATA_PATTERN.match(line.strip())
        if match is None:
            continue
        extra_data[match.group("key")] = match.group("value")
    return extra_data


def parse_snapshot_names(properties: dict[str, str]) -> list[str]:
    """Extract snapshot names from machine-readable VM properties.

    :param properties: Parsed machine-readable VM properties.
    :returns: Snapshot names ordered with the current snapshot first when possible.
    """
    snapshot_entries: list[tuple[tuple[int, ...], str]] = []
    for key, value in properties.items():
        if not key.startswith("SnapshotName"):
            continue
        suffix = key.removeprefix("SnapshotName")
        order = _snapshot_order(suffix)
        snapshot_entries.append((order, value))
    snapshot_entries.sort(key=lambda item: item[0])
    return [value for _, value in snapshot_entries]


def _snapshot_order(suffix: str) -> tuple[int, ...]:
    """Convert a VirtualBox snapshot suffix into a sortable numeric key.

    VirtualBox can emit snapshot keys like ``SnapshotName-1`` and nested forms
    like ``SnapshotName-1-1`` for descendant snapshots.

    :param suffix: Raw suffix following ``SnapshotName``.
    :returns: Sortable numeric key with the current snapshot first.
    """
    if not suffix:
        return (0,)

    numeric_parts = [part for part in suffix.split("-") if part]
    if numeric_parts and all(part.isdigit() for part in numeric_parts):
        return (1, *(int(part) for part in numeric_parts))

    return (2,)


def parse_import_candidates(output: str) -> list[ImportCandidate]:
    """Parse ``VBoxManage import --dry-run`` output.

    :param output: Raw dry-run output.
    :returns: Appliance VM candidates discovered in the dry run.
    """
    candidates: list[ImportCandidate] = []
    current_index: int | None = None
    current_name = ""
    current_group = "/"
    current_eula = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        vsys_match = IMPORT_VSYS_PATTERN.match(line)
        if vsys_match is not None:
            if current_index is not None and current_name:
                candidates.append(
                    ImportCandidate(
                        vsys_index=current_index,
                        vm_name=current_name,
                        group=current_group,
                        requires_eula_accept=current_eula,
                    )
                )
            current_index = int(vsys_match.group("index"))
            current_name = ""
            current_group = "/"
            current_eula = False
            continue

        if current_index is None:
            continue

        name_match = IMPORT_NAME_PATTERN.match(line)
        if name_match is not None:
            current_name = name_match.group("name")
            continue

        group_match = IMPORT_GROUP_PATTERN.match(line)
        if group_match is not None:
            current_group = group_match.group("group")
            continue

        if IMPORT_EULA_PATTERN.match(line) is not None:
            current_eula = True

    if current_index is not None and current_name:
        candidates.append(
            ImportCandidate(
                vsys_index=current_index,
                vm_name=current_name,
                group=current_group,
                requires_eula_accept=current_eula,
            )
        )

    return candidates


def split_groups(raw_groups: str) -> tuple[str, ...]:
    """Split a machine-readable group list.

    :param raw_groups: Raw ``groups`` property value.
    :returns: Tuple of group names.
    """
    if not raw_groups:
        return ()
    return tuple(group for group in raw_groups.split(",") if group)


def _unquote(value: str) -> str:
    """Remove machine-readable quoting from a value.

    :param value: Raw property value.
    :returns: Unquoted property value.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        body = value[1:-1]
        return body.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\n", "\n")
    return value
