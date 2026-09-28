"""Interactive viewers for reports: the ``text`` pager and the ``show`` player.

Both viewers draw only styled text produced by PySnap itself (transcript runs
or the ``pyte`` screen of the player), so nothing from a report reaches the
reader's terminal as a raw control sequence.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Sequence

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from pysnap.report.models import CellStyle, StyledRun
from pysnap.report.player import PlaybackController
from pysnap.terminal.emulator import _normalize_style_color
from pysnap.terminal.session import (
    ScrollableTerminalControl,
    _safe_exit_application,
    _should_enable_mouse_scrolling,
    _should_use_full_screen,
)

StyleFragments = list[tuple[str, str]]

FRAME_INTERVAL = 1 / 30
# Markers of text cut at the window edge in the ``text`` pager.
MARKER_STYLE = CellStyle(reverse=True)
_STATUS_STYLE = "reverse"


class PagerState:
    """Track the scroll position of the ``text`` pager."""

    def __init__(
        self,
        line_count: int,
        height: int = 1,
        *,
        line_width: int = 0,
        width: int = 80,
    ) -> None:
        """Initialize the pager at the top left.

        :param line_count: Number of text lines.
        :param height: Number of visible lines.
        :param line_width: Length of the longest line.
        :param width: Number of visible columns.
        """
        self.line_count = line_count
        self.height = max(height, 1)
        self.offset = 0
        self.line_width = line_width
        self.width = max(width, 1)
        self.column = 0

    @property
    def max_offset(self) -> int:
        """Return the offset that shows the last page."""
        return max(self.line_count - self.height, 0)

    def set_height(self, height: int) -> None:
        """Adapt to a new window height.

        :param height: Number of visible lines.
        """
        self.height = max(height, 1)
        self.offset = min(self.offset, self.max_offset)

    @property
    def max_column(self) -> int:
        """Return the column offset that shows the end of the longest line."""
        return max(self.line_width - self.width, 0)

    def set_width(self, width: int) -> None:
        """Adapt to a new window width.

        :param width: Number of visible columns.
        """
        self.width = max(width, 1)
        self.column = min(self.column, self.max_column)

    def scroll_right(self) -> None:
        """Shift the view right by half the window width, like ``less``."""
        self.column = min(self.column + max(self.width // 2, 1), self.max_column)

    def scroll_left(self) -> None:
        """Shift the view left by half the window width, like ``less``."""
        self.column = max(self.column - max(self.width // 2, 1), 0)

    def scroll(self, lines: int) -> None:
        """Scroll by a number of lines; negative values scroll up.

        :param lines: Line count to move.
        """
        self.offset = min(max(self.offset + lines, 0), self.max_offset)

    def page_down(self) -> None:
        """Scroll one page down."""
        self.scroll(self.height)

    def page_up(self) -> None:
        """Scroll one page up."""
        self.scroll(-self.height)

    def to_top(self) -> None:
        """Show the first page."""
        self.offset = 0

    def to_bottom(self) -> None:
        """Show the last page."""
        self.offset = self.max_offset

    def visible_range(self) -> range:
        """Return the indices of the visible lines."""
        return range(self.offset, min(self.offset + self.height, self.line_count))


def clip_runs(runs: Sequence[StyledRun], column: int, width: int) -> list[StyledRun]:
    """Cut a line to the visible columns, marking hidden text like ``less -S``.

    A ``<`` in the first visible column means text continues to the left, a
    ``>`` in the last visible column means text continues to the right.

    :param runs: Styled runs of one line.
    :param column: First visible column.
    :param width: Number of visible columns.
    :returns: Styled runs of the visible part.
    """
    cells = [(char, run.style) for run in runs for char in run.text]
    visible = cells[column:column + width]
    if column > 0 and visible:
        visible[0] = ("<", MARKER_STYLE)
    if len(cells) > column + width and visible:
        visible[-1] = (">", MARKER_STYLE)
    clipped: list[StyledRun] = []
    for char, style in visible:
        if clipped and clipped[-1].style == style:
            clipped[-1] = StyledRun(clipped[-1].text + char, style)
        else:
            clipped.append(StyledRun(char, style))
    return clipped


def style_runs_to_fragments(runs: Sequence[StyledRun]) -> StyleFragments:
    """Convert styled runs into prompt_toolkit fragments.

    :param runs: Styled runs of one line.
    :returns: ``(style, text)`` fragments.
    """
    return [(cell_style_to_prompt_toolkit(run.style), run.text) for run in runs]


def cell_style_to_prompt_toolkit(style: CellStyle) -> str:
    """Translate a :class:`CellStyle` into a prompt_toolkit style string.

    :param style: Cell style.
    :returns: prompt_toolkit style string.
    """
    parts: list[str] = []
    foreground = _normalize_style_color(style.fg)
    background = _normalize_style_color(style.bg)
    if foreground:
        parts.append(f"fg:{foreground}")
    if background:
        parts.append(f"bg:{background}")
    for enabled, name in (
        (style.bold, "bold"),
        (style.italics, "italic"),
        (style.underscore, "underline"),
        (style.strikethrough, "strike"),
        (style.reverse, "reverse"),
    ):
        if enabled:
            parts.append(name)
    return " ".join(parts)


def run_pager(title: str, lines: Sequence[Sequence[StyledRun]]) -> None:
    """Show styled lines in a full-screen scrollable pager.

    Long lines are cut at the window edge like ``less -S``; ``<`` and ``>``
    mark hidden text. Keys: ``Up``/``Down`` or ``k``/``j`` scroll a line,
    ``PageUp``/``PageDown``, ``b`` and ``Space`` scroll a page, ``g``/``Home``
    and ``G``/``End`` jump to the start or end, ``Left``/``Right`` shift the
    view by half the window width, ``q`` or ``Ctrl-Q`` quit; the mouse wheel
    scrolls on Linux.

    :param title: Name shown in the status line.
    :param lines: Styled runs of every line.
    """
    line_list = [tuple(runs) for runs in lines]
    state = PagerState(
        len(line_list),
        line_width=max((sum(len(run.text) for run in runs) for runs in line_list), default=0),
    )

    def render_text() -> StyleFragments:
        size = get_app().output.get_size()
        state.set_height(size.rows - 1)
        state.set_width(size.columns)
        fragments: StyleFragments = []
        for position, index in enumerate(state.visible_range()):
            if position:
                fragments.append(("", "\n"))
            fragments.extend(
                style_runs_to_fragments(clip_runs(line_list[index], state.column, state.width))
            )
        return fragments

    def render_status() -> StyleFragments:
        visible = state.visible_range()
        first = visible.start + 1 if len(visible) else 0
        column = f" | column {state.column + 1}" if state.column else ""
        text = (
            f" {title} | lines {first}-{visible.stop} of {state.line_count}{column} | "
            "arrows/PgUp/PgDn scroll, q quits "
        )
        return [(_STATUS_STYLE, text)]

    def scroll_up() -> None:
        state.scroll(-1)
        get_app().invalidate()

    def scroll_down() -> None:
        state.scroll(1)
        get_app().invalidate()

    bindings = KeyBindings()
    actions = {
        ("up",): lambda: state.scroll(-1),
        ("k",): lambda: state.scroll(-1),
        ("down",): lambda: state.scroll(1),
        ("j",): lambda: state.scroll(1),
        ("pageup",): state.page_up,
        ("b",): state.page_up,
        ("pagedown",): state.page_down,
        (" ",): state.page_down,
        ("g",): state.to_top,
        ("home",): state.to_top,
        ("G",): state.to_bottom,
        ("end",): state.to_bottom,
        ("left",): state.scroll_left,
        ("right",): state.scroll_right,
    }
    for keys, action in actions.items():
        _bind(bindings, keys, action)
    for keys in (("q",), (Keys.ControlQ,)):
        bindings.add(*keys)(lambda event: _safe_exit_application(event.app))

    text_control = ScrollableTerminalControl(
        text=render_text,
        focusable=True,
        show_cursor=False,
        on_scroll_up=scroll_up,
        on_scroll_down=scroll_down,
        mouse_scrolling_enabled=_should_enable_mouse_scrolling(),
    )
    layout = Layout(
        HSplit(
            [
                Window(content=text_control, wrap_lines=False),
                Window(content=FormattedTextControl(text=render_status), height=1),
            ]
        )
    )
    app = Application(
        layout=layout,
        key_bindings=bindings,
        full_screen=_should_use_full_screen(),
        mouse_support=True,
    )
    app.run()


def screen_viewport(
    rows: int,
    cursor_row: int,
    visible_rows: int,
) -> range:
    """Choose the screen rows shown when the window is lower than the screen.

    The viewport keeps the cursor row visible and prefers the bottom of the
    screen, where new output appears.

    :param rows: Number of rows of the recorded screen.
    :param cursor_row: Row of the cursor.
    :param visible_rows: Number of rows that fit into the window.
    :returns: Indices of the rows to show.
    """
    visible_rows = max(visible_rows, 1)
    if rows <= visible_rows:
        return range(rows)
    top = min(max(cursor_row - visible_rows + 1, 0), rows - visible_rows)
    return range(top, top + visible_rows)


def split_rows(fragments: StyleFragments) -> list[StyleFragments]:
    """Split screen fragments into rows at their ``"\\n"`` separators.

    :param fragments: Fragments of a whole screen.
    :returns: Fragments per row.
    """
    rows: list[StyleFragments] = [[]]
    for style, text in fragments:
        if text == "\n":
            rows.append([])
        else:
            rows[-1].append((style, text))
    return rows


def format_player_status(controller: PlaybackController, title: str, clipped: bool) -> str:
    """Build the status line of the ``show`` player.

    :param controller: Playback state.
    :param title: Report name.
    :param clipped: Whether the window is smaller than the recorded screen.
    :returns: Status text.
    """
    player = controller.player
    state = "Playing" if controller.playing else "Paused"
    parts = [f" {title}"]
    if clipped:
        parts.append(
            f"window smaller than recording {player.emulator.screen.columns}x"
            f"{player.emulator.screen.lines}"
        )
    parts.extend(
        [
            f"{state} x{controller.speed:g}",
            f"{_format_time(player.time)} / {_format_time(player.duration)}",
            f"command {player.current_command}/{len(player.command_times)}",
            "Space pause, +/- speed, n/p commands, q quits ",
        ]
    )
    return " | ".join(parts)


def run_player(controller: PlaybackController, title: str) -> None:
    """Play a recording in a full-screen window.

    Keys follow the ``show`` decision: ``Space`` pauses and resumes, ``+``
    and ``-`` change the speed, ``n``/``p`` jump between commands,
    ``Home``/``End`` jump to the start or end, ``q`` or ``Ctrl-Q`` quit.
    While paused, ``Alt+Up``/``Alt+Down`` and the mouse wheel scroll the
    screen history.

    :param controller: Playback state with its player.
    :param title: Report name shown in the status line.
    """
    message = {"text": ""}

    def render_screen() -> StyleFragments:
        app = get_app()
        size = app.output.get_size()
        emulator = controller.player.emulator
        rows = split_rows(emulator.as_formatted_text())
        viewport = screen_viewport(len(rows), emulator.screen.cursor.y, size.rows - 1)
        fragments: StyleFragments = []
        for position, index in enumerate(viewport):
            if position:
                fragments.append(("", "\n"))
            fragments.extend(rows[index])
        return fragments

    def render_status() -> StyleFragments:
        size = get_app().output.get_size()
        screen = controller.player.emulator.screen
        clipped = screen.columns > size.columns or screen.lines > size.rows - 1
        text = format_player_status(controller, title, clipped)
        if message["text"]:
            text = f"{text}| {message['text']} "
        return [(_STATUS_STYLE, text)]

    def scroll(lines: int) -> None:
        if controller.playing:
            message["text"] = "Pause to scroll"
        elif lines < 0:
            message["text"] = ""
            controller.player.emulator.scroll_up(-lines)
        else:
            message["text"] = ""
            controller.player.emulator.scroll_down(lines)
        get_app().invalidate()

    bindings = KeyBindings()
    actions = {
        (" ",): controller.toggle,
        ("+",): controller.faster,
        ("=",): controller.faster,
        ("-",): controller.slower,
        ("n",): controller.next_command,
        ("p",): controller.previous_command,
        ("home",): controller.to_start,
        ("end",): controller.to_end,
        (Keys.Escape, Keys.Up): lambda: scroll(-1),
        (Keys.Escape, Keys.Down): lambda: scroll(1),
    }
    for keys, action in actions.items():
        _bind(bindings, keys, action)
    for keys in (("q",), (Keys.ControlQ,)):
        bindings.add(*keys)(lambda event: _safe_exit_application(event.app))

    screen_control = ScrollableTerminalControl(
        text=render_screen,
        focusable=True,
        show_cursor=False,
        on_scroll_up=lambda: scroll(-1),
        on_scroll_down=lambda: scroll(1),
        mouse_scrolling_enabled=_should_enable_mouse_scrolling(),
    )
    layout = Layout(
        HSplit(
            [
                Window(content=screen_control, wrap_lines=False),
                Window(content=FormattedTextControl(text=render_status), height=1),
            ]
        )
    )
    app = Application(
        layout=layout,
        key_bindings=bindings,
        full_screen=_should_use_full_screen(),
        mouse_support=True,
        terminal_size_polling_interval=0.25,
    )

    async def tick() -> None:
        previous = monotonic()
        while True:
            await asyncio.sleep(FRAME_INTERVAL)
            now = monotonic()
            if controller.playing:
                controller.tick(now - previous)
                app.invalidate()
            previous = now

    async def main() -> None:
        ticker = asyncio.ensure_future(tick())
        try:
            await app.run_async()
        finally:
            ticker.cancel()

    asyncio.run(main())


def _bind(bindings: KeyBindings, keys: tuple, action) -> None:
    """Bind keys to an action that is followed by a redraw."""

    @bindings.add(*keys)
    def _handler(event) -> None:
        action()
        event.app.invalidate()


def _format_time(seconds: float) -> str:
    """Format seconds as ``MM:SS.s``."""
    minutes, rest = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes):02d}:{rest:04.1f}"
