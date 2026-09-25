"""Highlighting of prompts and entered commands in a transcript.

The ``report`` prompt is recorded without colors, so PySnap styles its parts
like a typical colored bash prompt and shows entered commands in bold. The
result is a list of styled runs per line that any front end can draw.
"""

from __future__ import annotations

from pysnap.report.models import (
    CellStyle,
    CommandRecord,
    StyledRun,
    Transcript,
    TranscriptLine,
)
from pysnap.report.render import PROMPT_PATTERN

PROMPT_USER_STYLE = CellStyle(fg="green", bold=True)
PROMPT_HOST_STYLE = CellStyle(fg="cyan", bold=True)
PROMPT_DIRECTORY_STYLE = CellStyle(fg="blue", bold=True)
PROMPT_MARK_STYLE = CellStyle(bold=True)
COMMAND_STYLE = CellStyle(bold=True)


def highlight_transcript(transcript: Transcript) -> list[tuple[StyledRun, ...]]:
    """Return the transcript lines with highlighted prompts and commands.

    Lines without a command keep the styles produced by the guest.

    :param transcript: Rendered session.
    :returns: Styled runs for every transcript line.
    """
    first_lines = {command.line: command for command in transcript.commands}
    continuation: set[int] = set()
    for command in transcript.commands:
        continuation.update(range(command.line + 1, command.output_start))

    highlighted: list[tuple[StyledRun, ...]] = []
    for number, line in enumerate(transcript.lines):
        if number in first_lines:
            highlighted.append(_command_line_runs(line, first_lines[number]))
        elif number in continuation:
            highlighted.append((StyledRun(line.text, COMMAND_STYLE),))
        else:
            highlighted.append(line.runs)
    return highlighted


def _command_line_runs(line: TranscriptLine, command: CommandRecord) -> tuple[StyledRun, ...]:
    """Style the first line of a command: prompt parts and the input."""
    text = line.text
    prompt_text, input_text = text[: command.column], text[command.column:]
    runs: list[StyledRun] = []
    stripped = prompt_text.rstrip()
    match = PROMPT_PATTERN.match(stripped)
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
                StyledRun(stripped[-1], PROMPT_MARK_STYLE),
                StyledRun(prompt_text[len(stripped):]),
            ]
        )
    elif prompt_text:
        runs.append(StyledRun(prompt_text))
    runs.append(StyledRun(input_text, COMMAND_STYLE))
    return tuple(run for run in runs if run.text)
