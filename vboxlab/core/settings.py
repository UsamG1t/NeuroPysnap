"""Parse ``pysnap set`` options for serial ports and network adapters.

Each option is ``KEY=VALUE``:

- ``comN``: ``auto`` or a TCP port number for a TCP server, ``server:NAME``
  or ``client:NAME`` for a host pipe between VMs, or ``off``;
- ``nicN``: an internal network name, ``nat`` or ``off``;
- ``access``: ``com``, ``ssh`` or ``auto`` (the choice by protosettings);
- ``ssh-user``: the guest user for ssh connections.

``N`` is any port or adapter number; whether the VM has it (VirtualBox
offers COM1-COM4 and, depending on the chipset, NIC1-NIC8 or NIC1-NIC36) is
checked against the VM.

A host pipe is given by a short name; :func:`host_pipe_path` turns it into
the path format of the host system, so two VMs that use the same name are
connected on any platform.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Sequence

from vboxlab.core.access import validate_access_mode, validate_ssh_user
from vboxlab.core.models import ComPortSetting, NicSetting
from vboxlab.errors import PySnapError

PIPE_PREFIX = "pysnap-"
WINDOWS_PIPE_ROOT = "\\\\.\\pipe\\"

_KEY_PATTERN = re.compile(r"^(?P<kind>com|nic)(?P<number>[1-9][0-9]*)$")
_PIPE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ComPortRequest:
    """Represent a requested serial port change.

    :param mode: ``"off"``, ``"tcpserver"``, ``"server"`` or ``"client"``.
    :param port: Requested TCP port; ``None`` with ``tcpserver`` means
        ``auto``.
    :param pipe_name: Short host pipe name of ``server`` and ``client``.
    """

    mode: str
    port: int | None = None
    pipe_name: str | None = None


@dataclass(frozen=True)
class SetRequest:
    """Represent the changes requested by ``pysnap set``.

    :param com_ports: Port numbers mapped to the requested change.
    :param nics: Adapter numbers mapped to the requested setting.
    :param access: ``"com"``, ``"ssh"`` or ``"auto"``, if requested.
    :param ssh_user: Guest user for ssh, if requested.
    """

    com_ports: dict[int, ComPortRequest]
    nics: dict[int, NicSetting]
    access: str | None = None
    ssh_user: str | None = None

    @property
    def empty(self) -> bool:
        """Return whether no change was requested."""
        return not self.hardware_changes and self.access is None and self.ssh_user is None

    @property
    def hardware_changes(self) -> bool:
        """Return whether serial ports or adapters change (the VM must be stopped)."""
        return bool(self.com_ports or self.nics)


def parse_set_options(options: Sequence[str]) -> SetRequest:
    """Parse ``KEY=VALUE`` options.

    :param options: Command line options.
    :returns: Requested changes.
    :raises PySnapError: For unknown keys, repeated keys and invalid values.
    """
    com_ports: dict[int, ComPortRequest] = {}
    nics: dict[int, NicSetting] = {}
    settings: dict[str, str] = {}
    for option in options:
        key, separator, value = option.partition("=")
        name = key.strip().lower()
        if separator and name in {"access", "ssh-user"}:
            if name in settings:
                raise PySnapError(f"{name} is given more than once.")
            value = value.strip()
            settings[name] = validate_access_mode(value) if name == "access" else validate_ssh_user(value)
            continue
        match = _KEY_PATTERN.match(name)
        if not separator or match is None:
            raise PySnapError(
                f'Invalid option "{option}"; use comN=VALUE, nicN=VALUE, access=VALUE '
                "or ssh-user=NAME."
            )
        number = int(match["number"])
        value = value.strip()
        if match["kind"] == "com":
            if number in com_ports:
                raise PySnapError(f"COM{number} is given more than once.")
            com_ports[number] = _parse_com_value(number, value)
        else:
            if number in nics:
                raise PySnapError(f"NIC{number} is given more than once.")
            nics[number] = _parse_nic_value(number, value)
    return SetRequest(
        com_ports=com_ports,
        nics=nics,
        access=settings.get("access"),
        ssh_user=settings.get("ssh-user"),
    )


def host_pipe_path(name: str, *, windows: bool | None = None) -> str:
    """Return the host pipe path for a short pipe name.

    :param name: Short pipe name.
    :param windows: Override the host system check, for tests.
    :returns: ``\\\\.\\pipe\\pysnap-NAME`` on Windows, otherwise a socket path
        ``pysnap-NAME`` in the temporary directory.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return f"{WINDOWS_PIPE_ROOT}{PIPE_PREFIX}{name}"
    return str(Path(tempfile.gettempdir()) / f"{PIPE_PREFIX}{name}")


def host_pipe_name(path: str) -> str | None:
    """Return the short name of a pipe path created by PySnap.

    :param path: Host pipe path.
    :returns: Short name, or ``None`` for other paths.
    """
    base = path.rsplit("\\", 1)[-1] if path.startswith(WINDOWS_PIPE_ROOT) else Path(path).name
    if base.startswith(PIPE_PREFIX) and _PIPE_NAME_PATTERN.match(base[len(PIPE_PREFIX):]):
        return base[len(PIPE_PREFIX):]
    return None


def com_setting_from_request(request: ComPortRequest, port: int | None) -> ComPortSetting:
    """Return the resulting port setting of a request.

    :param request: Requested change.
    :param port: Resolved TCP port of a ``tcpserver`` request.
    :returns: Port setting.
    """
    if request.mode == "tcpserver":
        return ComPortSetting(mode="tcpserver", port=port)
    if request.mode in {"server", "client"}:
        return ComPortSetting(mode=request.mode, path=host_pipe_path(request.pipe_name or ""))
    return ComPortSetting(mode="off")


def _parse_com_value(number: int, value: str) -> ComPortRequest:
    """Parse the value of a ``comN`` option."""
    lowered = value.lower()
    if lowered == "off":
        return ComPortRequest(mode="off")
    if lowered == "auto":
        return ComPortRequest(mode="tcpserver")
    if value.isdigit():
        port = int(value)
        if not 1 <= port <= 65535:
            raise PySnapError(f"COM{number}: the TCP port must be between 1 and 65535.")
        return ComPortRequest(mode="tcpserver", port=port)
    mode, separator, name = value.partition(":")
    if separator and mode.lower() in {"server", "client"}:
        if not _PIPE_NAME_PATTERN.match(name):
            raise PySnapError(
                f'COM{number}: invalid pipe name "{name}"; use letters, digits, ".", "_" or "-".'
            )
        return ComPortRequest(mode=mode.lower(), pipe_name=name)
    raise PySnapError(
        f'COM{number}: invalid value "{value}"; use auto, a TCP port, '
        "server:NAME, client:NAME or off."
    )


def _parse_nic_value(number: int, value: str) -> NicSetting:
    """Parse the value of a ``nicN`` option."""
    if not value or any(ord(character) < 32 for character in value):
        raise PySnapError(f"NIC{number}: give an internal network name, nat or off.")
    lowered = value.lower()
    if lowered == "off":
        return NicSetting(attachment="none")
    if lowered == "nat":
        return NicSetting(attachment="nat")
    return NicSetting(attachment="intnet", network=value)
