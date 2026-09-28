"""Safe reading of ``report`` archives.

A report is an untrusted ``tar.gz`` file produced inside a student VM. The
reader never extracts anything to disk: it streams the archive once, keeps
only regular files with the names written by ``report`` and bounds both the
compressed and the decompressed amount of data it is willing to process.
"""

from __future__ import annotations

import gzip
from pathlib import Path
import tarfile
from typing import BinaryIO
import zlib

from pysnap.errors import ReportFormatError
from pysnap.report.models import ReportArchive, ReportDiagnostic, ReportLimits

# Members written by ``report`` through ``tar -C $BASE -czf NAME .``.
KNOWN_MEMBERS = ("CPU.txt", "IN.txt", "OUT.txt", "BOTH.txt", "TIME.txt")

DIAG_IGNORED_MEMBER = "ignored-member"
DIAG_OVERSIZED_MEMBER = "oversized-member"
DIAG_DUPLICATE_MEMBER = "duplicate-member"


class _UnpackedLimitExceeded(Exception):
    """Signal that the decompressed stream grew beyond the configured limit."""


class _BoundedReader:
    """Count the bytes read from a stream and stop at a fixed limit."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        """Initialize the bounded reader.

        :param stream: Decompressed archive stream.
        :param limit: Largest number of bytes that may be read.
        """
        self._stream = stream
        self._limit = limit
        self._consumed = 0

    def read(self, size: int = -1) -> bytes:
        """Read from the wrapped stream while enforcing the limit.

        :param size: Requested number of bytes, negative for "until the limit".
        :returns: Bytes read from the wrapped stream.
        :raises _UnpackedLimitExceeded: When the limit is exceeded.
        """
        remaining = self._limit - self._consumed + 1
        chunk = self._stream.read(remaining if size < 0 else min(size, remaining))
        self._consumed += len(chunk)
        if self._consumed > self._limit:
            raise _UnpackedLimitExceeded
        return chunk


def read_report_archive(
    path: str | Path,
    limits: ReportLimits | None = None,
) -> ReportArchive:
    """Read the known members of a report archive into memory.

    Unknown names, links, devices, directories and path-traversal entries are
    skipped with a diagnostic; their content is never read.

    :param path: Path to the report file.
    :param limits: Resource limits, defaults to :class:`ReportLimits`.
    :returns: Raw archive members.
    :raises ReportFormatError: When the file is missing, too large, or is not
        a gzip-compressed tar archive.
    """
    active_limits = limits or ReportLimits()
    report_path = Path(path)
    if not report_path.is_file():
        raise ReportFormatError(f'Report file "{report_path}" was not found.')
    if report_path.stat().st_size > active_limits.max_archive_bytes:
        raise ReportFormatError(
            f'Report file "{report_path}" is larger than '
            f"{active_limits.max_archive_bytes} bytes."
        )

    members: dict[str, bytes] = {}
    mtimes: dict[str, int] = {}
    diagnostics: list[ReportDiagnostic] = []
    try:
        with report_path.open("rb") as raw_file, gzip.GzipFile(
            fileobj=raw_file, mode="rb"
        ) as unpacked:
            bounded = _BoundedReader(unpacked, active_limits.max_unpacked_bytes)
            # Stream mode reads the archive strictly sequentially, so skipped
            # members are consumed through the bounded reader as well.
            with tarfile.open(fileobj=bounded, mode="r|") as archive:  # type: ignore[arg-type]
                for member in archive:
                    _read_member(
                        archive, member, active_limits, members, mtimes, diagnostics
                    )
    except _UnpackedLimitExceeded:
        raise ReportFormatError(
            f'Report file "{report_path}" unpacks to more than '
            f"{active_limits.max_unpacked_bytes} bytes."
        ) from None
    except (tarfile.TarError, OSError, EOFError, zlib.error) as error:
        raise ReportFormatError(
            f'File "{report_path}" is not a report archive: {error}'
        ) from None

    return ReportArchive(
        members=members, mtimes=mtimes, diagnostics=tuple(diagnostics)
    )


def _read_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    limits: ReportLimits,
    members: dict[str, bytes],
    mtimes: dict[str, int],
    diagnostics: list[ReportDiagnostic],
) -> None:
    """Store one archive member when it is a known regular file.

    :param archive: Open archive positioned at ``member``.
    :param member: Current archive member.
    :param limits: Resource limits.
    :param members: Collected member contents, updated in place.
    :param mtimes: Collected member times, updated in place.
    :param diagnostics: Collected diagnostics, updated in place.
    """
    name = member.name[2:] if member.name.startswith("./") else member.name
    if member.isdir() and name in {"", "."}:
        return
    if name not in KNOWN_MEMBERS or not member.isreg():
        diagnostics.append(
            ReportDiagnostic(
                DIAG_IGNORED_MEMBER,
                f'Ignored unexpected archive entry "{member.name}".',
            )
        )
        return
    if name in members:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_DUPLICATE_MEMBER,
                f'Ignored duplicate archive entry "{member.name}".',
            )
        )
        return
    if member.size > limits.max_member_bytes:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_OVERSIZED_MEMBER,
                f'Ignored archive entry "{member.name}" larger than '
                f"{limits.max_member_bytes} bytes.",
            )
        )
        return
    content = archive.extractfile(member)
    members[name] = content.read() if content is not None else b""
    mtimes[name] = int(member.mtime)
