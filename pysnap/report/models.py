"""Data contracts shared by the report reading modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

# ``report`` names its archives ``report.<NN>.<host>`` where ``NN`` is the
# task number printed with ``%02d``. The host part may itself contain dots.
_REPORT_NAME_PATTERN = re.compile(r"^report\.(?P<task>\d+)\.(?P<host>.+)$")


@dataclass(frozen=True)
class ReportLimits:
    """Bound the resources spent on one untrusted report archive.

    :param max_archive_bytes: Largest accepted compressed archive size.
    :param max_member_bytes: Largest accepted size of one archive member.
    :param max_unpacked_bytes: Largest amount of decompressed data read from
        the archive, including skipped members and tar headers.
    """

    max_archive_bytes: int = 16 * 1024 * 1024
    max_member_bytes: int = 64 * 1024 * 1024
    max_unpacked_bytes: int = 128 * 1024 * 1024


@dataclass(frozen=True)
class ReportDiagnostic:
    """Describe one non-fatal problem found in a report.

    :param code: Stable machine-readable problem identifier.
    :param message: Human-readable explanation.
    """

    code: str
    message: str


@dataclass(frozen=True)
class ReportName:
    """Represent the task number and host encoded in a report file name."""

    task: int
    host: str

    @classmethod
    def parse(cls, file_name: str) -> ReportName | None:
        """Parse a ``report.<NN>.<host>`` file name.

        :param file_name: Base name of the report file.
        :returns: Parsed name or ``None`` when the name does not follow the
            ``report`` convention.
        """
        match = _REPORT_NAME_PATTERN.match(file_name)
        if match is None:
            return None
        return cls(task=int(match["task"]), host=match["host"])


@dataclass(frozen=True)
class ReportArchive:
    """Hold the raw members read from a report archive.

    :param members: Known member names mapped to their content.
    :param mtimes: Known member names mapped to their archive modification
        time in seconds since the epoch.
    :param diagnostics: Problems found while reading the archive.
    """

    members: dict[str, bytes]
    mtimes: dict[str, int]
    diagnostics: tuple[ReportDiagnostic, ...] = ()


@dataclass(frozen=True)
class TimingEvent:
    """Represent one input or output chunk of the recorded session.

    :param kind: ``"I"`` for keyboard input or ``"O"`` for terminal output.
    :param delay: Seconds elapsed since the previous chunk.
    :param time: Seconds elapsed since the start of the recording.
    :param data: Raw bytes of the chunk.
    """

    kind: str
    delay: float
    time: float
    data: bytes


@dataclass(frozen=True)
class Recording:
    """Represent a parsed session report.

    :param source_name: Base name of the report file.
    :param name: Task and host parsed from the file name, when available.
    :param headers: Raw ``H`` header values from the timing log.
    :param start_time: Recording start reported by ``script``.
    :param duration: Recording duration in seconds reported by ``script``.
    :param exit_code: Exit code of the recorded shell.
    :param columns: Terminal width at recording start.
    :param lines: Terminal height at recording start.
    :param events: Input and output chunks in recording order.
    :param input_bytes: Complete recorded keyboard input.
    :param output_bytes: Complete recorded terminal output.
    :param cpu_info: Content of ``CPU.txt`` (``lscpu`` output).
    :param mtimes: Archive modification times of the known members.
    :param diagnostics: Non-fatal problems found in the report.
    """

    source_name: str
    name: ReportName | None
    headers: dict[str, str]
    start_time: datetime | None
    duration: float | None
    exit_code: int | None
    columns: int | None
    lines: int | None
    events: tuple[TimingEvent, ...]
    input_bytes: bytes
    output_bytes: bytes
    cpu_info: str = ""
    mtimes: dict[str, int] = field(default_factory=dict)
    diagnostics: tuple[ReportDiagnostic, ...] = ()


@dataclass(frozen=True)
class CellStyle:
    """Represent the visual attributes of rendered terminal text.

    Colors keep the ``pyte`` notation: ``"default"``, an ANSI color name such
    as ``"red"`` or ``"brightred"``, or a six-digit hex value.
    """

    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False


PLAIN_STYLE = CellStyle()


@dataclass(frozen=True)
class StyledRun:
    """Represent consecutive characters that share one style."""

    text: str
    style: CellStyle = PLAIN_STYLE


@dataclass(frozen=True)
class TranscriptLine:
    """Represent one rendered line of the session transcript."""

    runs: tuple[StyledRun, ...] = ()

    @property
    def text(self) -> str:
        """Return the plain text of the line."""
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True)
class PromptInfo:
    """Represent the parts of the prompt installed by ``report``.

    ``report`` sets ``PS1="[\\u@NN-HOST \\W]# "``.
    """

    user: str
    task: int
    host: str
    directory: str


@dataclass(frozen=True)
class CommandRecord:
    """Represent one input line submitted with Enter.

    :param index: Zero-based position among all recorded commands.
    :param time: Recording time of the Enter key press in seconds.
    :param line: Transcript line where the input starts.
    :param column: Column where the input starts, i.e. the prompt width.
    :param prompt: Text left of the input on its first line.
    :param text: Submitted input as displayed by the shell.
    :param prompt_info: Parsed ``report`` prompt, ``None`` for other prompts
        such as ``vtysh`` or a remote shell.
    :param first_event: Index of the first input event of this line.
    :param enter_event: Index of the input event that contained Enter.
    :param output_start: First transcript line after the input.
    :param output_end: Transcript line after the last output line.
    """

    index: int
    time: float
    line: int
    column: int
    prompt: str
    text: str
    prompt_info: PromptInfo | None
    first_event: int
    enter_event: int
    output_start: int
    output_end: int


@dataclass(frozen=True)
class Transcript:
    """Represent the rendered session: final text and recognized commands."""

    lines: tuple[TranscriptLine, ...]
    commands: tuple[CommandRecord, ...]
    diagnostics: tuple[ReportDiagnostic, ...] = ()
