"""The PySnap ssh key pair and per-VM ``known_hosts`` files.

Everything lives in ``~/.pysnap/ssh`` (mode 0700): the key pair
``pysnap_vm_access_ed25519`` with its ``.pub`` file, created once by
``ssh-keygen`` without a passphrase, and ``known_hosts.d/<VM UUID>``, one
``known_hosts`` file per VM. Both the directory and the file names are unique
to PySnap, and existing files are never overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess

from pysnap.errors import PySnapError

KEY_NAME = "pysnap_vm_access_ed25519"
KEY_COMMENT_PREFIX = "pysnap-vm-access@"

_UUID_PATTERN = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def find_executable(name: str) -> str:
    """Return the path of an OpenSSH program.

    :param name: ``ssh`` or ``ssh-keygen``.
    :returns: Executable path.
    :raises PySnapError: When the program is not installed.
    """
    path = shutil.which(name)
    if path is None:
        raise PySnapError(
            f"The OpenSSH program {name} was not found. Install the OpenSSH client "
            "(it is part of Linux distributions, macOS and Windows 10 and newer)."
        )
    return path


class SshKeyStore:
    """Locate and create the PySnap key pair and known_hosts files."""

    def __init__(self, root: Path | None = None) -> None:
        """Initialize the store.

        :param root: Directory to use instead of ``~/.pysnap/ssh``.
        """
        self.root = root or (Path.home() / ".pysnap" / "ssh")

    @property
    def private_key(self) -> Path:
        """Return the private key path."""
        return self.root / KEY_NAME

    @property
    def public_key(self) -> Path:
        """Return the public key path."""
        return self.root / f"{KEY_NAME}.pub"

    def known_hosts(self, vm_uuid: str) -> Path:
        """Return the known_hosts file of one VM, creating its directory.

        :param vm_uuid: VirtualBox UUID of the VM.
        :returns: File path; the file itself is written by ssh.
        :raises PySnapError: For an unusable UUID.
        """
        if not _UUID_PATTERN.match(vm_uuid):
            raise PySnapError(f'Invalid virtual machine UUID "{vm_uuid}".')
        directory = self._ensure_directory(self.root / "known_hosts.d")
        return directory / vm_uuid

    def ensure_key(self) -> Path:
        """Return the private key, creating the key pair on first use.

        :returns: Private key path.
        :raises PySnapError: When only one half of the pair exists, when a path
            is taken by something else or ``ssh-keygen`` fails.
        """
        self._ensure_directory(self.root)
        private_exists = os.path.lexists(self.private_key)
        public_exists = os.path.lexists(self.public_key)
        if private_exists and public_exists:
            for path in (self.private_key, self.public_key):
                if not path.is_file() or path.is_symlink():
                    raise PySnapError(f"{path} is not a regular file; PySnap does not use it.")
            return self.private_key
        if private_exists or public_exists:
            present = self.private_key if private_exists else self.public_key
            raise PySnapError(
                f"Only {present} exists; PySnap does not overwrite it. Move it away "
                "to let PySnap create a new key pair."
            )
        comment = f"{KEY_COMMENT_PREFIX}{socket.gethostname() or 'host'}"
        result = subprocess.run(
            [find_executable("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", comment,
             "-f", str(self.private_key)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PySnapError(f"ssh-keygen failed: {result.stderr.strip() or result.returncode}")
        return self.private_key

    def public_key_text(self) -> str:
        """Return the public key line, creating the key pair if needed.

        :returns: One ``ssh-ed25519 ... comment`` line.
        """
        self.ensure_key()
        return self.public_key.read_text(encoding="ascii").strip()

    @staticmethod
    def _ensure_directory(path: Path) -> Path:
        """Create a private directory or check an existing one."""
        if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise PySnapError(f"{path} exists and is not a directory; PySnap does not use it.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
