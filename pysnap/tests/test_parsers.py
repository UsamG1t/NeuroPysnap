"""Unit tests for VBoxManage parsers."""

from __future__ import annotations

import unittest

from pysnap.vbox.parsers import (
    parse_extra_data,
    parse_import_candidates,
    parse_list_vms,
    parse_machine_readable,
    parse_snapshot_names,
)


class ParserTests(unittest.TestCase):
    """Verify parsing of VirtualBox command output."""

    def test_parse_list_vms(self) -> None:
        """Parse VM names and UUIDs from ``list vms`` output."""
        output = '"base-vm" {11111111-1111-1111-1111-111111111111}\n'
        result = parse_list_vms(output)
        self.assertEqual(result[0].name, "base-vm")
        self.assertEqual(result[0].uuid, "11111111-1111-1111-1111-111111111111")

    def test_parse_machine_readable(self) -> None:
        """Parse machine-readable properties and snapshots."""
        output = (
            'name="base-vm"\n'
            'groups="/Lab"\n'
            'UUID="11111111-1111-1111-1111-111111111111"\n'
            'SnapshotName="current-snapshot"\n'
            'SnapshotName-1="older-snapshot"\n'
        )
        properties = parse_machine_readable(output)
        self.assertEqual(properties["name"], "base-vm")
        self.assertEqual(parse_snapshot_names(properties), ["current-snapshot", "older-snapshot"])

    def test_parse_extra_data(self) -> None:
        """Parse extra data entries returned by VirtualBox."""
        output = (
            "Key: pysnap/managed, Value: true\n"
            "Key: pysnap/parent, Value: base-vm\n"
        )
        metadata = parse_extra_data(output)
        self.assertEqual(metadata["pysnap/managed"], "true")
        self.assertEqual(metadata["pysnap/parent"], "base-vm")

    def test_parse_import_candidates(self) -> None:
        """Parse VM import metadata from a dry-run output."""
        output = (
            "Interpreting test.ova...\n"
            "OK.\n"
            "Virtual system 0:\n"
            ' 1: Suggested VM name "base-vm"\n'
            ' 2: Suggested VM group "/"\n'
            " 3: End-user license agreement\n"
        )
        result = parse_import_candidates(output)
        self.assertEqual(result[0].vm_name, "base-vm")
        self.assertEqual(result[0].group, "/")
        self.assertTrue(result[0].requires_eula_accept)


if __name__ == "__main__":
    unittest.main()
