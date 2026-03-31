"""Unit tests for the proto-settings store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pysnap.config.protosettings import ProtoSettingsStore


class ProtoSettingsStoreTests(unittest.TestCase):
    """Verify persistent proto-settings storage."""

    def test_add_vm_name_creates_unique_entries(self) -> None:
        """Append one VM only once and preserve file order."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ProtoSettingsStore(path=Path(temp_dir) / ".ptotosettings")

            first = store.add_vm_name("base-a")
            second = store.add_vm_name("base-b")
            third = store.add_vm_name("base-a")

            self.assertEqual(first, ("base-a",))
            self.assertEqual(second, ("base-a", "base-b"))
            self.assertEqual(third, ("base-a", "base-b"))
            self.assertEqual(store.list_vm_names(), ("base-a", "base-b"))

    def test_remove_vm_names_deletes_matching_lines(self) -> None:
        """Remove configured VM names and keep the rest intact."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ProtoSettingsStore(path=Path(temp_dir) / ".ptotosettings")
            store.add_vm_name("base-a")
            store.add_vm_name("base-b")
            store.add_vm_name("base-c")

            remaining = store.remove_vm_names(["base-a", "base-c"])

            self.assertEqual(remaining, ("base-b",))
            self.assertEqual(store.list_vm_names(), ("base-b",))


if __name__ == "__main__":
    unittest.main()
