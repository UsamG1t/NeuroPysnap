"""Pairwise comparison of session reports (decisions D41, D42).

``pysnap report compare`` looks for signs that reports of different students
share their origin. Every pair of the given reports is compared; deciding
which reports belong to the same student is left to the teacher. The
signals only point at pairs worth a look and never decide anything.

Strong signals:

- ``S1`` the report files are identical (SHA-256 of the archives);
- ``S2`` the recorded output is identical (SHA-256 of the output body);
- ``S3`` both outputs show the same MAC address, except the all-zero and
  broadcast addresses and group (multicast) addresses;
- ``S4`` the same ``START_TIME`` (to the second) and ``DURATION`` (to the
  microsecond).

Medium signals:

- ``M1`` at least 20 identical consecutive keyboard input delays, compared to
  the microsecond: a copied or edited timing log;
- ``M2`` values that are random in practice: at least three consecutive
  ``ping`` round-trip times (``time=... ms``) in the same order, or at least
  three shared ``tcpdump`` timestamps with microseconds. ``ping`` prints only
  three significant digits, so single values often match by chance, while a
  run in the same order does not;
- ``M3`` byte-identical ``CPU.txt`` (it includes the BogoMIPS value measured
  at boot);
- ``M4`` at least two shared erroneous commands, whose output reports
  ``command not found`` or ``No such file``.

Weak signal:

- ``W1`` the same CPU model.

Signals implied by a stronger one of the same pair are not repeated: an
identical file (``S1``) implies all others, and an identical ``CPU.txt``
(``M3``) implies the same CPU model (``W1``).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import combinations
from pathlib import Path
import re
from typing import Iterable, Sequence

from pysnap.errors import PySnapError
from pysnap.report.models import Recording, ReportLimits, ReportName, Transcript
from pysnap.report.recording import load_report
from pysnap.report.render import render_transcript
from pysnap.report.stats import MAC_PATTERN, SERVICE_MACS, parse_cpu_info, transcript_output_text

DELAY_RUN = 20
PING_RUN = 3
MIN_TCPDUMP_TIMES = 3
MIN_ERROR_COMMANDS = 2
LEVELS = ("strong", "medium", "weak")

_PING_TIME = re.compile(r"\btime=(\d+(?:\.\d+)?) ?ms\b")
_TCPDUMP_TIME = re.compile(r"(?<![\d:.])\d{2}:\d{2}:\d{2}\.\d{6}(?![\d.])")
_ERROR_MARKERS = ("command not found", "No such file")


@dataclass(frozen=True)
class CompareSignal:
    """Represent one signal found for a pair of reports.

    :param code: Signal code such as ``"S3"``.
    :param level: ``"strong"``, ``"medium"`` or ``"weak"``.
    :param description: Human-readable description with the shared values.
    """

    code: str
    level: str
    description: str


@dataclass(frozen=True)
class PairResult:
    """Represent the comparison of two reports.

    :param first: Path of the first report as found.
    :param second: Path of the second report as found.
    :param signals: Signals found, strongest first.
    """

    first: str
    second: str
    signals: tuple[CompareSignal, ...]

    @property
    def level(self) -> int:
        """Return the rank of the strongest signal, ``len(LEVELS)`` for none."""
        return min((LEVELS.index(signal.level) for signal in self.signals), default=len(LEVELS))


@dataclass(frozen=True)
class CompareResult:
    """Represent the comparison of a set of reports.

    :param reports: Paths of the compared reports.
    :param pairs: Pairs with signals, strongest first, then pairs without.
    :param warnings: Paths that were skipped and why.
    """

    reports: tuple[str, ...]
    pairs: tuple[PairResult, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReportFingerprint:
    """Hold the values of one report that the signals compare.

    :param path: Path of the report as found.
    :param file_sha256: SHA-256 of the report file.
    :param output_sha256: SHA-256 of the output body, ``None`` when empty.
    :param macs: Unicast MAC addresses seen in the output.
    :param start: ``START_TIME`` to the second and ``DURATION`` in
        microseconds, when both are known.
    :param delay_windows: Hash of every run of ``DELAY_RUN`` consecutive input
        delays in microseconds, mapped to its first position. Runs of one
        repeated delay are left out.
    :param ping_times: ``ping`` round-trip times in output order.
    :param tcpdump_times: ``tcpdump`` timestamps.
    :param error_commands: Commands whose output reports an error.
    :param cpu_info: Content of ``CPU.txt``.
    :param cpu_model: CPU model from ``CPU.txt``.
    """

    path: str
    file_sha256: str
    output_sha256: str | None
    macs: frozenset[str]
    start: tuple[str, int] | None
    delay_windows: dict[int, int]
    ping_times: tuple[str, ...]
    tcpdump_times: frozenset[str]
    error_commands: frozenset[str]
    cpu_info: str
    cpu_model: str | None


def collect_report_paths(paths: Iterable[str | Path]) -> tuple[list[Path], list[str]]:
    """Expand files and directories into the list of report files.

    Directories are searched recursively for files named ``report.NN.HOST``;
    files given explicitly are used whatever their name. A file reached
    twice is compared once.

    :param paths: Files and directories.
    :returns: Report paths and warnings for paths that gave no reports.
    """
    found: list[Path] = []
    warnings: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            reports = sorted(
                item for item in path.rglob("*")
                if item.is_file() and ReportName.parse(item.name) is not None
            )
            if not reports:
                warnings.append(f"{path}: no report files (report.NN.HOST) in the directory")
            for report in reports:
                add(report)
        elif path.is_file():
            add(path)
        else:
            warnings.append(f"{path}: no such file or directory")
    return found, warnings


def fingerprint_report(path: str | Path, recording: Recording, transcript: Transcript) -> ReportFingerprint:
    """Collect the compared values of one report.

    :param path: Path of the report file.
    :param recording: Parsed report.
    :param transcript: Rendered report.
    :returns: Report fingerprint.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    output = transcript_output_text(transcript)
    macs = frozenset(
        mac for mac in (value.lower() for value in MAC_PATTERN.findall(output))
        if mac not in SERVICE_MACS and not int(mac[:2], 16) & 1
    )
    start = None
    if recording.start_time is not None and recording.duration is not None:
        start = (
            recording.start_time.replace(microsecond=0).isoformat(),
            round(recording.duration * 1_000_000),
        )
    delays = [round(event.delay * 1_000_000) for event in recording.events if event.kind == "I"]
    windows: dict[int, int] = {}
    for index in range(len(delays) - DELAY_RUN + 1):
        window = tuple(delays[index:index + DELAY_RUN])
        if len(set(window)) > 1:
            windows.setdefault(hash(window), index)
    errors = set()
    for command in transcript.commands:
        text = "\n".join(line.text for line in transcript.lines[command.output_start:command.output_end])
        if command.text.strip() and any(marker in text for marker in _ERROR_MARKERS):
            errors.add(command.text.strip())
    return ReportFingerprint(
        path=str(path),
        file_sha256=digest.hexdigest(),
        output_sha256=hashlib.sha256(recording.output_bytes).hexdigest() if recording.output_bytes else None,
        macs=macs,
        start=start,
        delay_windows=windows,
        ping_times=tuple(_PING_TIME.findall(output)),
        tcpdump_times=frozenset(_TCPDUMP_TIME.findall(output)),
        error_commands=frozenset(errors),
        cpu_info=recording.cpu_info,
        cpu_model=parse_cpu_info(recording.cpu_info).get("Model name") or None,
    )


