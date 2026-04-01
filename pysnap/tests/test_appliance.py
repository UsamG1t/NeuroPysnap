"""Unit tests for appliance metadata parsing."""

from __future__ import annotations

from pathlib import Path
import tarfile
import tempfile
import unittest

from pysnap.core.appliance import read_appliance_vm_names


def _minimal_ovf(vm_names: list[str]) -> str:
    """Build a small OVF descriptor for one or more virtual systems."""
    systems = "\n".join(
        (
            f'  <VirtualSystem ovf:id="{vm_name}">\n'
            "    <Info>A virtual machine</Info>\n"
            f"    <Name>{vm_name}</Name>\n"
            "  </VirtualSystem>"
        )
        for vm_name in vm_names
    )
    return (
        '<?xml version="1.0"?>\n'
        '<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" '
        'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">\n'
        f"{systems}\n"
        "</Envelope>\n"
    )


class ApplianceTests(unittest.TestCase):
    """Verify OVF and OVA metadata extraction."""

    def test_read_appliance_vm_names_from_ovf(self) -> None:
        """Read declared VM names directly from an OVF descriptor."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ovf_path = Path(temp_dir) / "image.ovf"
            ovf_path.write_text(_minimal_ovf(["vm-a", "vm-b"]), encoding="utf-8")

            vm_names = read_appliance_vm_names(ovf_path)

        self.assertEqual(vm_names, ("vm-a", "vm-b"))

    def test_read_appliance_vm_names_from_ova(self) -> None:
        """Read declared VM names from the OVF descriptor embedded in an OVA."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ova_path = Path(temp_dir) / "image.ova"
            source_ovf = Path(temp_dir) / "image.ovf"
            source_ovf.write_text(_minimal_ovf(["vm-a"]), encoding="utf-8")
            with tarfile.open(ova_path, "w") as archive:
                archive.add(source_ovf, arcname="image.ovf")

            vm_names = read_appliance_vm_names(ova_path)

        self.assertEqual(vm_names, ("vm-a",))


if __name__ == "__main__":
    unittest.main()
