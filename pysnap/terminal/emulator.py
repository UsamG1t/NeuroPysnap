"""Terminal emulation utilities built on top of :mod:`pyte`."""

from __future__ import annotations

from typing import Iterable

import pyte


class TerminalEmulator:
    """Wrap a :class:`pyte.Screen` and render it for the TUI."""

    def __init__(self, columns: int = 80, lines: int = 24) -> None:
        """Initialize the terminal emulator.

        :param columns: Initial terminal width.
        :param lines: Initial terminal height.
        """
        self.screen = pyte.Screen(columns, lines)
        self.stream = pyte.ByteStream(self.screen)

    def feed(self, data: bytes) -> None:
        """Feed new bytes into the terminal parser.

        :param data: Raw bytes read from the VM serial socket.
        """
        self.stream.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        """Resize the virtual screen.

        :param columns: New terminal width.
        :param lines: New terminal height.
        """
        if columns < 1 or lines < 1:
            return
        if self.screen.columns == columns and self.screen.lines == lines:
            return
        self.screen.resize(lines=lines, columns=columns)

    def as_formatted_text(self) -> list[tuple[str, str]]:
        """Render the current screen buffer as prompt_toolkit fragments.

        :returns: A list of ``(style, text)`` fragments.
        """
        fragments: list[tuple[str, str]] = []
        cursor = self.screen.cursor
        for y in range(self.screen.lines):
            line = self.screen.buffer[y]
            for x in range(self.screen.columns):
                char = line[x]
                style_parts = self._style_parts(char)
                if not cursor.hidden and cursor.x == x and cursor.y == y:
                    style_parts.append("reverse")
                fragments.append((" ".join(style_parts), char.data))
            if y != self.screen.lines - 1:
                fragments.append(("", "\n"))
        return fragments

    def _style_parts(self, char: pyte.screens.Char) -> list[str]:
        """Translate a ``pyte`` character style into prompt_toolkit styles.

        :param char: Character cell from the virtual screen.
        :returns: Prompt-toolkit style tokens.
        """
        style_parts: list[str] = []
        if char.fg != "default":
            style_parts.append(f"fg:{char.fg}")
        if char.bg != "default":
            style_parts.append(f"bg:{char.bg}")
        if char.bold:
            style_parts.append("bold")
        if char.italics:
            style_parts.append("italic")
        if char.underscore:
            style_parts.append("underline")
        if char.strikethrough:
            style_parts.append("strike")
        if char.reverse:
            style_parts.append("reverse")
        return style_parts
