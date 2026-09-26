"""Report information and statistics for ``pysnap report check``.

Every value comes directly from the report: the ``script`` headers, the
archive member times, ``CPU.txt``, the timed keystrokes and the rendered
transcript. The definitions follow the specification decisions D29 and D33.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
import statistics

from pysnap.report.models import Recording, ReportDiagnostic, Transcript

DEFAULT_PASTE_MIN_CHARACTERS = 5
DEFAULT_MTIME_TOLERANCE = 2.0

BRACKETED_PASTE_START = b"\x1b[200~"
_KEY_PATTERN = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences such as arrows or Delete
    rb"|\x1bO."  # SS3 sequences such as application-mode arrows
    rb"|\x1b[\x00-\xff]"  # Alt+key
    rb"|[\xc0-\xff][\x80-\xbf]*"  # one UTF-8 character
    rb"|[\x00-\xff]",
    re.DOTALL,
)
_BACKSPACE_KEYS = {b"\x7f", b"\x08"}
_HISTORY_KEYS = {b"\x1b[A", b"\x1bOA", b"\x1b[B", b"\x1bOB", b"\x12"}
_INTERRUPT_KEY = b"\x03"
_IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC_PATTERN = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_SERVICE_MACS = {"ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}
# ``report`` writes CPU.txt right before starting ``script`` and packs the
# other members right after it ends.
_MEMBER_EXPECTED_TIME = {
    "CPU.txt": "start",
    "IN.txt": "end",
    "OUT.txt": "end",
    "BOTH.txt": "end",
    "TIME.txt": "end",
}


@dataclass(frozen=True)
class CommandStats:
    """Represent the statistics of one entered command.

    :param index: Zero-based command position.
    :param text: Entered command.
    :param pause: Seconds from the last output before the command to its
        first keystroke.
    :param typing_time: Seconds from the first keystroke to Enter.
    :param keys: Keystrokes from the first key through Enter.
    :param backspaces: Backspace keystrokes among ``keys``.
    :param from_history: Whether history keys (Up, Down, Ctrl-R) were used.
    :param interrupted: Whether ``Ctrl-C`` was typed while it ran.
    :param foreign_prompt: Whether it was typed at a prompt other than the
        one installed by ``report``.
    """

    index: int
    text: str
    pause: float
    typing_time: float
    keys: int
    backspaces: int
    from_history: bool
    interrupted: bool
    foreign_prompt: bool


@dataclass(frozen=True)
class PasteEvent:
    """Represent an input chunk that looks like pasted text.

    :param time: Recording time of the chunk in seconds.
    :param text: Printable text of the chunk.
    :param command: Index of the command the chunk belongs to, if any.
    """

    time: float
    text: str
    command: int | None


@dataclass(frozen=True)
class MemberTimeCheck:
    """Compare the archive time of a member with the recording time.

    :param member: Archive member name.
    :param expected: ``"start"`` or ``"end"`` of the recording.
    :param difference: Member time minus expected time in seconds.
    :param within_tolerance: Whether ``difference`` is within the tolerance.
    """

    member: str
    expected: str
    difference: float
    within_tolerance: bool


@dataclass(frozen=True)
class ReportStats:
    """Represent the information block of ``pysnap report check``."""

    source_name: str
    file_task: int | None
    file_host: str | None
    prompt_identities: tuple[tuple[int, str], ...]
    start_time: datetime | None
    end_time: datetime | None
    duration: float | None
    exit_code: int | None
    tty: str | None
    term: str | None
    columns: int | None
    lines: int | None
    cpu_model: str | None
    hypervisor: str | None
    commands: tuple[CommandStats, ...]
    unique_commands: int
    pause_min: float | None
    pause_median: float | None
    pause_max: float | None
    longest_pause_command: str | None
    typing_speed: float | None
    backspace_share: float | None
    pastes: tuple[PasteEvent, ...]
    ip_addresses: tuple[str, ...]
    mac_addresses: tuple[str, ...]
    member_times: tuple[MemberTimeCheck, ...]
    diagnostics: tuple[ReportDiagnostic, ...]
    warnings: tuple[str, ...]


def compute_stats(
    recording: Recording,
    transcript: Transcript,
    *,
    paste_min_characters: int = DEFAULT_PASTE_MIN_CHARACTERS,
    mtime_tolerance: float = DEFAULT_MTIME_TOLERANCE,
) -> ReportStats:
    """Compute the information block of a report.

    :param recording: Parsed recording.
    :param transcript: Rendered transcript.
    :param paste_min_characters: Printable characters in one input chunk
        from which the chunk counts as pasted.
    :param mtime_tolerance: Accepted difference in seconds between archive
        member times and the recording start or end.
    :returns: Report statistics.
    """
    events = recording.events
    commands = transcript.commands

    command_stats: list[CommandStats] = []
    for position, command in enumerate(commands):
        next_start = commands[position + 1].first_event if position + 1 < len(commands) else len(events)
        keys = [
            key
            for event in events[command.first_event:command.enter_event + 1]
            if event.kind == "I"
            for key in split_keys(event.data)
        ]
        previous_output = max(
            (event.time for event in events[:command.first_event] if event.kind == "O"),
            default=0.0,
        )
        command_stats.append(
            CommandStats(
                index=command.index,
                text=command.text,
                pause=max(events[command.first_event].time - previous_output, 0.0),
                typing_time=max(events[command.enter_event].time - events[command.first_event].time, 0.0),
                keys=len(keys),
                backspaces=sum(1 for key in keys if key in _BACKSPACE_KEYS),
                from_history=any(key in _HISTORY_KEYS for key in keys),
                interrupted=any(
                    event.kind == "I" and _INTERRUPT_KEY in event.data
                    for event in events[command.enter_event + 1:next_start]
                ),
                foreign_prompt=command.prompt_info is None,
            )
        )

    pauses = [item.pause for item in command_stats]
    longest = max(command_stats, key=lambda item: item.pause, default=None)
    total_keys = sum(item.keys for item in command_stats)
    total_typing = sum(item.typing_time for item in command_stats)

    identities = tuple(
        dict.fromkeys(
            (command.prompt_info.task, command.prompt_info.host)
            for command in commands
            if command.prompt_info is not None
        )
    )
    end_time = (
        recording.start_time + timedelta(seconds=recording.duration)
        if recording.start_time is not None and recording.duration is not None
        else None
    )
    output_text = _output_text(transcript)
    cpu = _parse_cpu_info(recording.cpu_info)
    member_times = _member_time_checks(recording, end_time, mtime_tolerance)
    pastes = _find_pastes(recording, transcript, paste_min_characters)
    diagnostics = (*recording.diagnostics, *transcript.diagnostics)

    stats = ReportStats(
        source_name=recording.source_name,
        file_task=recording.name.task if recording.name else None,
        file_host=recording.name.host if recording.name else None,
        prompt_identities=identities,
        start_time=recording.start_time,
        end_time=end_time,
        duration=recording.duration,
        exit_code=recording.exit_code,
        tty=recording.headers.get("TTY"),
        term=recording.headers.get("TERM"),
        columns=recording.columns,
        lines=recording.lines,
        cpu_model=cpu.get("Model name"),
        hypervisor=cpu.get("Hypervisor vendor"),
        commands=tuple(command_stats),
        unique_commands=len({item.text for item in command_stats}),
        pause_min=min(pauses) if pauses else None,
        pause_median=statistics.median(pauses) if pauses else None,
        pause_max=max(pauses) if pauses else None,
        longest_pause_command=longest.text if longest else None,
        typing_speed=total_keys / total_typing if total_typing > 0 else None,
        backspace_share=(
            sum(item.backspaces for item in command_stats) / total_keys if total_keys else None
        ),
        pastes=pastes,
        ip_addresses=tuple(sorted(set(_valid_ipv4(output_text)), key=_ipv4_key)),
        mac_addresses=tuple(
            sorted(
                {mac.lower() for mac in _MAC_PATTERN.findall(output_text)} - _SERVICE_MACS
            )
        ),
        member_times=member_times,
        diagnostics=diagnostics,
        warnings=(),
    )
    return _with_warnings(stats)


def split_keys(data: bytes) -> list[bytes]:
    """Split raw keyboard input into keystrokes.

    Escape sequences such as cursor keys count as one keystroke; a chunk may
    hold several keystrokes because the serial console groups fast typing.

    :param data: Raw input bytes.
    :returns: One item per keystroke.
    """
    return _KEY_PATTERN.findall(data)


def _printable_text(data: bytes) -> str:
    """Return the printable characters of an input chunk."""
    text = b"".join(key for key in split_keys(data) if not key.startswith(b"\x1b"))
    decoded = text.decode("utf-8", errors="replace")
    return "".join(char for char in decoded if char.isprintable())


def _find_pastes(
    recording: Recording,
    transcript: Transcript,
    min_characters: int,
) -> tuple[PasteEvent, ...]:
    """Find input chunks that look like pasted text."""
    owners: dict[int, int] = {}
    for command in transcript.commands:
        for index in range(command.first_event, command.enter_event + 1):
            owners[index] = command.index
    pastes = []
    for index, event in enumerate(recording.events):
        if event.kind != "I":
            continue
        text = _printable_text(event.data)
        if BRACKETED_PASTE_START in event.data or len(text) >= min_characters:
            pastes.append(PasteEvent(event.time, text, owners.get(index)))
    return tuple(pastes)


def _output_text(transcript: Transcript) -> str:
    """Return the transcript text without the lines of entered commands."""
    input_lines: set[int] = set()
    for command in transcript.commands:
        input_lines.update(range(command.line, command.output_start))
    return "\n".join(
        line.text for number, line in enumerate(transcript.lines) if number not in input_lines
    )


def _valid_ipv4(text: str) -> list[str]:
    """Return dotted quads whose octets are within 0..255."""
    return [
        candidate
        for candidate in _IPV4_PATTERN.findall(text)
        if all(int(octet) <= 255 for octet in candidate.split("."))
    ]


def _ipv4_key(address: str) -> tuple[int, ...]:
    """Sort IPv4 addresses numerically."""
    return tuple(int(octet) for octet in address.split("."))


def _parse_cpu_info(cpu_info: str) -> dict[str, str]:
    """Parse ``lscpu`` output into a field mapping."""
    fields: dict[str, str] = {}
    for line in cpu_info.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    return fields


def _member_time_checks(
    recording: Recording,
    end_time: datetime | None,
    tolerance: float,
) -> tuple[MemberTimeCheck, ...]:
    """Compare archive member times with the recording start and end."""
    if recording.start_time is None:
        return ()
    anchors = {"start": recording.start_time, "end": end_time}
    checks = []
    for member, expected in _MEMBER_EXPECTED_TIME.items():
        anchor = anchors[expected]
        if member not in recording.mtimes or anchor is None:
            continue
        member_time = datetime.fromtimestamp(recording.mtimes[member], timezone.utc)
        difference = (member_time - anchor).total_seconds()
        checks.append(
            MemberTimeCheck(member, expected, difference, abs(difference) <= tolerance)
        )
    return tuple(checks)


def _with_warnings(stats: ReportStats) -> ReportStats:
    """Attach the warnings derived from the statistics."""
    warnings: list[str] = []
    prompt_hosts = stats.prompt_identities
    if len(prompt_hosts) > 1:
        shown = ", ".join(f"{task:02d}-{host}" for task, host in prompt_hosts)
        warnings.append(f"Prompts show several task/host pairs: {shown}.")
    if stats.file_host is not None and prompt_hosts:
        expected = (stats.file_task, stats.file_host)
        if any(identity != expected for identity in prompt_hosts):
            warnings.append(
                f"File name says task {stats.file_task:02d}, host {stats.file_host}, "
                "but the prompts differ."
            )
    if stats.pastes:
        warnings.append(f"{len(stats.pastes)} input chunk(s) look pasted.")
    for check in stats.member_times:
        if not check.within_tolerance:
            warnings.append(
                f"{check.member} archive time differs from the recording "
                f"{check.expected} by {check.difference:+.1f} s."
            )
    if stats.diagnostics:
        warnings.append(f"{len(stats.diagnostics)} report problem(s) were found while reading.")
    return replace(stats, warnings=tuple(warnings))
