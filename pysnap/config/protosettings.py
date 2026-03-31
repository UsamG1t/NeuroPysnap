"""Persistent storage for prototype clone settings."""

from __future__ import annotations

from pathlib import Path

from pysnap.errors import PySnapError


class ProtoSettingsStore:
    """Persist VM names that trigger extra clone settings."""

    DEFAULT_FILENAME = ".ptotosettings"

    def __init__(self, path: Path | None = None) -> None:
        """Initialize the store.

        :param path: Optional file path override.
        """
        self.path = path or (Path.home() / self.DEFAULT_FILENAME)

    def list_vm_names(self) -> tuple[str, ...]:
        """Return configured VM names without duplicates.

        :returns: Configured VM names in file order.
        :raises PySnapError: If the file cannot be read.
        """
        if not self.path.exists():
            return ()

        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise PySnapError(
                f'Unable to read proto settings from "{self.path}": {error}'
            ) from error

        unique_names: list[str] = []
        seen: set[str] = set()
        for line in raw_lines:
            vm_name = line.strip()
            if not vm_name or vm_name in seen:
                continue
            unique_names.append(vm_name)
            seen.add(vm_name)
        return tuple(unique_names)

    def contains(self, vm_name: str) -> bool:
        """Return whether one VM name is configured.

        :param vm_name: VM name to check.
        :returns: ``True`` when the VM is configured.
        """
        return vm_name in set(self.list_vm_names())

    def add_vm_name(self, vm_name: str) -> tuple[str, ...]:
        """Add one VM name to the store.

        :param vm_name: VM name to persist.
        :returns: Updated VM names.
        :raises PySnapError: If the file cannot be written.
        """
        vm_names = list(self.list_vm_names())
        if vm_name not in vm_names:
            vm_names.append(vm_name)
            self._write_vm_names(vm_names)
        return tuple(vm_names)

    def remove_vm_names(self, vm_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Remove VM names from the store.

        :param vm_names: VM names to remove.
        :returns: Remaining VM names.
        :raises PySnapError: If the file cannot be written.
        """
        current_names = list(self.list_vm_names())
        if not current_names:
            return ()

        remove_set = set(vm_names)
        remaining = [name for name in current_names if name not in remove_set]
        if remaining != current_names:
            self._write_vm_names(remaining)
        return tuple(remaining)

    def _write_vm_names(self, vm_names: list[str]) -> None:
        """Write VM names to the store file.

        :param vm_names: VM names to write.
        :raises PySnapError: If the file cannot be written.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(vm_names)
            if content:
                content = f"{content}\n"
            self.path.write_text(content, encoding="utf-8")
        except OSError as error:
            raise PySnapError(
                f'Unable to write proto settings to "{self.path}": {error}'
            ) from error
