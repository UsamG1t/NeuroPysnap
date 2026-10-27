"""Terminal emulation utilities built on top of :mod:`pyte`."""

from __future__ import annotations

from collections import defaultdict
import copy
import re
import unicodedata

import pyte
# ``pyte`` decides what to draw with this cached ``wcwidth``; reusing it keeps
# the rule below identical to the one inside ``Screen.draw``.
from pyte.screens import HistoryScreen, Margins, StaticDefaultDict, wcwidth

_HEX_COLOR_RE = re.compile(r"^[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
_ANSI_BRIGHT_COLOR_ALIASES = {
    "brightblack": "ansibrightblack",
    "brightred": "ansibrightred",
    "brightgreen": "ansibrightgreen",
    "brightbrown": "ansibrightyellow",
    "brightyellow": "ansibrightyellow",
    "brightblue": "ansibrightblue",
    "brightmagenta": "ansibrightmagenta",
    "brightcyan": "ansibrightcyan",
    "brightwhite": "ansiwhite",
    "bfightmagenta": "ansibrightmagenta",
}


_ESC = 0x1B
_BEL = 0x07
_CAN_OR_SUB = (0x18, 0x1A)
# ``ESC`` followed by one of these opens a control string: OSC ``]``, DCS
# ``P``, SOS ``X``, PM ``^`` and APC ``_``.
_STRING_INTRODUCERS = b"]PX^_"
# Private CSI sequences that ``pyte`` implements: DEC modes and selective
# erase.
_PRIVATE_FINALS = b"hlJK"
_MAX_CSI_LENGTH = 256
# A control string that never ends would hide all further output; after this
# many bytes the rest is shown again.
_MAX_STRING_LENGTH = 65536


class SequenceFilter:
    """Remove control sequences ``pyte`` cannot handle before it parses them.

    ``pyte`` 0.8 fails on some sequences that current programs send, for
    example editors such as Vim with an xterm ``TERM``:

    - private CSI sequences other than DEC modes, such as ``ESC [ ? 4 m``,
      raise ``TypeError``;
    - ``>``, ``=`` and ``<`` prefixes are ignored, so ``ESC [ > 4 ; 2 m``
      switches on underline;
    - intermediate bytes (``ESC [ 0 % m``) and colon sub-parameters
      (``ESC [ 3 8 : 2 : : 1 : 2 : 3 m``) end the sequence early and the rest
      is drawn as text;
    - the payload of DCS, SOS, PM and APC strings is drawn as text.

    The filter keeps the sequences ``pyte`` handles, rewrites colon SGR
    parameters to the semicolon form and drops the rest, together with all
    control strings (including OSC: window titles are not used). Its state
    survives chunk boundaries, so a sequence split between two reads is
    handled like a whole one.
    """

    def __init__(self) -> None:
        """Start in the ground state."""
        self._state = "ground"
        self._sequence = bytearray()
        self._string_length = 0

    def feed(self, data: bytes) -> bytes:
        """Return the bytes of ``data`` that ``pyte`` may parse.

        :param data: Raw guest output.
        :returns: Filtered output; parts of an unfinished sequence are kept
            until the next call.
        """
        output = bytearray()
        for byte in data:
            state = self._state
            if state == "ground":
                if byte == _ESC:
                    self._begin_escape()
                else:
                    output.append(byte)
            elif state == "escape":
                if byte == ord("["):
                    self._sequence.append(byte)
                    self._state = "csi"
                elif byte in _STRING_INTRODUCERS:
                    self._begin_string()
                elif byte == _ESC:
                    output += self._sequence
                    self._begin_escape()
                else:
                    output += self._sequence
                    output.append(byte)
                    self._reset()
            elif state in ("csi", "csi-ignore"):
                self._feed_csi(byte, output)
            else:
                self._feed_string(byte, output)
        return bytes(output)

    def _feed_csi(self, byte: int, output: bytearray) -> None:
        """Collect one CSI byte and pass or drop a finished sequence."""
        if byte == _ESC:
            self._begin_escape()
        elif byte in _CAN_OR_SUB:
            self._reset()
        elif self._state == "csi-ignore":
            if 0x40 <= byte <= 0x7E:
                self._reset()
        elif byte < 0x20:
            # C0 controls inside a sequence act at once, as in ``pyte``.
            output.append(byte)
        elif 0x40 <= byte <= 0x7E:
            self._sequence.append(byte)
            output += _accepted_csi(bytes(self._sequence))
            self._reset()
        elif len(self._sequence) >= _MAX_CSI_LENGTH:
            # Too long to be real: skip it up to its final byte.
            self._state = "csi-ignore"
        else:
            self._sequence.append(byte)

    def _feed_string(self, byte: int, output: bytearray) -> None:
        """Skip one byte of a control string."""
        self._string_length += 1
        if self._state == "string-escape":
            if byte == ord("\\"):
                self._reset()
            else:
                # ESC ends the string and starts a new sequence.
                self._reset()
                output += self.feed(bytes([_ESC, byte]))
            return
        if byte == _BEL or byte in _CAN_OR_SUB:
            self._reset()
        elif byte == _ESC:
            self._state = "string-escape"
        elif self._string_length > _MAX_STRING_LENGTH:
            self._reset()

    def _begin_escape(self) -> None:
        self._state = "escape"
        self._sequence = bytearray([_ESC])

    def _begin_string(self) -> None:
        self._state = "string"
        self._sequence = bytearray()
        self._string_length = 0

    def _reset(self) -> None:
        self._state = "ground"
        self._sequence = bytearray()


def _accepted_csi(sequence: bytes) -> bytes:
    """Return a finished CSI sequence in a form ``pyte`` handles, or nothing.

    :param sequence: ``ESC [`` parameters, intermediates and the final byte.
    :returns: The sequence, its rewritten form, or ``b""`` to drop it.
    """
    body, final = sequence[2:-1], sequence[-1:]
    prefix = body[:1] if body[:1] in (b"<", b"=", b">", b"?") else b""
    parameters = body[len(prefix):]
    if any(0x20 <= byte <= 0x2F for byte in parameters):
        return b""
    if any(byte in b"<=>?" for byte in parameters):
        return b""
    if prefix == b"?":
        return sequence if final in _PRIVATE_FINALS and b":" not in parameters else b""
    if prefix:
        return b""
    if b":" in parameters:
        if final != b"m":
            return b""
        parameters = _semicolon_sgr(parameters)
    return b"\x1b[" + parameters + final


def _semicolon_sgr(parameters: bytes) -> bytes:
    """Rewrite colon SGR sub-parameters to the semicolon form ``pyte`` knows.

    ``38:2::R:G:B`` and ``38:2:R:G:B`` become ``38;2;R;G;B`` (also for 48 and
    58), ``38:5:N`` becomes ``38;5;N``, the underline styles ``4:1`` to
    ``4:5`` become ``4`` and ``4:0`` becomes ``24``; other groups keep their
    first number.

    :param parameters: SGR parameters with at least one colon.
    :returns: Semicolon-separated parameters.
    """
    groups = []
    for group in parameters.split(b";"):
        parts = group.split(b":")
        head = parts[0]
        if len(parts) == 1:
            groups.append(head)
        elif head in (b"38", b"48", b"58") and parts[1:2] == [b"2"]:
            rgb = parts[2:]
            if len(rgb) == 4:
                rgb = rgb[1:]  # Skip the color space identifier.
            groups.append(b";".join([head, b"2", *(value or b"0" for value in rgb)]))
        elif head in (b"38", b"48", b"58") and parts[1:2] == [b"5"]:
            groups.append(b";".join([head, b"5", parts[2] if len(parts) > 2 else b"0"]))
        elif head == b"4":
            groups.append(b"24" if parts[1] == b"0" else b"4")
        else:
            groups.append(head)
    return b";".join(groups)


class ScrollRegionMixin:
    """Add the CSI ``S`` and ``T`` scroll commands that ``pyte`` 0.8 lacks.

    Programs such as nano scroll their editing area with them when the
    terminal is an xterm. The scroll happens inside the scrolling margins and
    does not move the cursor; it reuses ``index`` and ``reverse_index`` so
    that screens which track lines leaving the screen see them as usual.
    """

    def scroll_up(self, count: int = 1) -> None:
        """Scroll the lines inside the margins up (SU).

        :param count: Number of lines.
        """
        top, bottom = self.margins or Margins(0, self.lines - 1)
        row = self.cursor.y
        self.cursor.y = bottom
        for _ in range(min(max(count, 1), bottom - top + 1)):
            self.index()
        self.cursor.y = row

    def scroll_down(self, count: int = 1) -> None:
        """Scroll the lines inside the margins down (SD).

        :param count: Number of lines.
        """
        top, bottom = self.margins or Margins(0, self.lines - 1)
        row = self.cursor.y
        self.cursor.y = top
        for _ in range(min(max(count, 1), bottom - top + 1)):
            self.reverse_index()
        self.cursor.y = row


class ScreenStream(pyte.ByteStream):
    """``pyte`` byte stream that also dispatches CSI ``S`` and ``T``."""

    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}


