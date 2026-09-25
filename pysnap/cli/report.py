"""``pysnap report`` subcommands."""

from __future__ import annotations

import argparse
import os
import re
from typing import Sequence, TextIO

from pysnap.core.service import PySnapService
from pysnap.report.models import (
    CellStyle,
    CommandRecord,
    Recording,
    StyledRun,
    Transcript,
    TranscriptLine,
)
from pysnap.report.recording import load_report
from pysnap.report.render import PROMPT_PATTERN, render_transcript

_ANSI_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_RESET = "\x1b[0m"

# Colors of the ``report`` prompt parts in ``pysnap report text``: the prompt
# is recorded without colors, so PySnap highlights it like a typical colored
# bash prompt.
PROMPT_USER_STYLE = CellStyle(fg="green", bold=True)
PROMPT_HOST_STYLE = CellStyle(fg="cyan", bold=True)
PROMPT_DIRECTORY_STYLE = CellStyle(fg="blue", bold=True)
PROMPT_MARK_STYLE = CellStyle(bold=True)
COMMAND_STYLE = CellStyle(bold=True)


def build_report_parser(stdout: TextIO, stderr: TextIO) -> argparse.ArgumentParser:
    """Build the ``pysnap report`` parser with its subcommands.

    :param stdout: Stream used for help output.
    :param stderr: Stream used for parser errors.
    :returns: Configured parser.
    """
    # ``pysnap.cli.app`` imports this module to dispatch ``report``, so the
    # shared parser class is imported lazily to avoid an import cycle.
    from pysnap.cli.app import CliArgumentParser

    parser = CliArgumentParser(
        prog="pysnap report",
        description="Read session reports recorded by the report utility.",
        stdout=stdout,
        stderr=stderr,
    )
    subcommands = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    subcommands.required = True

    text = subcommands.add_parser(
        "text",
        help="Print the session text with highlighted commands.",
        description=(
            "Print the text of a recorded session without timing. Commands "
            "entered after a prompt are highlighted."
        ),
        stdout=stdout,
        stderr=stderr,
    )
    text.add_argument("report", help="Report file, for example report.01.first.")
    text.add_argument(
        "--commands",
        action="store_true",
        help="Print only the list of entered commands.",
    )
    text.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Highlight output: auto (default) colors only a terminal.",
    )
    return parser


