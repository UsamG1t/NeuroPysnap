"""``pysnap report`` subcommands."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from typing import Sequence, TextIO

from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError
from pysnap.report.highlight import highlight_transcript
from pysnap.report.models import (
    CellStyle,
    CommandRecord,
    Recording,
    StyledRun,
    Transcript,
)
from pysnap.report.player import DEFAULT_MAX_DELAY, PlaybackController, ReplayPlayer
from pysnap.report.recording import load_report
from pysnap.report.render import render_transcript

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
    text.add_argument(
        "--no-pager",
        action="store_true",
        help="Print directly even when the text is longer than the terminal.",
    )

    show = subcommands.add_parser(
        "show",
        help="Replay a report with its timing in a safe terminal view.",
        description=(
            "Replay a recorded session with its timing. Keys: Space pause, "
            "+/- speed, n/p next or previous command, Home/End start or end, "
            "q quit; while paused Alt+Up/Down scroll the history."
        ),
        stdout=stdout,
        stderr=stderr,
    )
    show.add_argument("report", help="Report file, for example report.01.first.")
    show.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Initial speed: 0.25, 0.5, 1, 2, 4, 8 or 16 (default 1).",
    )
    show.add_argument(
        "--max-delay",
        type=float,
        default=DEFAULT_MAX_DELAY,
        metavar="SECONDS",
        help=(
            "Shorten pauses longer than this, like scriptreplay -m "
            f"(default {DEFAULT_MAX_DELAY:g}; 0 keeps real pauses)."
        ),
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
    if namespace.subcommand == "show":
        return _run_show(namespace, stdout, stderr)
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
    if not namespace.no_pager and _needs_pager(len(transcript.lines), stdout):
        from pysnap.report.viewer import run_pager

        highlighted = highlight_transcript(transcript)
        if not color:
            highlighted = [(StyledRun("".join(run.text for run in runs)),) for runs in highlighted]
        run_pager(recording.source_name, highlighted)
        return 0
    for line in format_transcript(transcript, color=color):
        print(line, file=stdout)
    return 0


def _run_show(namespace: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    """Run ``pysnap report show``.

    :param namespace: Parsed arguments.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    :raises PySnapError: When the output is not an interactive terminal.
    """
    if not _is_terminal(stdout):
        raise PySnapError(
            "pysnap report show needs an interactive terminal; "
            "use pysnap report text for redirected output."
        )
    recording = load_report(namespace.report)
    transcript = render_transcript(recording)
    _print_diagnostics(recording, transcript, stderr)
    player = ReplayPlayer(recording, transcript, max_delay=namespace.max_delay)
    from pysnap.report.viewer import run_player

    run_player(PlaybackController(player, speed=namespace.speed), recording.source_name)
    return 0


def format_transcript(transcript: Transcript, *, color: bool) -> list[str]:
    """Render transcript lines, highlighting prompts and commands.

    :param transcript: Rendered session.
    :param color: Whether to emit ANSI styles.
    :returns: Output lines.
    """
    if not color:
        return [line.text for line in transcript.lines]
    return [_render_runs(runs) for runs in highlight_transcript(transcript)]


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
    return _is_terminal(stdout)


def _is_terminal(stream: TextIO) -> bool:
    """Return whether a stream is an interactive terminal."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _needs_pager(line_count: int, stdout: TextIO) -> bool:
    """Return whether ``text`` output should open the pager.

    The pager opens only for a terminal and only when the text does not fit
    on the screen.
    """
    if not _is_terminal(stdout):
        return False
    return line_count > shutil.get_terminal_size(fallback=(80, 24)).lines


def _print_diagnostics(recording: Recording, transcript: Transcript, stderr: TextIO) -> None:
    """Print report diagnostics as warnings."""
    for diagnostic in (*recording.diagnostics, *transcript.diagnostics):
        print(f"Warning: {diagnostic.message}", file=stderr)