# DEC private modes of the alternate screen: 47 and 1047 switch, 1049 also
# saves the cursor before and restores it after; 1048 only saves and
# restores the cursor.
_ALTERNATE_MODES = (47, 1047, 1049)
_SAVE_CURSOR_MODES = (1048, 1049)


class AlternateScreenMixin:
    """Give a ``pyte`` history screen the xterm alternate screen.

    Full-screen programs such as vim, nano and less switch to the alternate
    screen and back when they exit, so the shell screen reappears as it was.
    Lines scrolled on the alternate screen do not enter the history, as in
    xterm. ``pyte`` 0.8 has no alternate screen: it ignores these modes.
    """

    _main_buffer = None
    _saved_cursor = None

    @property
    def in_alternate_screen(self) -> bool:
        """Return whether the alternate screen is shown."""
        return self._main_buffer is not None

    def set_mode(self, *modes: int, **kwargs: object) -> None:
        """Handle the alternate screen modes; ``pyte`` records all modes as usual."""
        if kwargs.get("private"):
            if any(mode in _SAVE_CURSOR_MODES for mode in modes):
                self._saved_cursor = copy.copy(self.cursor)
            if any(mode in _ALTERNATE_MODES for mode in modes) and self._main_buffer is None:
                self._main_buffer = self.buffer
                self.buffer = self._new_buffer()
                self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: object) -> None:
        """Leave the alternate screen; ``pyte`` records all modes as usual."""
        if kwargs.get("private"):
            if any(mode in _ALTERNATE_MODES for mode in modes) and self._main_buffer is not None:
                self.buffer = self._main_buffer
                self._main_buffer = None
                self.dirty.update(range(self.lines))
            if any(mode in _SAVE_CURSOR_MODES for mode in modes) and self._saved_cursor is not None:
                self.cursor = self._saved_cursor
                self._saved_cursor = None
                self.ensure_hbounds()
                self.ensure_vbounds()
        super().reset_mode(*modes, **kwargs)

    def index(self) -> None:
        """Scroll without filling the history while the alternate screen is shown."""
        if self._main_buffer is not None:
            pyte.Screen.index(self)
        else:
            super().index()

    def reverse_index(self) -> None:
        """Scroll back without touching the history on the alternate screen."""
        if self._main_buffer is not None:
            pyte.Screen.reverse_index(self)
        else:
            super().reverse_index()

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Resize both screens while the alternate one is shown.

        The shell screen loses its top lines to the history and its cells
        beyond the new width, as ``pyte`` does for the visible screen.
        """
        old_lines, old_columns = self.lines, self.columns
        super().resize(lines=lines, columns=columns)
        main = self._main_buffer
        if main is None:
            return
        removed = old_lines - self.lines
        if removed > 0:
            for y in range(removed):
                self.history.top.append(main[y])
            for y in range(self.lines):
                main[y] = main[y + removed]
            for y in range(self.lines, old_lines):
                main.pop(y, None)
        if self.columns < old_columns:
            for line in main.values():
                for x in range(self.columns, old_columns):
                    line.pop(x, None)

    def reset(self) -> None:
        """Return to the shell screen on a full reset."""
        self._main_buffer = None
        self._saved_cursor = None
        super().reset()

    def _new_buffer(self):
        return defaultdict(lambda: StaticDefaultDict(self.default_char))


class _InvisibleSafeHistoryScreen(ScrollRegionMixin, AlternateScreenMixin, HistoryScreen):
    """History screen that does not lose text after invisible characters.

    ``pyte`` stops drawing the rest of a chunk at the first character without
    width that is not a combining mark, for example U+200B or U+202E. Such
    characters are replaced before drawing: dropped by default, or shown as a
    visible placeholder.
    """

    def __init__(self, *args: object, invisible_replacement: str = "", **kwargs: object) -> None:
        """Initialize the screen.

        :param invisible_replacement: Text drawn instead of an invisible
            character; empty to skip it.
        """
        super().__init__(*args, **kwargs)
        self._invisible_replacement = invisible_replacement

    def draw(self, data: str) -> None:
        """Draw text after replacing characters ``pyte`` would stop at."""
        super().draw(
            "".join(
                self._invisible_replacement if is_invisible_character(char) else char
                for char in data
            )
        )


def is_invisible_character(char: str) -> bool:
    """Return whether ``pyte`` would stop drawing at this character.

    :param char: One character.
    :returns: ``True`` for characters without width that do not combine
        with the previous character.
    """
    width = wcwidth(char)
    return width < 0 or (width == 0 and not unicodedata.combining(char))


class TerminalEmulator:
    """Wrap a :class:`pyte.Screen` and render it for the TUI."""

    def __init__(
        self,
        columns: int = 80,
        lines: int = 24,
        history: int = 5000,
        invisible_replacement: str = "",
    ) -> None:
        """Initialize the terminal emulator.

        :param columns: Initial terminal width.
        :param lines: Initial terminal height.
        :param history: Number of scrollback lines to keep locally.
        :param invisible_replacement: Text drawn instead of invisible
            characters such as U+200B; empty to skip them like a terminal.
        """
        self.screen = _InvisibleSafeHistoryScreen(
            columns,
            lines,
            history=history,
            invisible_replacement=invisible_replacement,
        )
        self.stream = ScreenStream(self.screen)
        self.sequence_filter = SequenceFilter()
        self._style_cache: dict[tuple, str] = {}

    def feed(self, data: bytes) -> None:
        """Feed new bytes into the terminal parser.

        Parsing while the local viewport is scrolled back would write into
        the manually rotated history buffers and lose lines, so the view
        first returns to the live bottom. This mirrors the common
        jump-to-bottom-on-output behavior of desktop terminals.

        Control sequences ``pyte`` cannot handle are filtered first (see
        :class:`SequenceFilter`).

        :param data: Raw bytes read from the guest connection.
        """
        if self.is_scrollback_active:
            self.scroll_to_bottom()
        data = self.sequence_filter.feed(data)
        try:
            self.stream.feed(data)
        except Exception:
            # A sequence ``pyte`` still cannot handle must not end the
            # session: the rest of this chunk is lost, parsing goes on.
            self.stream = ScreenStream(self.screen)

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

        The alternate screen of full-screen programs has no history, so
        nothing happens while it is shown.

        :param lines: Number of lines to move upward.
        """
        if self.screen.in_alternate_screen:
            return
        self._scroll_history_up(max(lines, 1))

    def scroll_down(self, lines: int = 1) -> None:
        """Scroll the local view toward newer history lines.

        :param lines: Number of lines to move downward.
        """
        self._scroll_history_down(max(lines, 1))

    def scroll_to_top(self) -> None:
        """Jump to the oldest locally retained history."""
        if self.screen.in_alternate_screen:
            return
        while self._scroll_history_up(self.screen.lines):
            continue

    def scroll_to_bottom(self) -> None:
        """Jump back to the live end of the terminal output."""
        while self._scroll_history_down(self.screen.lines):
            continue

    @property
    def bracketed_paste(self) -> bool:
        """Return whether the guest asked for bracketed paste (mode 2004)."""
        # ``pyte`` keeps private DEC modes shifted by five bits.
        return (2004 << 5) in self.screen.mode

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
            run_style = ""
            run_characters: list[str] = []
            for x in range(self.screen.columns):
                char = line[x]
                style = self._cell_style(char)
                if not cursor.hidden and cursor.x == x and cursor.y == y:
                    style = f"{style} reverse".strip()
                if style != run_style and run_characters:
                    fragments.append((run_style, "".join(run_characters)))
                    run_characters = []
                run_style = style
                run_characters.append(char.data)
            if run_characters:
                fragments.append((run_style, "".join(run_characters)))
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

    def _cell_style(self, char: pyte.screens.Char) -> str:
        """Return the cached prompt-toolkit style string for one cell.

        Screens repeat a small number of distinct styles across thousands of
        cells, so caching the joined style string keeps rendering cheap.

        :param char: Character cell from the virtual screen.
        :returns: Prompt-toolkit style string.
        """
        key = (
            char.fg,
            char.bg,
            char.bold,
            char.italics,
            char.underscore,
            char.strikethrough,
            char.reverse,
        )
        cached = self._style_cache.get(key)
        if cached is None:
            cached = " ".join(self._style_parts(char))
            self._style_cache[key] = cached
        return cached

    def _style_parts(self, char: pyte.screens.Char) -> list[str]:
        """Translate a ``pyte`` character style into prompt_toolkit styles.

        :param char: Character cell from the virtual screen.
        :returns: Prompt-toolkit style tokens.
        """
        style_parts: list[str] = []
        foreground = _normalize_style_color(char.fg)
        background = _normalize_style_color(char.bg)
        if foreground:
            style_parts.append(f"fg:{foreground}")
        if background:
            style_parts.append(f"bg:{background}")
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


def _normalize_style_color(color: str) -> str | None:
    """Normalize one ``pyte`` color value for prompt-toolkit.

    ``pyte`` may expose colors as plain hex strings like ``8a8a8a`` or as
    bright ANSI names such as ``brightred``. Prompt-toolkit expects hex colors
    with a leading ``#`` and uses ``ansibright*`` names for bright ANSI tones.

    :param color: Raw color string from ``pyte``.
    :returns: A prompt-toolkit compatible color token or ``None`` for default.
    """
    if color in {"", "default"}:
        return None

    lowered = color.lower()
    if lowered in _ANSI_BRIGHT_COLOR_ALIASES:
        return _ANSI_BRIGHT_COLOR_ALIASES[lowered]
    if color.startswith("#") and _HEX_COLOR_RE.fullmatch(color[1:]):
        return color
    if _HEX_COLOR_RE.fullmatch(color):
        return f"#{color}"
    return color

