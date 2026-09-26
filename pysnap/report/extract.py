"""Copy a report file from a running VM through its serial console.

The transfer uses only the guest shell and coreutils (decision D38):

1. The VM must be running, expose ``UART1`` as a TCP server and have no
   attached ``pysnap connect`` session, because VirtualBox serves one client.
2. PySnap wakes the console with Enter and refuses to continue when the
   prompt installed by ``report`` is visible: a recording is running and every
   further command would end up in the student's report.
3. ``echo PYSNAP_$((20+22))`` must answer ``PYSNAP_42``. The echoed command
   line cannot contain that text, so the answer proves a working shell and
   not a login screen or a full-screen program.
4. One command prints a begin marker, the file as one ``base64`` line, its
   ``sha256sum`` and an end marker. The markers are split in the command so
   that its echo never looks like a marker.
5. PySnap decodes the data, compares the checksum, writes the file
   atomically and clears the guest screen.

Service commands start with a space, so shells with ``HISTCONTROL`` set to
``ignorespace`` or ``ignoreboth`` keep them out of the history.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import tempfile
from time import monotonic
from typing import Callable

from pysnap.errors import PySnapError
from pysnap.terminal.transport import open_serial_socket

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_SHELL_TIMEOUT = 5.0
DEFAULT_IDLE_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_QUIET_PERIOD = 0.3

PROBE_COMMAND = b" echo PYSNAP_$((20+22))\r"
PROBE_ANSWER = b"PYSNAP_42"
_ANSI_SEQUENCE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b.")
# The prompt installed by ``report`` (``[\u@NN-HOST \W]# ``).
_REPORT_PROMPT = re.compile(rb"\[[^@\]\s]+@\d+-[^\s\]]+ [^\]]*\][#$] ?")

Connector = Callable[[str, int, float], socket.socket]


class ExtractError(PySnapError):
    """Report a file transfer from a VM that could not be completed."""


@dataclass(frozen=True)
class ExtractResult:
    """Describe a completed transfer.

    :param vm_name: Source VM.
    :param remote_path: Shell path of the file inside the VM.
    :param destination: File written on the host.
    :param size: Number of bytes transferred.
    :param sha256: Checksum confirmed by the guest and the host.
    """

    vm_name: str
    remote_path: str
    destination: Path
    size: int
    sha256: str


def extract_file(
    service,
    vm_name: str,
    name: str,
    *,
    destination: Path | None = None,
    force: bool = False,
    connector: Connector | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    shell_timeout: float = DEFAULT_SHELL_TIMEOUT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ExtractResult:
    """Copy one file from a running VM to the host.

    :param service: PySnap service used to inspect the VM and its sessions.
    :param vm_name: VM to copy from.
    :param name: File name in the root home directory, or a path with ``/``.
    :param destination: Host path; defaults to the file name in the current
        directory.
    :param force: Whether an existing destination may be replaced.
    :param connector: Opens the serial TCP socket; for tests.
    :param connect_timeout: Seconds to wait for the serial TCP port.
    :param shell_timeout: Seconds to wait for the shell check answer.
    :param idle_timeout: Seconds without data after which the transfer fails.
    :param max_bytes: Largest accepted file size.
    :returns: Transfer details.
    :raises ExtractError: When any precondition or the transfer fails.
    """
    remote_path = remote_shell_path(name)
    target = destination if destination is not None else Path.cwd() / Path(name).name
    if target.exists() and not force:
        raise ExtractError(f'"{target}" already exists; use --force to replace it.')
    port = _connectable_port(service, vm_name)

    open_socket = connector or (lambda host, number, timeout: open_serial_socket(host, number, timeout=timeout))
    connection = open_socket("localhost", port, connect_timeout)
    try:
        console = _Console(connection)
        greeting = console.exchange(b"\r", quiet=_QUIET_PERIOD, limit=2.0)
        if _REPORT_PROMPT.search(_plain(greeting)):
            raise ExtractError(
                f'A report recording is running on "{vm_name}". Finish it with exit or '
                "Ctrl-D in pysnap connect, then extract the report."
            )
        console.send(PROBE_COMMAND)
        if not console.read_until(lambda data: PROBE_ANSWER in data, shell_timeout):
            raise ExtractError(
                f'The console of "{vm_name}" is not at a shell prompt. Open it with '
                "pysnap connect, log in or leave the running program, detach with "
                "Ctrl-Q and try again."
            )
        console.exchange(b"", quiet=_QUIET_PERIOD, limit=1.0)
        data, checksum = _transfer(console, remote_path, idle_timeout, max_bytes)
        console.send(b" clear\r")
        console.exchange(b"", quiet=_QUIET_PERIOD, limit=1.0)
    finally:
        connection.close()

    _write_atomically(target, data)
    return ExtractResult(vm_name, remote_path, target, len(data), checksum)


def remote_shell_path(name: str) -> str:
    """Return the quoted shell path of a file in the VM (D38).

    :param name: File name, looked up in the home directory, or a path.
    :returns: Shell expression for the path.
    :raises ExtractError: For empty names or names with control characters.
    """
    if not name or any(ord(char) < 32 for char in name) or name.endswith("/"):
        raise ExtractError(f"Invalid file name {name!r}.")
    if "/" in name:
        return shlex.quote(name)
    return f'"$HOME"/{shlex.quote(name)}'


def _connectable_port(service, vm_name: str) -> int:
    """Check that the VM can serve a transfer and return its serial port."""
    vm_info = service.show_vm(vm_name)
    if vm_info.serial_port is None:
        raise ExtractError(
            f'Virtual machine "{vm_name}" has no TCP serial port; run pysnap plug first.'
        )
    if (vm_info.vm_state or "").lower() != "running":
        raise ExtractError(
            f'Virtual machine "{vm_name}" is not running; start it with pysnap connect.'
        )
    if service.session_registry.get_live_session(vm_name) is not None:
        raise ExtractError(
            f'Virtual machine "{vm_name}" has an attached pysnap connect session; '
            "detach with Ctrl-Q and try again."
        )
    return vm_info.serial_port


def _transfer(console: "_Console", remote_path: str, idle_timeout: float, max_bytes: int) -> tuple[bytes, str]:
    """Run the transfer command and return the verified file content."""
    token = secrets.token_hex(8)
    begin, end, missing = (f"PYSNAP-{kind}-{token}".encode() for kind in ("BEGIN", "END", "MISSING"))
    command = (
        f" f={remote_path}; if [ -f \"$f\" ] && [ -r \"$f\" ]; then "
        f"printf '%s-%s\\n' PYSNAP-BEGIN {token}; base64 -w0 \"$f\"; printf '\\n'; "
        f"sha256sum < \"$f\"; printf '%s-%s\\n' PYSNAP-END {token}; "
        f"else printf '%s-%s\\n' PYSNAP-MISSING {token}; fi\r"
    )
    console.send(command.encode())
    limit = max_bytes * 4 // 3 + 4096

    def complete(data: bytes) -> bool:
        return end in data or missing in data or len(data) > limit

    if not console.read_until(complete, idle_timeout, idle=True):
        raise ExtractError("The transfer stopped: no data arrived from the VM in time.")
    output = console.buffer.replace(b"\r", b"")
    if missing in output:
        raise ExtractError(f"The file {remote_path} was not found or is not readable in the VM.")
    if end not in output:
        raise ExtractError(f"The file is larger than {max_bytes} bytes.")
    body = output[output.index(begin + b"\n") + len(begin) + 1:output.index(end)]
    lines = body.split(b"\n")
    if len(lines) < 2:
        raise ExtractError("The VM sent an incomplete transfer.")
    try:
        data = base64.b64decode(lines[0], validate=True)
    except (binascii.Error, ValueError):
        raise ExtractError("The VM sent damaged data; try again.") from None
    expected = lines[1].split()[0].decode("ascii", errors="replace") if lines[1].split() else ""
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != expected:
        raise ExtractError("The checksum of the received file does not match; try again.")
    if len(data) > max_bytes:
        raise ExtractError(f"The file is larger than {max_bytes} bytes.")
    return data, checksum


def _plain(data: bytes) -> bytes:
    """Remove terminal control sequences from console output."""
    return _ANSI_SEQUENCE.sub(b"", data)


def _write_atomically(target: Path, data: bytes) -> None:
    """Write a file through a temporary file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class _Console:
    """Minimal blocking reader and writer for the serial TCP socket."""

    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.connection.settimeout(0.05)
        self.buffer = b""

    def send(self, data: bytes) -> None:
        """Send bytes and start a new response buffer."""
        self.buffer = b""
        if data:
            self.connection.sendall(data)

    def exchange(self, data: bytes, *, quiet: float, limit: float) -> bytes:
        """Send bytes and collect output until it stays quiet."""
        self.send(data)
        deadline = monotonic() + limit
        last_data = monotonic()
        while monotonic() < deadline and monotonic() - last_data < quiet:
            if self._receive():
                last_data = monotonic()
        return self.buffer

    def read_until(self, done: Callable[[bytes], bool], timeout: float, *, idle: bool = False) -> bool:
        """Collect output until ``done`` holds; ``idle`` restarts the timer on data."""
        deadline = monotonic() + timeout
        while not done(self.buffer):
            if monotonic() >= deadline:
                return False
            if self._receive() and idle:
                deadline = monotonic() + timeout
        return True

    def _receive(self) -> bool:
        """Read one chunk; return whether data arrived."""
        try:
            chunk = self.connection.recv(65536)
        except socket.timeout:
            return False
        except OSError as error:
            raise ExtractError(f"The serial connection failed: {error}") from None
        if not chunk:
            raise ExtractError("The VM closed the serial connection.")
        self.buffer += chunk
        return True
