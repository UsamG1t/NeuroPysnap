"""Read the host clipboard for the ``Ctrl-]`` ``v`` paste of ``vboxlab connect``.

The clipboard is read only when the user asks for it. Copying and the usual
paste are left to the terminal of the user; this paste helps where the
terminal offers none from the keyboard, such as the Windows console without
its Ctrl key shortcuts.

- Windows: the Win32 clipboard API through ``ctypes``, no external program;
- macOS: ``pbpaste``;
- Linux and other systems: ``wl-paste`` under Wayland, then ``xclip`` or
  ``xsel`` for the X11 clipboard.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

READ_TIMEOUT = 3.0
_CF_UNICODETEXT = 13


class ClipboardError(Exception):
    """Report that the host clipboard cannot be read."""


def read_host_clipboard() -> str:
    """Return the text in the host clipboard.

    :returns: Clipboard text; empty when the clipboard holds no text.
    :raises ClipboardError: When no way to read the clipboard works.
    """
    if sys.platform == "win32":
        return _read_windows_clipboard()
    commands = _clipboard_commands()
    available = [command for command in commands if shutil.which(command[0])]
    if not available:
        names = ", ".join(command[0] for command in commands)
        raise ClipboardError(f"no clipboard program found; install one of: {names}")
    errors = []
    for command in available:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=READ_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{command[0]}: {error}")
            continue
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
        message = result.stderr.decode("utf-8", errors="replace").strip()
        errors.append(f"{command[0]}: {message or f'exit status {result.returncode}'}")
    raise ClipboardError("; ".join(errors))


def _clipboard_commands() -> list[tuple[str, ...]]:
    """Return the programs that print the clipboard, in the order to try."""
    if sys.platform == "darwin":
        return [("pbpaste",)]
    commands = [
        ("xclip", "-selection", "clipboard", "-o"),
        ("xsel", "--clipboard", "--output"),
    ]
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.insert(0, ("wl-paste", "--no-newline"))
    return commands


def _read_windows_clipboard() -> str:  # pragma: no cover - Windows only
    """Return the Unicode text of the Windows clipboard."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        raise ClipboardError("the clipboard is in use by another program")
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("the clipboard data cannot be locked")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
