"""``pysnap report`` subcommands."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
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
from pysnap.report.checkfile import load_check
from pysnap.report.extract import extract_file
from pysnap.report.matcher import CheckResult, run_check
from pysnap.report.stats import ReportStats, compute_stats

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

    check = subcommands.add_parser(
        "check",
        help="Show report information and check it against a check file.",
        description=(
            "Show where and when a report was recorded, its commands, pauses, "
            "typing, pasted input, addresses and integrity checks. With a "
            "check file, also check the expected commands and output blocks "
            "and grade the report."
        ),
        stdout=stdout,
        stderr=stderr,
    )
    check.add_argument("report", help="Report file, for example report.01.first.")
    check.add_argument(
        "check_file",
        nargs="?",
        metavar="CHECK",
        help="Check file in TOML format, for example lab02-first.check.toml.",
    )

    extract = subcommands.add_parser(
        "extract",
        help="Copy a report file from a running VM through its serial console.",
        description=(
            "Copy a file from a running VM to the host through the UART1 serial "
            "console. The VM must be at a shell prompt, without a running report "
            "recording. An attached pysnap connect session runs the transfer on "
            "its own connection. A name without / is looked up in the home "
            "directory of the console user."
        ),
        stdout=stdout,
        stderr=stderr,
    )
    extract.add_argument("vm", help="Virtual machine name.")
    extract.add_argument("name", help="Report file in the VM, for example report.01.first.")
    extract.add_argument(
        "--output",
        metavar="PATH",
        help="Host file to write; defaults to the same name in the current directory.",
    )
    extract.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing host file.",
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
    if namespace.subcommand == "check":
        return _run_check(namespace, stdout)
    if namespace.subcommand == "extract":
        return _run_extract(namespace, service, stdout, stderr)
    return 1


def _run_extract(
    namespace: argparse.Namespace,
    service: PySnapService,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run ``pysnap report extract``.

    :param namespace: Parsed arguments.
    :param service: Application service.
    :param stdout: Output stream.
    :param stderr: Error stream.
    :returns: Process exit code.
    """
    result = extract_file(
        service,
        namespace.vm,
        namespace.name,
        destination=Path(namespace.output) if namespace.output else None,
        force=namespace.force,
    )
    print(
        f"Extracted {result.remote_path} from {result.vm_name} to {result.destination} "
        f"({result.size} bytes, sha256 {result.sha256}).",
        file=stdout,
    )
    try:
        load_report(result.destination)
    except PySnapError as error:
        print(f"Warning: the file is not a readable report: {error}", file=stderr)
    return 0


def _run_check(namespace: argparse.Namespace, stdout: TextIO) -> int:
    """Run ``pysnap report check``.

    :param namespace: Parsed arguments.
    :param stdout: Output stream.
    :returns: Process exit code.
    """
    spec = load_check(namespace.check_file) if namespace.check_file else None
    recording = load_report(namespace.report)
    transcript = render_transcript(recording)
    print(format_report_stats(compute_stats(recording, transcript)), file=stdout)
    if spec is not None:
        result = run_check(spec, transcript, report_name=recording.name)
        print("", file=stdout)
        print(format_check_result(result, transcript), file=stdout)
    return 0


def format_check_result(result: CheckResult, transcript: Transcript) -> str:
    """Render the per-item results and the grade.

    :param result: Graded check result.
    :param transcript: Rendered report, used to show what matched.
    :returns: Multi-line human-readable text.
    """
    lines = [f"Checks: {result.check_name}"]
    for item in result.items:
        state = "PASS" if item.passed else "FAIL"
        pattern_lines = item.pattern.split("\n")
        head = f"  {state}  {item.kind} {item.number}"
        if item.kind == "command":
            label = f" [{item.label}]" if item.label != item.pattern else ""
            lines.append(f"{head}{label}: {item.pattern}")
            if item.command is not None:
                matched = transcript.commands[item.command]
                lines.append(f"        matched command {item.command + 1}: {matched.text}")
        else:
            count = f" (found {item.count} times)" if item.passed and item.count > 1 else ""
            lines.append(f"{head}{count}:")
            lines += [f"          {line}" for line in pattern_lines]
            if item.lines:
                lines.append(f"        first match at transcript line {item.lines[0] + 1}")
        if not item.passed:
            lines.append(f"        reason: {item.reason}")
            if item.closest and item.kind == "command":
                lines.append(f"        closest entered command: {item.closest}")
    if result.bindings:
        values = ", ".join(f"{name}={value}" for name, value in result.bindings.items())
        lines.append(f"  Values: {values}")
    passed = sum(1 for item in result.items if item.passed)
    mark = f", mark {result.mark}" if result.mark is not None else ""
    lines += [
        "",
        f"Result: {passed} of {len(result.items)} checks passed, "
        f"{result.points:g} of {result.total_points:g} points "
        f"({result.percent:.1f}%){mark}",
    ]
    if result.search_exhausted:
        lines.append(
            "WARNING: The check file allows too many combinations; "
            "the best result found within the search limit is shown."
        )
    lines += [f"WARNING: {warning}" for warning in result.warnings]
    return "\n".join(lines)


