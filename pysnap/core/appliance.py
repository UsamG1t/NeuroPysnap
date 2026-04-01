"""Helpers for reading appliance metadata from OVF and OVA files."""

from __future__ import annotations

from pathlib import Path
import tarfile
from xml.etree import ElementTree

from pysnap.errors import PySnapError

OVF_NAMESPACE = {"ovf": "http://schemas.dmtf.org/ovf/envelope/1"}
OVF_ID_ATTRIBUTE = "{http://schemas.dmtf.org/ovf/envelope/1}id"


def read_appliance_vm_names(image_path: str | Path) -> tuple[str, ...]:
    """Read original VM names declared inside an OVF or OVA appliance.

    :param image_path: Appliance path.
    :returns: Declared appliance VM names in descriptor order.
    :raises PySnapError: If the descriptor cannot be read or parsed.
    """
    path = Path(image_path)
    descriptor = _read_ovf_descriptor(path)
    return _parse_ovf_vm_names(descriptor, path)


def _read_ovf_descriptor(path: Path) -> bytes:
    """Read the OVF descriptor bytes from an OVF or OVA path.

    :param path: Appliance path.
    :returns: Raw OVF descriptor bytes.
    :raises PySnapError: If no OVF descriptor can be found.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".ovf":
            return path.read_bytes()
        if suffix == ".ova":
            with tarfile.open(path, "r:*") as archive:
                member = next(
                    (item for item in archive.getmembers() if item.name.lower().endswith(".ovf")),
                    None,
                )
                if member is None:
                    raise PySnapError(f'Appliance "{path}" does not contain an OVF descriptor.')
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise PySnapError(
                        f'Unable to read OVF descriptor "{member.name}" from "{path}".'
                    )
                return extracted.read()
    except (OSError, tarfile.TarError) as error:
        raise PySnapError(f'Unable to read appliance descriptor from "{path}": {error}') from error

    raise PySnapError(f'Unsupported appliance format for "{path}".')


def _parse_ovf_vm_names(descriptor: bytes, path: Path) -> tuple[str, ...]:
    """Parse original VM names from raw OVF descriptor bytes.

    :param descriptor: Raw OVF descriptor bytes.
    :param path: Appliance path used for diagnostics.
    :returns: Declared appliance VM names.
    :raises PySnapError: If the descriptor does not expose any VM names.
    """
    try:
        root = ElementTree.fromstring(descriptor)
    except ElementTree.ParseError as error:
        raise PySnapError(f'Unable to parse OVF descriptor from "{path}": {error}') from error

    vm_names: list[str] = []
    seen: set[str] = set()
    for virtual_system in root.findall(".//ovf:VirtualSystem", OVF_NAMESPACE):
        vm_name = (virtual_system.get(OVF_ID_ATTRIBUTE) or "").strip()
        if not vm_name:
            name_node = virtual_system.find("ovf:Name", OVF_NAMESPACE)
            vm_name = ((name_node.text or "") if name_node is not None else "").strip()
        if not vm_name or vm_name in seen:
            continue
        vm_names.append(vm_name)
        seen.add(vm_name)

    if not vm_names:
        raise PySnapError(f'Unable to determine VM names declared by "{path}".')
    return tuple(vm_names)
