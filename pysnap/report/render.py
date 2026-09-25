"""Replay a recording through ``pyte`` to obtain text and typed commands.

The rendered transcript is what a reader would have seen: every line that
scrolled off the screen or was wiped by ``clear``, followed by the final
screen. Command lines are recovered from the screen rather than from the raw
keystrokes, because the keystrokes contain line editing (Backspace, cursor
keys, history recall) that only the shell resolves.

A command is captured when Enter was pressed and the shell moves to the next
line: the output chunk after Enter is fed up to its first line feed, and the
logical input line is read from the screen at that moment. The input starts
where the cursor stood at the first keystroke of the line, so prompts of
other programs (``vtysh``, a remote shell) are handled the same way as the
prompt installed by ``report``.
"""

from __future__ import annotations

import re
from typing import Callable

import pyte
from pyte.screens import Char, Margins

from pysnap.report.models import (
    CellStyle,
    CommandRecord,
    PLAIN_STYLE,
    PromptInfo,
    Recording,
    ReportDiagnostic,
    StyledRun,
    Transcript,
    TranscriptLine,
)
from pysnap.terminal.emulator import is_invisible_character

DEFAULT_COLUMNS = 80
DEFAULT_LINES = 24
DEFAULT_MAX_LINES = 200_000
# Recorded sizes come from an untrusted report; a huge screen would make every
# rendered row expensive, so sizes are clamped.
DEFAULT_MAX_SCREEN_SIZE = 1000

DIAG_TRANSCRIPT_TRUNCATED = "transcript-truncated"
DIAG_SCREEN_SIZE = "screen-size"

# ``report`` installs ``PS1="[\u@NN-HOST \W]# "``.
PROMPT_PATTERN = re.compile(
    r"^\[(?P<user>[^@\]\s]+)@(?P<task>\d+)-(?P<host>[^\s\]]+) (?P<dir>[^\]]*)\][#$]$"
)
# The same prompt anywhere inside a line, used to skip keys typed ahead while
# a program was running: the shell prints its prompt after their echo.
_INLINE_PROMPT_PATTERN = re.compile(r"\[[^@\]\s]+@\d+-[^\s\]]+ [^\]]*\][#$] ")
_RESIZE_PATTERN = re.compile(r"\bROWS=(?P<rows>\d+)\s+COLS=(?P<cols>\d+)")
# Keys that abandon the current input line instead of submitting it.
_LINE_ABORT_KEYS = (b"\x03", b"\x04", b"\x1a")
# Characters that must never reach the viewer's terminal: C0/C1 controls and
# bidirectional overrides that could disguise the displayed text.
_UNSAFE_CHARACTERS = re.compile(
    "[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]"
)