def compare_pair(first: ReportFingerprint, second: ReportFingerprint) -> PairResult:
    """Compare two reports.

    :param first: First report.
    :param second: Second report.
    :returns: The pair with its signals, strongest first.
    """
    if first.file_sha256 == second.file_sha256:
        signal = CompareSignal("S1", "strong", "identical report files; other signals are not listed")
        return PairResult(first.path, second.path, (signal,))
    signals: list[CompareSignal] = []
    if first.output_sha256 is not None and first.output_sha256 == second.output_sha256:
        signals.append(CompareSignal("S2", "strong", "identical recorded output"))
    macs = sorted(first.macs & second.macs)
    if macs:
        signals.append(CompareSignal("S3", "strong", f"same MAC address: {', '.join(macs)}"))
    if first.start is not None and first.start == second.start:
        signals.append(CompareSignal(
            "S4", "strong",
            f"same START_TIME {first.start[0]} and DURATION {first.start[1] / 1_000_000:.6f}",
        ))
    run = _longest_delay_run(first.delay_windows, second.delay_windows)
    if run:
        signals.append(CompareSignal("M1", "medium", f"{run} identical consecutive keyboard delays"))
    random_parts = []
    ping_run = _common_ping_run(first.ping_times, second.ping_times)
    if ping_run:
        times = ", ".join(f"time={value} ms" for value in ping_run)
        random_parts.append(f"ping times in the same order: {times}")
    stamps = sorted(first.tcpdump_times & second.tcpdump_times)
    if len(stamps) >= MIN_TCPDUMP_TIMES:
        random_parts.append(f"tcpdump timestamps: {_shorten(stamps)}")
    if random_parts:
        signals.append(CompareSignal("M2", "medium", "same random output values: " + "; ".join(random_parts)))
    if first.cpu_info and first.cpu_info == second.cpu_info:
        signals.append(CompareSignal("M3", "medium", "identical CPU.txt"))
    errors = sorted(first.error_commands & second.error_commands)
    if len(errors) >= MIN_ERROR_COMMANDS:
        signals.append(CompareSignal("M4", "medium", f"same erroneous commands: {_shorten(errors)}"))
    if first.cpu_model and first.cpu_model == second.cpu_model and not (
        first.cpu_info and first.cpu_info == second.cpu_info
    ):
        signals.append(CompareSignal("W1", "weak", f"same CPU model: {first.cpu_model}"))
    return PairResult(first.path, second.path, tuple(signals))