def format_report_stats(stats: ReportStats) -> str:
    """Render the report information block.

    :param stats: Report statistics.
    :returns: Multi-line human-readable text.
    """
    lines = [f"Report information: {stats.source_name}", "", "Identity"]
    file_identity = (
        f"task {stats.file_task:02d}, host {stats.file_host}"
        if stats.file_host is not None
        else "not a report.NN.HOST name"
    )
    prompt_identity = (
        ", ".join(f"task {task:02d}, host {host}" for task, host in stats.prompt_identities)
        or "no report prompt found"
    )
    lines += [
        f"  File name:  {file_identity}",
        f"  Prompt:     {prompt_identity}",
        "",
        "Timing",
        f"  Started:    {_format_datetime(stats.start_time)}",
        f"  Finished:   {_format_datetime(stats.end_time)}",
        f"  Duration:   {_format_seconds(stats.duration)}",
        f"  Exit code:  {_format_value(stats.exit_code)}",
        "",
        "Environment",
        f"  Terminal:   {_format_value(stats.tty)}, {_format_value(stats.term)}, "
        f"{_format_value(stats.columns)}x{_format_value(stats.lines)}",
        f"  CPU:        {_format_value(stats.cpu_model)}",
        f"  Hypervisor: {_format_value(stats.hypervisor)}",
        "",
        "Commands",
        f"  Entered:    {len(stats.commands)} ({stats.unique_commands} unique)",
        f"  Interrupted with Ctrl-C: {_count(stats.commands, 'interrupted')}",
        f"  Recalled from history:   {_count(stats.commands, 'from_history')}",
        f"  At other prompts:        {_count(stats.commands, 'foreign_prompt')}",
    ]
    if stats.commands:
        lines.append(
            f"  Pauses before commands: min {_format_seconds(stats.pause_min)}, "
            f"median {_format_seconds(stats.pause_median)}, "
            f"max {_format_seconds(stats.pause_max)} (before: {stats.longest_pause_command})"
        )
        width = len(str(len(stats.commands)))
        for item in stats.commands:
            marks = [
                mark
                for enabled, mark in (
                    (item.interrupted, "interrupted"),
                    (item.from_history, "history"),
                    (item.foreign_prompt, "other prompt"),
                )
                if enabled
            ]
            suffix = f"  [{', '.join(marks)}]" if marks else ""
            lines.append(
                f"  {item.index + 1:>{width}}. pause {_format_seconds(item.pause):>7}  "
                f"{item.text}{suffix}"
            )
    lines += [
        "",
        "Typing",
        f"  Speed:      {_format_rate(stats.typing_speed)}",
        f"  Backspace:  {_format_share(stats.backspace_share)}",
        f"  Pasted input: {len(stats.pastes)}",
    ]
    for paste in stats.pastes:
        owner = f"command {paste.command + 1}" if paste.command is not None else "no command"
        lines.append(f"    at {_format_seconds(paste.time)} ({owner}): {paste.text[:60]}")
    lines += [
        "",
        "Addresses in output",
        f"  IPv4:       {', '.join(stats.ip_addresses) or 'none'}",
        f"  MAC:        {', '.join(stats.mac_addresses) or 'none'}",
        "",
        "Integrity",
        f"  Report problems: {len(stats.diagnostics)}",
    ]
    lines += [f"    {diagnostic.message}" for diagnostic in stats.diagnostics]
    for check in stats.member_times:
        state = "ok" if check.within_tolerance else "MISMATCH"
        lines.append(
            f"  {check.member:<9} time vs recording {check.expected}: "
            f"{check.difference:+.1f} s ({state})"
        )
    if stats.warnings:
        lines += ["", *[f"WARNING: {warning}" for warning in stats.warnings]]
    return "\n".join(lines)


def _count(items, attribute: str) -> int:
    """Count items whose boolean attribute is set."""
    return sum(1 for item in items if getattr(item, attribute))


def _format_value(value) -> str:
    """Format an optional value."""
    return "unknown" if value is None else str(value)


def _format_datetime(value) -> str:
    """Format an optional timestamp with its UTC offset."""
    return "unknown" if value is None else value.isoformat(sep=" ", timespec="seconds")


def _format_seconds(value: float | None) -> str:
    """Format an optional duration in seconds."""
    return "unknown" if value is None else f"{value:.2f} s"


def _format_rate(value: float | None) -> str:
    """Format an optional typing speed."""
    return "unknown" if value is None else f"{value:.2f} keys/s"


def _format_share(value: float | None) -> str:
    """Format an optional share as a percentage."""
    return "unknown" if value is None else f"{value * 100:.1f}% of keys"


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
