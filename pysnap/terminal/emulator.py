"""Terminal emulation utilities built on top of :mod:`pyte`."""

from __future__ import annotations

import pyte
from pyte.screens import HistoryScreen


class TerminalEmulator:
    """Wrap a :class:`pyte.Screen` and render it for the TUI."""

    def __init__(
        self,
        columns: int = 80,
        lines: int = 24,
        history: int = 5000,
    ) -> None:
        """Initialize the terminal emulator.

        :param columns: Initial terminal width.
        :param lines: Initial terminal height.
        :param history: Number of scrollback lines to keep locally.
        """
        self.screen = HistoryScreen(columns, lines, history=history)
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

    def scroll_up(self, lines: int = 1) -> None:
        """Scroll the local view toward older history lines.

        :param lines: Number of lines to move upward.
        """
        self._scroll_history_up(max(lines, 1))

    def scroll_down(self, lines: int = 1) -> None:
        """Scroll the local view toward newer history lines.

        :param lines: Number of lines to move downward.
        """
        self._scroll_history_down(max(lines, 1))

    def scroll_to_top(self) -> None:
        """Jump to the oldest locally retained history."""
        while self._scroll_history_up(self.screen.lines):
            continue

    def scroll_to_bottom(self) -> None:
        """Jump back to the live end of the terminal output."""
        while self._scroll_history_down(self.screen.lines):
            continue

    @property
    def is_scrollback_active(self) -> bool:
        """Return whether the current view is above the live output bottom."""
        return self.screen.history.position < self.screen.history.size

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

    def _scroll_history_up(self, lines: int) -> bool:
        """Move the local viewport upward through retained history.

        :param lines: Maximum number of lines to move.
        :returns: ``True`` when the viewport changed.
        """
        history = self.screen.history
        if history.position <= self.screen.lines or not history.top:
            return False

        amount = min(len(history.top), max(lines, 1), history.position - self.screen.lines)
        history.bottom.extendleft(
            self.screen.buffer[y]
            for y in range(self.screen.lines - 1, self.screen.lines - amount - 1, -1)
        )
        self.screen.history = history._replace(position=history.position - amount)

        for y in range(self.screen.lines - 1, amount - 1, -1):
            self.screen.buffer[y] = self.screen.buffer[y - amount]
        for y in range(amount - 1, -1, -1):
            self.screen.buffer[y] = self.screen.history.top.pop()

        self.screen.dirty = set(range(self.screen.lines))
        self.screen.after_event("prev_page")
        return True

    def _scroll_history_down(self, lines: int) -> bool:
        """Move the local viewport downward toward live output.

        :param lines: Maximum number of lines to move.
        :returns: ``True`` when the viewport changed.
        """
        history = self.screen.history
        if history.position >= history.size or not history.bottom:
            return False

        amount = min(len(history.bottom), max(lines, 1), history.size - history.position)
        history.top.extend(self.screen.buffer[y] for y in range(amount))
        self.screen.history = history._replace(position=history.position + amount)

        for y in range(self.screen.lines - amount):
            self.screen.buffer[y] = self.screen.buffer[y + amount]
        for y in range(self.screen.lines - amount, self.screen.lines):
            self.screen.buffer[y] = self.screen.history.bottom.popleft()

        self.screen.dirty = set(range(self.screen.lines))
        self.screen.after_event("next_page")
        return True

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
