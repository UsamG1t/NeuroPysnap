"""Unit tests for safe reading of report archives."""

from __future__ import annotations

import gzip
from pathlib import Path
import tarfile
import tempfile
import unittest

from pysnap.errors import ReportFormatError
from pysnap.report.archive import (
    DIAG_DUPLICATE_MEMBER,
    DIAG_IGNORED_MEMBER,
    DIAG_OVERSIZED_MEMBER,
    read_report_archive,
)
from pysnap.report.models import ReportLimits
from pysnap.tests.report_factory import (
    ARCHIVE_MTIME,
    ArchiveEntry,
    ReportBuilder,
    write_archive,
    write_report,
)


def _simple_members() -> dict[str, bytes]:
    """Return the members of a one-command report."""
    builder = ReportBuilder().show_prompt()
    builder.run("hostname", "first\n")
    return builder.finish()


class ReadReportArchiveTests(unittest.TestCase):
    """Verify which archive entries are read and which are refused."""

    def setUp(self) -> None:
        """Create a scratch directory for archives."""
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        """Remove the scratch directory."""
        self._temp_dir.cleanup()

    def test_reads_all_members_of_a_regular_report(self) -> None:
        """Read the five files written by ``report`` with their mtimes."""
        members = _simple_members()
        archive = read_report_archive(write_report(self.root / "report.01.first", members))

        self.assertEqual(archive.members, members)
        self.assertEqual(set(archive.mtimes.values()), {ARCHIVE_MTIME})
        self.assertEqual(archive.diagnostics, ())

    def test_ignores_path_traversal_links_and_unknown_entries(self) -> None:
        """Skip hostile entries with diagnostics and never touch the disk."""
        members = _simple_members()
        entries = [
            ArchiveEntry("./", kind=tarfile.DIRTYPE),
            ArchiveEntry("../TIME.txt", b"H 0 EVIL x\n"),
            ArchiveEntry("/etc/OUT.txt", b"evil"),
            ArchiveEntry("./CPU.txt", kind=tarfile.SYMTYPE, link_target="/etc/passwd"),
            ArchiveEntry("./IN.txt", kind=tarfile.LNKTYPE, link_target="./OUT.txt"),
            ArchiveEntry("./sub", kind=tarfile.DIRTYPE),
            ArchiveEntry("./notes.txt", b"extra"),
            ArchiveEntry("./TIME.txt", members["TIME.txt"]),
            ArchiveEntry("./OUT.txt", members["OUT.txt"]),
        ]
        path = write_archive(self.root / "report.01.first", entries)
        before = sorted(self.root.rglob("*"))

        archive = read_report_archive(path)

        self.assertEqual(sorted(archive.members), ["OUT.txt", "TIME.txt"])
        self.assertEqual(archive.members["TIME.txt"], members["TIME.txt"])
        self.assertEqual(
            [item.code for item in archive.diagnostics],
            [DIAG_IGNORED_MEMBER] * 6,
        )
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_keeps_the_first_of_duplicate_entries(self) -> None:
        """Report a duplicated member and keep its first copy."""
        entries = [
            ArchiveEntry("./TIME.txt", b"first"),
            ArchiveEntry("./TIME.txt", b"second"),
        ]
        archive = read_report_archive(write_archive(self.root / "r", entries))

        self.assertEqual(archive.members["TIME.txt"], b"first")
        self.assertEqual(archive.diagnostics[0].code, DIAG_DUPLICATE_MEMBER)

    def test_skips_members_above_the_member_limit(self) -> None:
        """Leave out a member larger than ``max_member_bytes``."""
        entries = [
            ArchiveEntry("./OUT.txt", b"x" * 100),
            ArchiveEntry("./TIME.txt", b"small"),
        ]
        limits = ReportLimits(max_member_bytes=50)
        archive = read_report_archive(write_archive(self.root / "r", entries), limits)

        self.assertEqual(sorted(archive.members), ["TIME.txt"])
        self.assertEqual(archive.diagnostics[0].code, DIAG_OVERSIZED_MEMBER)

    def test_rejects_archives_above_the_compressed_limit(self) -> None:
        """Refuse a report file larger than ``max_archive_bytes``."""
        path = write_report(self.root / "r", _simple_members())
        limits = ReportLimits(max_archive_bytes=path.stat().st_size - 1)

        with self.assertRaisesRegex(ReportFormatError, "larger than"):
            read_report_archive(path, limits)

    def test_rejects_decompression_bombs(self) -> None:
        """Stop reading once the decompressed stream exceeds the limit."""
        entries = [ArchiveEntry("./junk.bin", b"\0" * 2_000_000)]
        path = write_archive(self.root / "r", entries)
        limits = ReportLimits(max_unpacked_bytes=1_000_000)

        self.assertLess(path.stat().st_size, 100_000)
        with self.assertRaisesRegex(ReportFormatError, "unpacks to more than"):
            read_report_archive(path, limits)

    def test_rejects_missing_files_and_directories(self) -> None:
        """Report a clear error for paths that are not files."""
        with self.assertRaisesRegex(ReportFormatError, "was not found"):
            read_report_archive(self.root / "missing")
        with self.assertRaisesRegex(ReportFormatError, "was not found"):
            read_report_archive(self.root)

    def test_rejects_files_that_are_not_gzip_tar_archives(self) -> None:
        """Refuse plain text, gzip without tar and truncated archives."""
        plain = self.root / "plain"
        plain.write_bytes(b"just text\n")
        gzip_text = self.root / "gzip-text"
        gzip_text.write_bytes(gzip.compress(b"not a tar archive\n" * 40))
        truncated = self.root / "truncated"
        full = write_report(self.root / "full", _simple_members()).read_bytes()
        truncated.write_bytes(full[: len(full) // 2])

        for path in (plain, gzip_text, truncated):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ReportFormatError, "not a report archive"):
                    read_report_archive(path)


if __name__ == "__main__":
    unittest.main()