class _CaptureScreen(pyte.Screen):
    """Hand every line that leaves the visible screen to a callback."""

    def __init__(
        self,
        columns: int,
        lines: int,
        on_line_removed: Callable[[dict[int, Char]], None],
    ) -> None:
        """Initialize the screen.

        :param columns: Screen width.
        :param lines: Screen height.
        :param on_line_removed: Receives each line leaving the top of the
            screen; it must copy the line because ``pyte`` reuses it.
        """
        super().__init__(columns, lines)
        self._on_line_removed = on_line_removed

    def draw(self, data: str) -> None:
        """Draw text, showing invisible characters as ``?``.

        ``pyte`` stops drawing the rest of a chunk at the first character
        without width that is not a combining mark (for example U+202E), so
        such characters are made visible instead of hiding the text after
        them.
        """
        super().draw("".join(_visible_character(char) for char in data))

    def index(self) -> None:
        """Scroll like ``pyte`` and keep the line leaving a full-screen scroll."""
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0:
            self._on_line_removed(self.buffer[top])
        super().index()

    def erase_in_display(self, how: int = 0, *args: object, **kwargs: object) -> None:
        """Keep the visible text before a full-screen erase such as ``clear``."""
        if how == 2:
            last = max(
                (y for y in range(self.lines) if _row_has_text(self.buffer[y])),
                default=-1,
            )
            for y in range(last + 1):
                self._on_line_removed(self.buffer[y])
        super().erase_in_display(how, *args, **kwargs)

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Keep the top lines that ``pyte`` drops when the screen shrinks.

        ``pyte`` restores the cursor to its old row after dropping lines, so
        the cursor is moved up with the content it was on, as terminals do.
        """
        dropped = 0
        if lines is not None and lines < self.lines:
            dropped = self.lines - lines
            for y in range(dropped):
                self._on_line_removed(self.buffer[y])
        cursor_row = self.cursor.y
        super().resize(lines, columns)
        if dropped:
            self.cursor.y = max(0, min(cursor_row - dropped, self.lines - 1))


class _Renderer:
    """Drive one replay of a recording."""

    def __init__(self, recording: Recording, max_lines: int, max_screen_size: int) -> None:
        """Prepare the virtual screen.

        :param recording: Parsed recording.
        :param max_lines: Largest number of transcript lines to keep.
        :param max_screen_size: Largest accepted screen width and height.
        """
        self.recording = recording
        self.max_lines = max_lines
        self.max_screen_size = max_screen_size
        self.removed: list[TranscriptLine] = []
        self.truncated = False
        self.diagnostics: list[ReportDiagnostic] = []
        self.screen = _CaptureScreen(
            self._clamp_size(recording.columns or DEFAULT_COLUMNS),
            self._clamp_size(recording.lines or DEFAULT_LINES),
            self._keep_line,
        )
        self.stream = pyte.ByteStream(self.screen)
        self.commands: list[dict] = []
        self.line_start: tuple[int, int] | None = None
        self.first_event = 0
        self.pending_enter: tuple[int, float] | None = None

    def run(self) -> Transcript:
        """Replay all events and build the transcript.

        :returns: Rendered transcript.
        """
        for index, event in enumerate(self.recording.events):
            if event.kind == "S":
                self._handle_signal(event.data)
            elif event.kind == "I":
                self._handle_input(index, event.time, event.data)
            else:
                self._handle_output(event.data)
        if self.pending_enter is not None:
            self._capture_command()

        lines = self.removed + [
            _freeze_row(self.screen.buffer[y], self.screen.columns)
            for y in range(self.screen.lines)
        ]
        while lines and not lines[-1].text:
            lines.pop()
        diagnostics = self.diagnostics
        if self.truncated or len(lines) > self.max_lines:
            lines = lines[: self.max_lines]
            diagnostics.append(
                ReportDiagnostic(
                    DIAG_TRANSCRIPT_TRUNCATED,
                    f"The transcript was cut after {self.max_lines} lines.",
                )
            )
        return Transcript(
            lines=tuple(lines),
            commands=self._finish_commands(len(lines)),
            diagnostics=tuple(diagnostics),
        )

    def _keep_line(self, row: dict[int, Char]) -> None:
        """Store a copy of a line that leaves the screen."""
        if len(self.removed) >= self.max_lines:
            self.truncated = True
            return
        self.removed.append(_freeze_row(row, self.screen.columns))

    def _handle_signal(self, data: bytes) -> None:
        """Apply a recorded terminal resize."""
        size = parse_resize(data)
        if size is not None:
            self.screen.resize(self._clamp_size(size[0]), self._clamp_size(size[1]))

    def _clamp_size(self, size: int) -> int:
        """Limit a recorded screen dimension, reporting the first clamp."""
        if size <= self.max_screen_size:
            return size
        if not any(item.code == DIAG_SCREEN_SIZE for item in self.diagnostics):
            self.diagnostics.append(
                ReportDiagnostic(
                    DIAG_SCREEN_SIZE,
                    f"A recorded screen size of {size} was limited to "
                    f"{self.max_screen_size}.",
                )
            )
        return self.max_screen_size

    def _handle_input(self, index: int, time: float, data: bytes) -> None:
        """Track where the current input line starts and when it is submitted."""
        if self.pending_enter is not None:
            # Enter was pressed but the shell has not moved on yet, for
            # example when keys are typed ahead: capture what is on screen.
            self._capture_command()
        if self.line_start is None:
            self.line_start = (self._absolute_row(), self.screen.cursor.x)
            self.first_event = index
        if b"\r" in data or b"\n" in data:
            self.pending_enter = (index, time)
        elif any(key in data for key in _LINE_ABORT_KEYS):
            self.line_start = None

    def _handle_output(self, data: bytes) -> None:
        """Feed output, capturing a submitted command before its line feed."""
        if self.pending_enter is not None:
            newline = data.find(b"\n")
            if newline >= 0:
                self.stream.feed(data[:newline])
                self._capture_command()
                data = data[newline:]
        self.stream.feed(data)

    def _capture_command(self) -> None:
        """Read the submitted input line from the screen."""
        enter_event, time = self.pending_enter or (0, 0.0)
        self.pending_enter = None
        if self.truncated:
            # Row numbers are no longer reliable once lines are discarded.
            self.line_start = None
            return
        cursor_row = self._absolute_row()
        start_row, start_column = self.line_start or (cursor_row, 0)
        self.line_start = None

        first_row = cursor_row
        while first_row > start_row and len(self._row_text(first_row - 1)) >= self.screen.columns:
            first_row -= 1
        if first_row != start_row:
            # The remembered start is not part of the submitted line (keys
            # were typed while a program was running); fall back to the
            # beginning of the logical line and a known prompt, if any.
            start_row, start_column = first_row, _prompt_width(self._row_text(first_row))

        first_text = self._row_text(start_row)
        for match in _INLINE_PROMPT_PATTERN.finditer(first_text, start_column):
            start_column = match.end()
        pieces = [first_text[start_column:]]
        pieces.extend(self._row_text(row) for row in range(start_row + 1, cursor_row + 1))
        text = "".join(pieces).strip()
        if not text:
            return
        prompt = first_text[:start_column].rstrip()
        self.commands.append(
            {
                "time": time,
                "line": start_row,
                "column": start_column,
                "prompt": prompt,
                "text": text,
                "prompt_info": parse_prompt(prompt),
                "first_event": self.first_event,
                "enter_event": enter_event,
                "last_row": max(cursor_row, start_row),
            }
        )

    def _absolute_row(self) -> int:
        """Return the transcript index of the cursor row."""
        return len(self.removed) + self.screen.cursor.y

    def _row_text(self, row: int) -> str:
        """Return the unstripped text of a transcript row by absolute index."""
        if row < len(self.removed):
            return self.removed[row].text.ljust(self.screen.columns)
        line = self.screen.buffer[row - len(self.removed)]
        return "".join(line[x].data for x in range(self.screen.columns))

    def _finish_commands(self, line_count: int) -> tuple[CommandRecord, ...]:
        """Attach output line ranges to the captured commands."""
        records: list[CommandRecord] = []
        for position, command in enumerate(self.commands):
            next_line = (
                self.commands[position + 1]["line"]
                if position + 1 < len(self.commands)
                else line_count
            )
            output_start = min(command["last_row"] + 1, line_count)
            records.append(
                CommandRecord(
                    index=position,
                    time=command["time"],
                    line=command["line"],
                    column=command["column"],
                    prompt=command["prompt"],
                    text=command["text"],
                    prompt_info=command["prompt_info"],
                    first_event=command["first_event"],
                    enter_event=command["enter_event"],
                    output_start=output_start,
                    output_end=max(output_start, min(next_line, line_count)),
                )
            )
        return tuple(records)


def render_transcript(
    recording: Recording,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_screen_size: int = DEFAULT_MAX_SCREEN_SIZE,
) -> Transcript:
    """Replay a recording and return its transcript and commands.

    :param recording: Parsed recording.
    :param max_lines: Largest number of transcript lines to keep.
    :param max_screen_size: Largest accepted screen width and height.
    :returns: Rendered transcript.
    """
    return _Renderer(recording, max_lines, max_screen_size).run()


def parse_resize(data: bytes) -> tuple[int, int] | None:
    """Parse a ``SIGWINCH ROWS=<n> COLS=<n>`` signal entry.

    :param data: Signal description from an ``S`` timing entry.
    :returns: ``(rows, columns)`` or ``None`` for other or invalid signals.
    """
    match = _RESIZE_PATTERN.search(data.decode("ascii", errors="replace"))
    if match is None or int(match["rows"]) <= 0 or int(match["cols"]) <= 0:
        return None
    return int(match["rows"]), int(match["cols"])


def parse_prompt(prompt: str) -> PromptInfo | None:
    """Parse the prompt installed by ``report``.

    :param prompt: Prompt text without trailing spaces.
    :returns: Prompt parts or ``None`` for any other prompt.
    """
    match = PROMPT_PATTERN.match(prompt)
    if match is None:
        return None
    return PromptInfo(
        user=match["user"],
        task=int(match["task"]),
        host=match["host"],
        directory=match["dir"],
    )


def _prompt_width(row_text: str) -> int:
    """Return the width of a ``report`` prompt at the start of a row, or 0."""
    match = _INLINE_PROMPT_PATTERN.match(row_text)
    return match.end() if match else 0


def _visible_character(char: str) -> str:
    """Replace a character ``pyte`` would silently drop with ``?``."""
    return "?" if is_invisible_character(char) else char


def _row_has_text(row: dict[int, Char]) -> bool:
    """Return whether a screen row holds any visible character."""
    return any(char.data.strip() for char in row.values())


def _freeze_row(row: dict[int, Char], columns: int) -> TranscriptLine:
    """Copy a screen row into an immutable transcript line.

    Trailing blanks without a visible background are dropped, unsafe
    characters are replaced by ``?`` and wide-character placeholders are
    skipped.

    :param row: ``pyte`` screen row.
    :param columns: Screen width.
    :returns: Transcript line.
    """
    cells: list[tuple[str, CellStyle]] = []
    for x in range(columns):
        char = row[x]
        if char.data == "":
            continue  # Right half of a wide character.
        cells.append((_UNSAFE_CHARACTERS.sub("?", char.data), _style_of(char)))
    while cells and cells[-1][0] == " " and _is_invisible_blank(cells[-1][1]):
        cells.pop()

    runs: list[StyledRun] = []
    for text, style in cells:
        if runs and runs[-1].style == style:
            runs[-1] = StyledRun(runs[-1].text + text, style)
        else:
            runs.append(StyledRun(text, style))
    return TranscriptLine(tuple(runs))


def _style_of(char: Char) -> CellStyle:
    """Convert ``pyte`` character attributes into a :class:`CellStyle`."""
    if (
        char.fg == "default"
        and char.bg == "default"
        and not (char.bold or char.italics or char.underscore)
        and not (char.strikethrough or char.reverse)
    ):
        return PLAIN_STYLE
    return CellStyle(
        fg=char.fg,
        bg=char.bg,
        bold=char.bold,
        italics=char.italics,
        underscore=char.underscore,
        strikethrough=char.strikethrough,
        reverse=char.reverse,
    )


def _is_invisible_blank(style: CellStyle) -> bool:
    """Return whether a blank cell with this style shows nothing."""
    return style.bg == "default" and not (style.reverse or style.underscore)
