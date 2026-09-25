"""Parsing of the ``script`` recording stored in a report archive.

``report`` runs ``script -I IN.txt -O OUT.txt -B BOTH.txt -T TIME.txt``.
``TIME.txt`` uses the multi-stream timing format of util-linux ``script``:
one entry per line, ``<type> <delay> <value>``, where ``H`` lines carry
headers, ``I``/``O`` lines carry the byte size of an input or output chunk
and ``S`` lines carry signals such as terminal resizes. The byte counts
address the stream files after their ``Script started on ...`` line and
before their ``Script done on ...`` trailer.
"""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
from typing import Callable, TypeVar

from pysnap.errors import ReportFormatError
from pysnap.report.archive import read_report_archive
from pysnap.report.models import (
    Recording,
    ReportArchive,
    ReportDiagnostic,
    ReportLimits,
    ReportName,
    TimingEvent,
)

STREAM_START = b"Script started on "
STREAM_END = b"\nScript done on "

DIAG_MISSING_MEMBER = "missing-member"
DIAG_STREAM_HEADER = "stream-header"
DIAG_STREAM_TRAILER = "stream-trailer"
DIAG_TIMING_LINE = "timing-line"
DIAG_TIMING_OVERRUN = "timing-overrun"
DIAG_TIMING_UNDERRUN = "timing-underrun"
DIAG_BOTH_MISMATCH = "both-mismatch"
DIAG_HEADER_VALUE = "header-value"

_STREAM_KINDS = {"I", "O"}

T = TypeVar("T")


def load_report(path: str | Path, limits: ReportLimits | None = None) -> Recording:
    """Load and parse a report file.

    :param path: Path to the report file.
    :param limits: Resource limits, defaults to :class:`ReportLimits`.
    :returns: Parsed recording.
    :raises ReportFormatError: When the file is not a usable report.
    """
    report_path = Path(path)
    archive = read_report_archive(report_path, limits)
    return parse_recording(archive, report_path.name)


def parse_recording(archive: ReportArchive, source_name: str) -> Recording:
    """Parse the members of a report archive.

    Inconsistencies are reported as diagnostics; only a report without a
    timing log or an output log is rejected.

    :param archive: Raw archive members.
    :param source_name: Base name of the report file.
    :returns: Parsed recording.
    :raises ReportFormatError: When ``TIME.txt`` or ``OUT.txt`` is missing or
        the timing log uses the unsupported single-stream format.
    """
    diagnostics = list(archive.diagnostics)
    for required in ("TIME.txt", "OUT.txt"):
        if required not in archive.members:
            raise ReportFormatError(
                f'Report "{source_name}" does not contain {required}.'
            )
    if "IN.txt" not in archive.members:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_MISSING_MEMBER,
                "Report does not contain IN.txt; keyboard input is unavailable.",
            )
        )

    output_body = _stream_body(archive.members["OUT.txt"], "OUT.txt", diagnostics)
    input_body = _stream_body(archive.members.get("IN.txt", b""), "IN.txt", diagnostics)
    headers, events = _parse_timing(
        archive.members["TIME.txt"].decode("utf-8", errors="replace"),
        input_body,
        output_body,
        diagnostics,
        has_input="IN.txt" in archive.members,
    )

    if "BOTH.txt" in archive.members:
        both_body = _stream_body(archive.members["BOTH.txt"], "BOTH.txt", diagnostics)
        if len(both_body) != len(input_body) + len(output_body):
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_BOTH_MISMATCH,
                    f"BOTH.txt holds {len(both_body)} bytes while IN.txt and "
                    f"OUT.txt hold {len(input_body) + len(output_body)} bytes.",
                )
            )

    return Recording(
        source_name=source_name,
        name=ReportName.parse(source_name),
        headers=headers,
        start_time=_header_value(headers, "START_TIME", _parse_datetime, diagnostics),
        duration=_header_value(headers, "DURATION", _parse_duration, diagnostics),
        exit_code=_header_value(headers, "EXIT_CODE", int, diagnostics),
        columns=_header_value(headers, "COLUMNS", _parse_terminal_size, diagnostics),
        lines=_header_value(headers, "LINES", _parse_terminal_size, diagnostics),
        events=tuple(events),
        input_bytes=input_body,
        output_bytes=output_body,
        cpu_info=archive.members.get("CPU.txt", b"").decode("utf-8", errors="replace"),
        mtimes=dict(archive.mtimes),
        diagnostics=tuple(diagnostics),
    )


def _stream_body(
    data: bytes,
    member: str,
    diagnostics: list[ReportDiagnostic],
) -> bytes:
    """Strip the ``script`` start line and done trailer from a stream log.

    :param data: Raw member content.
    :param member: Member name used in diagnostics.
    :param diagnostics: Collected diagnostics, updated in place.
    :returns: Recorded stream bytes.
    """
    if not data:
        return b""
    start = 0
    if data.startswith(STREAM_START):
        newline = data.find(b"\n")
        start = len(data) if newline < 0 else newline + 1
    else:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_STREAM_HEADER,
                f'{member} does not start with "Script started on".',
            )
        )
    # The trailer starts with its own newline; right after an empty body that
    # newline is the one closing the start line, hence ``start - 1``.
    end = data.rfind(STREAM_END)
    if end < 0 or end < start - 1:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_STREAM_TRAILER,
                f'{member} has no "Script done on" trailer; the recording '
                "may be truncated.",
            )
        )
        return data[start:]
    return data[start:max(end, start)]


