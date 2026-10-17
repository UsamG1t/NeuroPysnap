"""Run the system ``ssh`` client against a forwarded guest port.

Every call uses the PySnap key, the known_hosts file of the VM with
``StrictHostKeyChecking=accept-new`` (the first key is trusted, a changed key
is refused) and no user configuration (``-F`` with the null device), so the
behaviour does not depend on ``~/.ssh``. Non-interactive calls run in batch
mode: they never ask for a password.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import shlex
import subprocess
from typing import Sequence

from pysnap.core.access import SSH_HOST_ADDRESS
from pysnap.errors import PySnapError
from pysnap.ssh.keys import SshKeyStore, find_executable

CONNECT_TIMEOUT = 10


@dataclass(frozen=True)
class SshTarget:
    """Describe where and as whom to connect.

    :param vm_name: VM name, used in messages.
    :param vm_uuid: VM UUID, selects the known_hosts file.
    :param port: Host port forwarded to the guest ssh server.
    :param user: Guest user.
    """

    vm_name: str
    vm_uuid: str
    port: int
    user: str


@dataclass(frozen=True)
class AccessCheck:
    """Result of a key login attempt.

    :param status: ``"ok"``, ``"denied"`` (the server refused the key) or
        ``"unreachable"`` (no ssh server answered).
    :param message: Error output of ssh.
    """

    status: str
    message: str = ""


def ssh_command(
    target: SshTarget,
    store: SshKeyStore,
    *,
    batch: bool = True,
    tty: bool = False,
    remote_command: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """Build an ``ssh`` command line.

    :param target: Connection target.
    :param store: PySnap key store.
    :param batch: Use the PySnap key only and never ask for a password.
    :param tty: Request a terminal on the guest (``-tt``).
    :param remote_command: Command for the guest shell; a login shell without.
    :param extra: Further ssh options placed before the destination.
    :returns: Argument list.
    """
    arguments = [
        find_executable("ssh"),
        "-F", os.devnull,
        "-p", str(target.port),
        "-o", f"UserKnownHostsFile={store.known_hosts(target.vm_uuid)}",
        "-o", f"GlobalKnownHostsFile={os.devnull}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=15",
        "-o", "LogLevel=ERROR",
    ]
    if batch:
        arguments += [
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-i", str(store.ensure_key()),
        ]
    else:
        arguments += ["-o", "PubkeyAuthentication=no"]
    if tty:
        arguments.append("-tt")
    arguments += list(extra)
    arguments.append(f"{target.user}@{SSH_HOST_ADDRESS}")
    if remote_command is not None:
        arguments.append(remote_command)
    return arguments


def check_key_access(target: SshTarget, store: SshKeyStore) -> AccessCheck:
    """Try to log in with the PySnap key.

    :param target: Connection target.
    :param store: PySnap key store.
    :returns: Whether the key works, is refused or no server answers.
    """
    result = subprocess.run(
        ssh_command(target, store, remote_command="true"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CONNECT_TIMEOUT * 3,
    )
    message = result.stderr.strip()
    if result.returncode == 0:
        return AccessCheck("ok")
    if "Permission denied" in message or "Too many authentication failures" in message:
        return AccessCheck("denied", message)
    return AccessCheck("unreachable", message)


def run_remote(
    target: SshTarget,
    store: SshKeyStore,
    command: str,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run one command on the guest with the PySnap key.

    :param target: Connection target.
    :param store: PySnap key store.
    :param command: Shell command line for the guest.
    :param timeout: Seconds to wait.
    :returns: Completed process with bytes output.
    """
    return subprocess.run(
        ssh_command(target, store, remote_command=command),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )


async def open_terminal(
    target: SshTarget,
    store: SshKeyStore,
    *,
    columns: int,
    rows: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.subprocess.Process]:
    """Start an interactive guest shell on a pseudo terminal.

    The guest terminal gets the given size first; output of ssh itself
    (errors) is merged into the stream.

    :param target: Connection target.
    :param store: PySnap key store.
    :param columns: Terminal width.
    :param rows: Terminal height.
    :returns: Reader, writer and the ssh process.
    """
    remote = f"stty cols {int(columns)} rows {int(rows)} 2>/dev/null; exec \"${{SHELL:-/bin/sh}}\" -l"
    process = await asyncio.create_subprocess_exec(
        *ssh_command(target, store, tty=True, remote_command=remote),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None and process.stdin is not None
    return process.stdout, process.stdin, process


def install_script(public_key: str, *, with_root: bool) -> str:
    """Return the guest shell script that adds the PySnap key.

    The key is appended to ``~/.ssh/authorized_keys`` unless it is there
    already. With ``with_root``, a user other than root then runs the same
    for root through ``su``; the guest asks for the root password, and an
    empty or wrong one only skips root.

    :param public_key: Public key line.
    :param with_root: Also install the key for root.
    :returns: Script for ``sh -c``.
    """
    add = (
        "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"(grep -qxF {shlex.quote(public_key)} ~/.ssh/authorized_keys || "
        f"printf '%s\\n' {shlex.quote(public_key)} >> ~/.ssh/authorized_keys)"
    )
    if not with_root:
        return add
    return (
        f"{add} && echo 'PySnap: the key is installed for '\"$(id -un)\". && "
        "if [ \"$(id -u)\" != 0 ]; then "
        "echo 'PySnap: enter the root password to install the key for root too "
        "(press Enter to skip).'; "
        f"su - root -c {shlex.quote(add)} && echo 'PySnap: the key is installed for root.' "
        "|| echo 'PySnap: the key is not installed for root.'; fi"
    )


def install_key_interactively(target: SshTarget, store: SshKeyStore) -> int:
    """Log in with a password in the current terminal and add the key.

    ssh asks for the password of ``target.user``; the guest may ask for the
    root password. Nothing is captured, the user sees the whole dialogue.

    :param target: Connection target with the password login user.
    :param store: PySnap key store.
    :returns: Exit status of ssh.
    """
    script = install_script(store.public_key_text(), with_root=True)
    return subprocess.run(
        ssh_command(target, store, batch=False, tty=True, remote_command=f"sh -c {shlex.quote(script)}"),
    ).returncode


def check_or_raise(target: SshTarget, store: SshKeyStore) -> None:
    """Raise a helpful error unless the PySnap key logs in.

    :param target: Connection target.
    :param store: PySnap key store.
    :raises PySnapError: When the key is refused or no server answers.
    """
    check = check_key_access(target, store)
    if check.status == "denied":
        raise PySnapError(
            f'The ssh server of "{target.vm_name}" refused the PySnap key for '
            f"{target.user}; run pysnap ssh-key {target.vm_name}."
        )
    if check.status == "unreachable":
        raise PySnapError(
            f'No ssh server answered on port {target.port} of "{target.vm_name}": '
            f"{check.message or 'connection failed'}."
        )
