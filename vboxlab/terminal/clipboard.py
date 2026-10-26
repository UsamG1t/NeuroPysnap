"""Clipboard helpers for copying text out of the terminal session."""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys

from prompt_toolkit.output.base import Output


def copy_text_to_host_clipboard(text: str, output: Output | None = None) -> bool:
    """Copy text to the host clipboard.

    The function first tries native platform clipboard commands. When these are
    unavailable, it falls back to the OSC 52 terminal clipboard sequence for
    terminals that support it.

    :param text: Text to copy.
    :param output: Prompt-toolkit output used for the OSC 52 fallback.
    :returns: ``True`` when one copy strategy succeeded.
    """
    if not text:
        return False

    if _copy_with_platform_command(text):
        return True
    if output is None:
        return False

    output.write_raw(_osc52_payload(text))
    output.flush()
    return True


def _copy_with_platform_command(text: str) -> bool:
    """Copy text using a platform-specific clipboard command."""
    for command in _platform_clipboard_commands():
        executable = command[0]
        if shutil.which(executable) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


def _platform_clipboard_commands() -> tuple[tuple[str, ...], ...]:
    """Return clipboard commands to try on the current host platform."""
    if sys.platform == "win32":
        return (("clip.exe",), ("clip",))
    if sys.platform == "darwin":
        return (("pbcopy",),)
    return (
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
    )


def _osc52_payload(text: str) -> str:
    """Encode text as an OSC 52 clipboard sequence."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{encoded}\x07"