def _parse_timing(
    text: str,
    input_body: bytes,
    output_body: bytes,
    diagnostics: list[ReportDiagnostic],
    *,
    has_input: bool,
) -> tuple[dict[str, str], list[TimingEvent]]:
    """Parse the multi-stream timing log and slice the stream bodies.

    :param text: Decoded ``TIME.txt`` content.
    :param input_body: Recorded keyboard input.
    :param output_body: Recorded terminal output.
    :param diagnostics: Collected diagnostics, updated in place.
    :param has_input: Whether ``IN.txt`` was present in the archive.
    :returns: Header values and timed events.
    :raises ReportFormatError: When the log uses the single-stream format.
    """
    headers: dict[str, str] = {}
    events: list[TimingEvent] = []
    bodies = {"I": input_body, "O": output_body}
    offsets = {"I": 0, "O": 0}
    overrun_reported = {"I": not has_input, "O": False}
    elapsed = 0.0

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        kind = parts[0]
        if _is_float(kind):
            raise ReportFormatError(
                "The timing log uses the single-stream script format, which "
                "is not supported."
            )
        delay = _parse_float(parts[1]) if len(parts) == 3 else None
        if kind not in {"H", "S", *_STREAM_KINDS} or delay is None or delay < 0:
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_TIMING_LINE,
                    f"Ignored malformed TIME.txt line {line_number}: {line[:80]!r}.",
                )
            )
            continue
        if kind == "H":
            key, _, value = parts[2].partition(" ")
            headers.setdefault(key, value)
            continue

        elapsed += delay
        if kind == "S":
            events.append(TimingEvent("S", delay, elapsed, parts[2].encode("utf-8")))
            continue

        size = _parse_size(parts[2])
        if size is None:
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_TIMING_LINE,
                    f"Ignored malformed TIME.txt line {line_number}: {line[:80]!r}.",
                )
            )
            continue
        body = bodies[kind]
        start = offsets[kind]
        chunk = body[start:start + size]
        offsets[kind] = start + len(chunk)
        if len(chunk) < size and not overrun_reported[kind]:
            overrun_reported[kind] = True
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_TIMING_OVERRUN,
                    f"TIME.txt refers to more {_stream_name(kind)} bytes than "
                    "were recorded; the recording may be truncated.",
                )
            )
        events.append(TimingEvent(kind, delay, elapsed, chunk))

    for kind, body in bodies.items():
        if offsets[kind] < len(body):
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_TIMING_UNDERRUN,
                    f"{len(body) - offsets[kind]} recorded {_stream_name(kind)} "
                    "bytes are not covered by TIME.txt.",
                )
            )
    return headers, events


def _header_value(
    headers: dict[str, str],
    key: str,
    parser: Callable[[str], T | None],
    diagnostics: list[ReportDiagnostic],
) -> T | None:
    """Parse one optional header value.

    :param headers: Raw header values.
    :param key: Header name.
    :param parser: Conversion function raising ``ValueError`` on bad input;
        it may return ``None`` for a well-formed "unknown" value.
    :param diagnostics: Collected diagnostics, updated in place.
    :returns: Parsed value or ``None`` when absent, unknown or invalid.
    """
    raw_value = headers.get(key)
    if raw_value is None:
        return None
    try:
        return parser(raw_value.strip())
    except ValueError:
        diagnostics.append(
            ReportDiagnostic(
                DIAG_HEADER_VALUE,
                f"Ignored invalid {key} header value {raw_value[:40]!r}.",
            )
        )
        return None


def _stream_name(kind: str) -> str:
    """Return the human-readable stream name for an event kind."""
    return "input" if kind == "I" else "output"


def _is_float(value: str) -> bool:
    """Return whether a timing-log token is a plain number."""
    return _parse_float(value) is not None


def _parse_float(value: str) -> float | None:
    """Parse a finite floating-point number, returning ``None`` on failure."""
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_size(value: str) -> int | None:
    """Parse a non-negative byte count, returning ``None`` on failure."""
    return int(value) if value.isdigit() else None


def _parse_duration(value: str) -> float:
    """Parse the ``DURATION`` header, raising ``ValueError`` on bad input."""
    number = _parse_float(value)
    if number is None or number < 0:
        raise ValueError(value)
    return number


def _parse_terminal_size(value: str) -> int | None:
    """Parse ``COLUMNS``/``LINES``; ``script`` writes ``-1`` when unknown."""
    number = int(value)
    return number if number > 0 else None


def _parse_datetime(value: str) -> datetime:
    """Parse the ``START_TIME`` header written by ``script``."""
    return datetime.fromisoformat(value)
