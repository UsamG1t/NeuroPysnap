"""Build synthetic ``report`` archives for unit tests.

The builder reproduces the files written by ``report`` (``script -I -O -B
-T`` plus ``lscpu``) from a scripted session: keystrokes, their terminal echo
and command output. Expected values in tests are derived from the scenario,
not from real recordings.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import tarfile

DEFAULT_START_TIME = "2026-09-25 21:29:33+00:00"
DEFAULT_END_TIME = "2026-09-25 21:30:08+00:00"
DEFAULT_CPU_INFO = (
    "Architecture:                            x86_64\n"
    "Model name:                              Test CPU\n"
    "Hypervisor vendor:                       Oracle\n"
)
ARCHIVE_MTIME = 1790371773


@dataclass
class ArchiveEntry:
    """Describe one raw tar entry for hand-made archives.

    :param name: Entry name as stored in the archive.
    :param data: Regular-file content, ``None`` for non-regular entries.
    :param kind: ``tarfile`` entry type.
    :param link_target: Target of a symbolic or hard link.
    """

    name: str
    data: bytes | None = None
    kind: bytes = tarfile.REGTYPE
    link_target: str = ""


class ReportBuilder:
    """Record a scripted terminal session in the ``report`` file layout."""

    def __init__(
        self,
        *,
        task: int = 1,
        host: str = "first",
        user: str = "root",
        columns: int = 133,
        lines: int = 24,
        term: str = "vt220",
        tty: str = "/dev/ttyS0",
        start_time: str = DEFAULT_START_TIME,
    ) -> None:
        """Initialize an empty recording.

        :param task: Task number used in the prompt.
        :param host: Host name used in the prompt.
        :param user: User name used in the prompt.
        :param columns: Terminal width header.
        :param lines: Terminal height header.
        :param term: ``TERM`` header.
        :param tty: ``TTY`` header.
        :param start_time: ``START_TIME`` header.
        """
        self.task = task
        self.host = host
        self.user = user
        self.columns = columns
        self.lines = lines
        self.term = term
        self.tty = tty
        self.start_time = start_time
        self.elapsed = 0.0
        self.timing: list[str] = []
        self.input = bytearray()
        self.output_log = bytearray()
        self.both = bytearray()

    @property
    def prompt(self) -> str:
        """Return the prompt installed by ``report`` for this session."""
        return f"[{self.user}@{self.task:02d}-{self.host} ~]# "

    def output(self, data: bytes | str, delay: float = 0.001) -> ReportBuilder:
        """Record one output chunk.

        :param data: Output bytes or text.
        :param delay: Seconds since the previous chunk.
        :returns: The builder for chaining.
        """
        chunk = data.encode("utf-8") if isinstance(data, str) else data
        self.timing.append(f"O {delay:.6f} {len(chunk)}")
        self.output_log += chunk
        self.both += chunk
        self.elapsed += delay
        return self

    def key(
        self,
        data: bytes | str,
        delay: float = 0.1,
        echo: bytes | str | None = None,
    ) -> ReportBuilder:
        """Record one input chunk followed by its terminal echo.

        :param data: Input bytes or text.
        :param delay: Seconds since the previous chunk.
        :param echo: Echoed output; defaults to the input itself, ``""``
            records no echo.
        :returns: The builder for chaining.
        """
        chunk = data.encode("utf-8") if isinstance(data, str) else data
        self.timing.append(f"I {delay:.6f} {len(chunk)}")
        self.input += chunk
        self.both += chunk
        self.elapsed += delay
        echoed = chunk if echo is None else echo
        if echoed:
            self.output(echoed, delay=0.0002)
        return self

    def show_prompt(self, delay: float = 0.01) -> ReportBuilder:
        """Record the shell prompt with bracketed paste enabled.

        :param delay: Seconds since the previous chunk.
        :returns: The builder for chaining.
        """
        return self.output(f"\x1b[?2004h{self.prompt}", delay=delay)

    def type_text(self, text: str, delay: float = 0.1) -> ReportBuilder:
        """Type text key by key with echo.

        :param text: Typed characters.
        :param delay: Seconds between keystrokes.
        :returns: The builder for chaining.
        """
        for character in text:
            self.key(character, delay=delay)
        return self

    def enter(self, delay: float = 0.1) -> ReportBuilder:
        """Press Enter and record the shell leaving line-editing mode.

        :param delay: Seconds since the previous chunk.
        :returns: The builder for chaining.
        """
        return self.key("\r", delay=delay, echo="\r\n\x1b[?2004l\r")

    def run(
        self,
        command: str,
        output: str = "",
        *,
        key_delay: float = 0.1,
        think_delay: float = 1.0,
    ) -> ReportBuilder:
        """Type a command, run it and show the next prompt.

        :param command: Typed command line.
        :param output: Command output with ``\\n`` line breaks.
        :param key_delay: Seconds between keystrokes.
        :param think_delay: Seconds before the first keystroke.
        :returns: The builder for chaining.
        """
        if command:
            self.key(command[0], delay=think_delay)
            self.type_text(command[1:], delay=key_delay)
        self.enter(delay=key_delay)
        if output:
            self.output(output.replace("\n", "\r\n"), delay=0.01)
        return self.show_prompt()

    def signal(self, text: str, delay: float = 0.1) -> ReportBuilder:
        """Record a signal entry such as ``SIGWINCH ROWS=40 COLS=120``.

        :param text: Signal description.
        :param delay: Seconds since the previous chunk.
        :returns: The builder for chaining.
        """
        self.timing.append(f"S {delay:.6f} {text}")
        self.elapsed += delay
        return self

    def finish(self, exit_code: int = 0, *, cpu_info: str = DEFAULT_CPU_INFO) -> dict[str, bytes]:
        """Close the session with ``Ctrl-D`` and render the report files.

        :param exit_code: Recorded shell exit code.
        :param cpu_info: ``CPU.txt`` content.
        :returns: Report member names mapped to their content.
        """
        self.key("\x04", delay=0.5, echo="\x1b[?2004l\r\r\nexit\r\n")
        details = (
            f'TERM="{self.term}" TTY="{self.tty}" '
            f'COLUMNS="{self.columns}" LINES="{self.lines}"'
        )
        start_line = f"Script started on {self.start_time} [{details}]\n".encode()
        end_line = (
            f'\nScript done on {DEFAULT_END_TIME} [COMMAND_EXIT_CODE="{exit_code}"]\n'
        ).encode()
        headers = [
            f"H 0.000000 START_TIME {self.start_time}",
            f"H 0.000000 TERM {self.term}",
            f"H 0.000000 TTY {self.tty}",
            f"H 0.000000 COLUMNS {self.columns}",
            f"H 0.000000 LINES {self.lines}",
            "H 0.000000 SHELL /bin/bash",
            "H 0.000000 TIMING_LOG /root/.REPORT/TIME.txt",
            "H 0.000000 OUTPUT_LOG /root/.REPORT/BOTH.txt",
            "H 0.000000 INPUT_LOG /root/.REPORT/BOTH.txt",
        ]
        trailer = [
            f"H 0.000000 DURATION {self.elapsed:.6f}",
            f"H 0.000000 EXIT_CODE {exit_code}",
        ]
        return {
            "CPU.txt": cpu_info.encode("utf-8"),
            "IN.txt": start_line + bytes(self.input) + end_line,
            "OUT.txt": start_line + bytes(self.output_log) + end_line,
            "BOTH.txt": start_line + bytes(self.both) + end_line,
            "TIME.txt": ("\n".join(headers + self.timing + trailer) + "\n").encode(),
        }


def write_report(path: Path, members: dict[str, bytes]) -> Path:
    """Pack report members the way ``tar -C $BASE -czf NAME .`` does.

    :param path: Destination archive path.
    :param members: Member names mapped to their content.
    :returns: The destination path.
    """
    entries = [ArchiveEntry("./", kind=tarfile.DIRTYPE)]
    entries.extend(ArchiveEntry(f"./{name}", data) for name, data in members.items())
    return write_archive(path, entries)


def write_archive(path: Path, entries: list[ArchiveEntry]) -> Path:
    """Write a gzip-compressed tar archive with arbitrary raw entries.

    :param path: Destination archive path.
    :param entries: Entries to store, in order.
    :returns: The destination path.
    """
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.type = entry.kind
            info.mtime = ARCHIVE_MTIME
            info.linkname = entry.link_target
            payload = None
            if entry.data is not None:
                info.size = len(entry.data)
                payload = io.BytesIO(entry.data)
            archive.addfile(info, payload)
    return path
