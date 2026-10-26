"""How VBoxLab reaches the guest of a VM: serial console or ssh.

The access is a property of the VM, kept in its VirtualBox extra data: COM1
(the serial TCP server of COM1) or ssh (a rule on the NAT adapter forwards a
host port on ``127.0.0.1`` to port 22 of the guest). ``vboxlab import`` sets
it, clones copy it from their base VM and ``vboxlab set VM access=com|ssh``
changes it. A VM without it, for example after a clean import, counts as an
ssh VM without a forwarded port.

Lab VMs (``vboxlab/lab``) are reached through COM1 and give their clones the
lab DMI system information. The ssh user is kept in extra data as well; the
forwarded port is read from the NAT port-forwarding rule named
:data:`SSH_RULE_NAME`.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from vboxlab.core.models import VMInfo
from vboxlab.errors import VBoxLabError

ACCESS_KEY = "vboxlab/access"
LAB_KEY = "vboxlab/lab"
SSH_USER_KEY = "vboxlab/ssh-user"
DEFAULT_SSH_USER = "root"
SSH_RULE_NAME = "vboxlab-ssh"
SSH_HOST_ADDRESS = "127.0.0.1"
GUEST_SSH_PORT = 22
ACCESS_MODES = ("com", "ssh")

_SSH_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


@dataclass(frozen=True)
class AccessInfo:
    """Describe how VBoxLab reaches the guest of one VM.

    :param mode: ``"com"`` for the COM1 serial console, ``"ssh"`` for ssh.
    :param configured: Whether the VM stores its access; ``False`` means the
        ssh default of a VM imported without settings.
    :param lab: Whether the VM is a lab VM whose clones get lab settings.
    :param serial_port: TCP port of COM1, if any.
    :param ssh_port: Host port forwarded to the guest ssh server, if any.
    :param ssh_user: User for ssh connections.
    """

    mode: str
    configured: bool
    lab: bool
    serial_port: int | None
    ssh_port: int | None
    ssh_user: str


def resolve_access(info: VMInfo) -> AccessInfo:
    """Return the access mode and connection details of a VM.

    :param info: VM information including VBoxLab metadata.
    :returns: Access description.
    """
    stored = info.metadata.get(ACCESS_KEY, "").strip().lower()
    configured = stored in ACCESS_MODES
    return AccessInfo(
        mode=stored if configured else "ssh",
        configured=configured,
        lab=info.metadata.get(LAB_KEY) == "true",
        serial_port=info.serial_port,
        ssh_port=info.ssh_port,
        ssh_user=info.metadata.get(SSH_USER_KEY) or DEFAULT_SSH_USER,
    )


def validate_access_mode(value: str) -> str:
    """Validate an ``access=`` value.

    :param value: ``com`` or ``ssh``.
    :returns: The lower-case value.
    :raises VBoxLabError: For other values.
    """
    lowered = value.strip().lower()
    if lowered not in ACCESS_MODES:
        raise VBoxLabError(f'access: invalid value "{value}"; use com or ssh.')
    return lowered


def validate_lab_flag(value: str) -> bool:
    """Validate a ``lab=`` value.

    :param value: ``yes`` or ``no``.
    :returns: Whether the VM is a lab VM.
    :raises VBoxLabError: For other values.
    """
    lowered = value.strip().lower()
    if lowered not in ("yes", "no"):
        raise VBoxLabError(f'lab: invalid value "{value}"; use yes or no.')
    return lowered == "yes"


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
