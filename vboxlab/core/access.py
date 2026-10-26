"""How VBoxLab reaches the guest of a VM: serial console or ssh.

Base VMs registered with ``vboxlab protosettings`` and their clones are known
to have their console on COM1, so they are reached through the serial TCP
server of COM1. Every other VM is reached through ssh: its NAT adapter
forwards a host port on ``127.0.0.1`` to port 22 of the guest. The automatic
choice can be overridden per VM with ``vboxlab set VM access=com|ssh``.

The override and the ssh user are kept in VirtualBox extra data of the VM;
the forwarded port is read from the NAT port-forwarding rule named
:data:`SSH_RULE_NAME`.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Collection

from vboxlab.core.models import VMInfo
from vboxlab.errors import VBoxLabError

ACCESS_KEY = "pysnap/access"
SSH_USER_KEY = "pysnap/ssh-user"
DEFAULT_SSH_USER = "root"
SSH_RULE_NAME = "pysnap-ssh"
SSH_HOST_ADDRESS = "127.0.0.1"
GUEST_SSH_PORT = 22
ACCESS_MODES = ("com", "ssh")

_SSH_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


@dataclass(frozen=True)
class AccessInfo:
    """Describe how VBoxLab reaches the guest of one VM.

    :param mode: ``"com"`` for the COM1 serial console, ``"ssh"`` for ssh.
    :param source: Why this mode applies: ``"override"`` (``vboxlab set``),
        ``"protosettings"`` (the VM or its parent is registered) or
        ``"default"``.
    :param serial_port: TCP port of COM1, if any.
    :param ssh_port: Host port forwarded to the guest ssh server, if any.
    :param ssh_user: User for ssh connections.
    """

    mode: str
    source: str
    serial_port: int | None
    ssh_port: int | None
    ssh_user: str


def resolve_access(info: VMInfo, proto_settings_vms: Collection[str]) -> AccessInfo:
    """Return the access mode and connection details of a VM.

    :param info: VM information including VBoxLab metadata.
    :param proto_settings_vms: Base VM names registered with protosettings.
    :returns: Access description.
    """
    override = info.metadata.get(ACCESS_KEY, "").strip().lower()
    if override in ACCESS_MODES:
        mode, source = override, "override"
    elif info.name in proto_settings_vms or (
        info.parent_name is not None and info.parent_name in proto_settings_vms
    ):
        mode, source = "com", "protosettings"
    else:
        mode, source = "ssh", "default"
    return AccessInfo(
        mode=mode,
        source=source,
        serial_port=info.serial_port,
        ssh_port=info.ssh_port,
        ssh_user=info.metadata.get(SSH_USER_KEY) or DEFAULT_SSH_USER,
    )


def validate_access_mode(value: str) -> str:
    """Validate an ``access=`` value.

    :param value: ``com``, ``ssh`` or ``auto``.
    :returns: The lower-case value.
    :raises VBoxLabError: For other values.
    """
    lowered = value.strip().lower()
    if lowered not in (*ACCESS_MODES, "auto"):
        raise VBoxLabError(f'access: invalid value "{value}"; use com, ssh or auto.')
    return lowered


def validate_ssh_user(value: str) -> str:
    """Validate an ``ssh-user=`` value.

    :param value: User name in the guest.
    :returns: The user name.
    :raises VBoxLabError: For names that are not plain user names.
    """
    if not _SSH_USER_PATTERN.match(value):
        raise VBoxLabError(
            f'ssh-user: invalid user name "{value}"; use letters, digits, "_", "." or "-".'
        )
    return value


def ssh_forwarding_rule(port: int) -> str:
    """Return the VirtualBox NAT rule that forwards a host port to ssh.

    :param port: Host port on :data:`SSH_HOST_ADDRESS`.
    :returns: Rule in ``VBoxManage --natpfN`` syntax.
    """
    return f"{SSH_RULE_NAME},tcp,{SSH_HOST_ADDRESS},{port},,{GUEST_SSH_PORT}"


def parse_ssh_forwarding(rule: str) -> int | None:
    """Return the host port of a VBoxLab ssh forwarding rule.

    :param rule: Rule as printed by ``showvminfo --machinereadable``,
        ``name,protocol,host address,host port,guest address,guest port``.
    :returns: Host port, or ``None`` for other rules.
    """
    parts = [part.strip() for part in rule.split(",")]
    if len(parts) != 6 or parts[0] != SSH_RULE_NAME or parts[1].lower() != "tcp":
        return None
    return int(parts[3]) if parts[3].isdigit() else None