def run_report_command(
    arguments: Sequence[str],
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run ``pysnap report``.

    :param arguments: Arguments after ``report``.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    namespace = build_report_parser(stdout, stderr).parse_args(list(arguments))
    if namespace.subcommand == "text":
        return _run_text(namespace, stdout, stderr)
    return 1


def _run_text(namespace: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run ``pysnap report text``.

    :param namespace: Parsed arguments.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    recording = load_report(namespace.report)
    transcript = render_transcript(recording)
    _print_diagnostics(recording, transcript, stderr)
    if namespace.commands:
        print(format_command_list(transcript.commands), file=stdout)
        return 0
    color = _use_color(namespace.color, stdout)
    for line in format_transcript(transcript, color=color):
        print(line, file=stdout)
    return 0


def format_transcript(transcript: Transcript, *, color: bool) -> list[str]:
    """Render transcript lines, highlighting prompts and commands.

    :param transcript: Rendered session.
    :param color: Whether to emit ANSI styles.
    :returns: Output lines.
    """
    highlighted = {command.line: command for command in transcript.commands}
    continuation: set[int] = set()
    for command in transcript.commands:
        continuation.update(range(command.line + 1, command.output_start))

    rendered: list[str] = []
    for number, line in enumerate(transcript.lines):
        if not color:
            rendered.append(line.text)
        elif number in highlighted:
            rendered.append(_render_runs(_command_line_runs(line, highlighted[number])))
        elif number in continuation:
            rendered.append(_render_runs([StyledRun(line.text, COMMAND_STYLE)]))
        else:
            rendered.append(_render_runs(line.runs))
    return rendered


def format_command_list(commands: Sequence[CommandRecord]) -> str:
    """Render the numbered list of entered commands.

    :param commands: Commands in recording order.
    :returns: One line per command with its recording time.
    """
    if not commands:
        return "No commands found."
    width = len(str(len(commands)))
    rendered = []
    for command in commands:
        minutes, seconds = divmod(command.time, 60)
        line = f"{command.index + 1:>{width}}  {int(minutes):02d}:{seconds:05.2f}  {command.text}"
        if command.prompt_info is None and command.prompt:
            line += f"  [prompt: {command.prompt}]"
        rendered.append(line)
    return "\n".join(rendered)


def _command_line_runs(line: TranscriptLine, command: CommandRecord) -> list[StyledRun]:
    """Style the first line of a command: prompt parts and the input."""
    text = line.text
    prompt_text, input_text = text[: command.column], text[command.column:]
    runs: list[StyledRun] = []
    match = PROMPT_PATTERN.match(prompt_text.rstrip())
    if match is not None:
        runs.extend(
            [
                StyledRun("["),
                StyledRun(match["user"], PROMPT_USER_STYLE),
                StyledRun("@"),
                StyledRun(f"{match['task']}-{match['host']}", PROMPT_HOST_STYLE),
                StyledRun(" "),
                StyledRun(match["dir"], PROMPT_DIRECTORY_STYLE),
                StyledRun("]"),
                StyledRun(prompt_text.rstrip()[-1], PROMPT_MARK_STYLE),
                StyledRun(prompt_text[len(prompt_text.rstrip()):]),
            ]
        )
    else:
        runs.append(StyledRun(prompt_text))
    runs.append(StyledRun(input_text, COMMAND_STYLE))
    return runs


def _render_runs(runs: Sequence[StyledRun]) -> str:
    """Render styled runs with ANSI SGR sequences built from known values."""
    pieces: list[str] = []
    for run in runs:
        codes = _sgr_codes(run.style)
        if codes:
            pieces.append(f"\x1b[{';'.join(codes)}m{run.text}{_RESET}")
        else:
            pieces.append(run.text)
    return "".join(pieces)


def _sgr_codes(style: CellStyle) -> list[str]:
    """Translate a cell style into SGR parameters."""
    codes: list[str] = []
    if style.bold:
        codes.append("1")
    if style.italics:
        codes.append("3")
    if style.underscore:
        codes.append("4")
    if style.reverse:
        codes.append("7")
    if style.strikethrough:
        codes.append("9")
    codes.extend(_color_codes(style.fg, foreground=True))
    codes.extend(_color_codes(style.bg, foreground=False))
    return codes


def _color_codes(color: str, *, foreground: bool) -> list[str]:
    """Translate one ``pyte`` color into SGR parameters."""
    base, bright_base, extended = (30, 90, "38") if foreground else (40, 100, "48")
    name = color.lower()
    if name in _ANSI_COLORS:
        return [str(base + _ANSI_COLORS[name])]
    if name.startswith("bright") and name[6:] in _ANSI_COLORS:
        return [str(bright_base + _ANSI_COLORS[name[6:]])]
    if _HEX_COLOR.match(name):
        red, green, blue = (int(name[i:i + 2], 16) for i in (0, 2, 4))
        return [extended, "2", str(red), str(green), str(blue)]
    return []


def _use_color(mode: str, stdout: TextIO) -> bool:
    """Decide whether to emit ANSI styles."""
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stdout, "isatty", None)
    return bool(isatty and isatty())


def _print_diagnostics(recording: Recording, transcript: Transcript, stderr: TextIO) -> None:
    """Print report diagnostics as warnings."""
    for diagnostic in (*recording.diagnostics, *transcript.diagnostics):
        print(f"Warning: {diagnostic.message}", file=stderr)