def compare_fingerprints(fingerprints: Sequence[ReportFingerprint]) -> tuple[PairResult, ...]:
    """Compare every pair of reports.

    :param fingerprints: Reports in input order.
    :returns: Pairs with signals, strongest first, then pairs without.
    """
    pairs = [compare_pair(first, second) for first, second in combinations(fingerprints, 2)]
    flagged = sorted(
        (pair for pair in pairs if pair.signals),
        key=lambda pair: (pair.level, -len(pair.signals)),
    )
    return tuple(flagged + [pair for pair in pairs if not pair.signals])


def compare_reports(paths: Iterable[str | Path], limits: ReportLimits | None = None) -> CompareResult:
    """Load, fingerprint and compare reports.

    :param paths: Report files and directories.
    :param limits: Resource limits for reading each report.
    :returns: Comparison result; ``reports`` is empty when none could be
        read.
    """
    report_paths, warnings = collect_report_paths(paths)
    fingerprints = []
    for path in report_paths:
        try:
            recording = load_report(path, limits)
            fingerprints.append(fingerprint_report(path, recording, render_transcript(recording)))
        except (PySnapError, OSError) as error:
            warnings.append(f"{path}: skipped: {error}")
    return CompareResult(
        reports=tuple(fingerprint.path for fingerprint in fingerprints),
        pairs=compare_fingerprints(fingerprints),
        warnings=tuple(warnings),
    )


def _longest_delay_run(first: dict[int, int], second: dict[int, int]) -> int:
    """Return the longest shared run of input delays, 0 when shorter than ``DELAY_RUN``."""
    shared = {(position, second[window]) for window, position in first.items() if window in second}
    if not shared:
        return 0
    longest = 0
    for start in shared:
        if (start[0] - 1, start[1] - 1) in shared:
            continue
        length = 1
        while (start[0] + length, start[1] + length) in shared:
            length += 1
        longest = max(longest, length)
    return longest + DELAY_RUN - 1


def _common_ping_run(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    """Return the longest run of ping times shared in order, empty when shorter than ``PING_RUN``."""
    starts: dict[tuple[str, ...], list[int]] = {}
    for index in range(len(first) - PING_RUN + 1):
        starts.setdefault(first[index:index + PING_RUN], []).append(index)
    best: tuple[str, ...] = ()
    for index in range(len(second) - PING_RUN + 1):
        for start in starts.get(second[index:index + PING_RUN], ()):
            length = PING_RUN
            while (
                start + length < len(first) and index + length < len(second)
                and first[start + length] == second[index + length]
            ):
                length += 1
            if length > len(best):
                best = first[start:start + length]
    return best


def _shorten(values: Sequence[str], limit: int = 5) -> str:
    """Join values, listing at most ``limit`` of them."""
    text = ", ".join(values[:limit])
    return text if len(values) <= limit else f"{text} and {len(values) - limit} more"
