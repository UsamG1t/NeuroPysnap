"""Interactive terminal session attached to a VM serial TCP console."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import shutil
import sys
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError
from pysnap.terminal.clipboard import copy_text_to_host_clipboard
from pysnap.terminal.emulator import TerminalEmulator
from pysnap.terminal.keymap import key_press_to_bytes
from pysnap.terminal.protocol import TerminalQueryResponder
from pysnap.terminal.transport import open_serial_connection


@dataclass
class SessionStatus:
    """Mutable session status shown in the status bar."""

    vm_name: str
    vm_state: str = "Changing"
    serial_port: int | None = None
    message: str = "Connecting..."


@dataclass
class TerminalSelection:
    """Visible terminal selection tracked in local screen coordinates."""

    anchor_row: int
    anchor_column: int
    row: int
    column: int

    @property
    def normalized(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return inclusive start/end coordinates in natural screen order."""
        start = (self.anchor_row, self.anchor_column)
        end = (self.row, self.column)
        if start <= end:
            return start, end
        return end, start

    @property
    def is_collapsed(self) -> bool:
        """Return whether the selection never left its anchor cell."""
        return (self.anchor_row, self.anchor_column) == (self.row, self.column)


class SelectionTracker:
    """Track the visible drag selection and the text captured from it.

    The tracker separates two lifetimes: the on-screen highlight, which
    becomes stale as soon as new guest output moves the underlying cells,
    and the captured text, which stays valid until the user copies it or
    starts interacting with the guest again. This lets the copy chord work
    while background utilities keep printing.
    """

    def __init__(self, emulator: TerminalEmulator) -> None:
        """Initialize the selection tracker.

        :param emulator: Guest terminal emulator that owns the screen cells.
        """
        self._emulator = emulator
        self.selection: TerminalSelection | None = None
        self.captured_text: str | None = None

    @property
    def is_active(self) -> bool:
        """Return whether a highlight or captured text is currently held."""
        return self.selection is not None or self.captured_text is not None

    def begin(self, x: int, y: int) -> None:
        """Start a new drag selection at one screen cell.

        :param x: Column of the drag start.
        :param y: Row of the drag start.
        """
        row, column = self._clamp(x, y)
        self.selection = TerminalSelection(
            anchor_row=row,
            anchor_column=column,
            row=row,
            column=column,
        )
        self.captured_text = None

    def update(self, x: int, y: int) -> None:
        """Extend the active drag selection to one screen cell.

        :param x: Column of the drag position.
        :param y: Row of the drag position.
        """
        if self.selection is None:
            return
        row, column = self._clamp(x, y)
        self.selection.row = row
        self.selection.column = column

    def finish(self, x: int, y: int) -> bool:
        """Finalize the drag and capture the selected text.

        A click that never left its anchor cell is treated as a plain click
        and clears any previous selection instead of capturing one cell.

        :param x: Column of the drag end.
        :param y: Row of the drag end.
        :returns: ``True`` when non-empty text was captured.
        """
        if self.selection is None:
            return False
        self.update(x, y)
        if self.selection.is_collapsed:
            self.clear()
            return False
        self.captured_text = (
            self._emulator.selected_text(self.selection.normalized) or None
        )
        if self.captured_text is None:
            self.clear()
            return False
        return True

    def take_captured_text(self) -> str | None:
        """Return the captured text and reset the tracker.

        :returns: Captured text or ``None`` when nothing is selected.
        """
        text = self.captured_text
        self.clear()
        return text

    def clear_highlight(self) -> None:
        """Drop the stale on-screen highlight but keep the captured text."""
        self.selection = None

    def clear(self) -> None:
        """Drop both the on-screen highlight and the captured text."""
        self.selection = None
        self.captured_text = None

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """Clamp one mouse position to valid screen coordinates.

        :param x: Raw column reported by the mouse event.
        :param y: Raw row reported by the mouse event.
        :returns: Clamped ``(row, column)`` coordinates.
        """
        row = min(max(y, 0), self._emulator.screen.lines - 1)
        column = min(max(x, 0), self._emulator.screen.columns - 1)
        return row, column


class ScrollableTerminalControl(FormattedTextControl):
    """Formatted text control with optional local scrollback mouse support."""

    def __init__(
        self,
        *args,
        on_scroll_up: Callable[[], None] | None = None,
        on_scroll_down: Callable[[], None] | None = None,
        mouse_scrolling_enabled: bool = False,
        on_selection_start: Callable[[int, int], None] | None = None,
        on_selection_update: Callable[[int, int], None] | None = None,
        on_selection_finish: Callable[[int, int], None] | None = None,
        **kwargs,
    ) -> None:
        """Initialize the scrollable terminal control.

        :param on_scroll_up: Callback for one local upward scroll action.
        :param on_scroll_down: Callback for one local downward scroll action.
        :param mouse_scrolling_enabled: Whether to intercept wheel events.
        :param on_selection_start: Callback for selection drag start.
        :param on_selection_update: Callback for selection drag updates.
        :param on_selection_finish: Callback for selection drag end.
        """
        super().__init__(*args, **kwargs)
        self._on_scroll_up = on_scroll_up
        self._on_scroll_down = on_scroll_down
        self._mouse_scrolling_enabled = mouse_scrolling_enabled
        self._on_selection_start = on_selection_start
        self._on_selection_update = on_selection_update
        self._on_selection_finish = on_selection_finish

    def mouse_handler(self, mouse_event: MouseEvent):
        """Handle local wheel scrolling before delegating to the base control."""
        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and mouse_event.button == MouseButton.LEFT
            and self._on_selection_start
        ):
            self._on_selection_start(
                mouse_event.position.x,
                mouse_event.position.y,
            )
            return None
        if (
            mouse_event.event_type == MouseEventType.MOUSE_MOVE
            and mouse_event.button in {MouseButton.LEFT, MouseButton.UNKNOWN}
            and self._on_selection_update
        ):
            self._on_selection_update(
                mouse_event.position.x,
                mouse_event.position.y,
            )
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP and self._on_selection_finish:
            self._on_selection_finish(
                mouse_event.position.x,
                mouse_event.position.y,
            )
            return None
        if self._mouse_scrolling_enabled:
            if mouse_event.event_type == MouseEventType.SCROLL_UP and self._on_scroll_up:
                self._on_scroll_up()
                return None
            if (
                mouse_event.event_type == MouseEventType.SCROLL_DOWN
                and self._on_scroll_down
            ):
                self._on_scroll_down()
                return None
        return super().mouse_handler(mouse_event)


class TerminalSession:
    """Manage one interactive terminal connection to a VM."""

    def __init__(self, service: PySnapService | None = None) -> None:
        """Initialize the terminal session helper.

        :param service: Optional application service.
        """
        self.service = service or PySnapService()

    def run(self, vm_name: str) -> int:
        """Start or attach to a VM and launch the terminal interface.

        :param vm_name: Virtual machine name.
        :returns: Exit code.
        """
        vm_info = self.service.prepare_vm_connection(vm_name)
        if vm_info.serial_port is None:
            raise PySnapError(f'Virtual machine "{vm_name}" does not expose a serial TCP port.')
        try:
            asyncio.run(self._run_async(vm_info.name, vm_info.serial_port))
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        return 0

    async def _run_async(self, vm_name: str, serial_port: int) -> None:
        """Run the asynchronous terminal session.

        :param vm_name: Virtual machine name.
        :param serial_port: Serial TCP port.
        """
        reader, writer = await open_serial_connection("localhost", serial_port)
        columns, rows = shutil.get_terminal_size(fallback=(80, 24))
        emulator = TerminalEmulator(*_terminal_content_size(columns=columns, rows=rows))
        query_responder = TerminalQueryResponder()
        status = SessionStatus(
            vm_name=vm_name,
            vm_state="Working",
            serial_port=serial_port,
            message="Connected. Waking serial console...",
        )
        stop_event = asyncio.Event()
        received_output = asyncio.Event()
        selection_tracker = SelectionTracker(emulator)

        await _wake_serial_console(writer)
        status.message = "Connected. Ctrl-Q detaches."

        def begin_selection(x: int, y: int) -> None:
            selection_tracker.begin(x, y)
            status.message = "Selecting text..."
            app.invalidate()

        def update_selection(x: int, y: int) -> None:
            selection_tracker.update(x, y)
            app.invalidate()

        def finish_selection(x: int, y: int) -> None:
            if selection_tracker.finish(x, y):
                status.message = "Selection ready. Ctrl+Shift+C copies."
            app.invalidate()

        def scroll_up(lines: int = 1) -> None:
            selection_tracker.clear_highlight()
            emulator.scroll_up(lines)
            app.invalidate()

        def scroll_down(lines: int = 1) -> None:
            selection_tracker.clear_highlight()
            emulator.scroll_down(lines)
            app.invalidate()

        def render_terminal() -> list[tuple[str, str]]:
            app = get_app()
            _resize_emulator_to_output(app=app, emulator=emulator)
            selection = selection_tracker.selection
            current_selection = None if selection is None else selection.normalized
            return emulator.as_formatted_text(selection=current_selection)

        def render_status() -> list[tuple[str, str]]:
            scrollback_suffix = " | Scrollback" if emulator.is_scrollback_active else ""
            selection_suffix = " | Selection" if selection_tracker.is_active else ""
            text = (
                f" {status.vm_name} | {status.vm_state} | "
                f"UART1:{status.serial_port} | "
                f"{status.message}{scrollback_suffix}{selection_suffix} "
            )
            return [("reverse", text)]

        terminal_control = ScrollableTerminalControl(
            text=render_terminal,
            focusable=True,
            show_cursor=False,
            on_scroll_up=scroll_up,
            on_scroll_down=scroll_down,
            mouse_scrolling_enabled=_should_enable_mouse_scrolling(),
            on_selection_start=begin_selection,
            on_selection_update=update_selection,
            on_selection_finish=finish_selection,
        )
        status_control = FormattedTextControl(text=render_status)
        layout = Layout(
            HSplit(
                [
                    Window(content=terminal_control, wrap_lines=False),
                    Window(content=status_control, height=1),
                ]
            )
        )

        bindings = KeyBindings()

        @bindings.add(Keys.ControlQ)
        def _detach(event) -> None:
            """Detach from the VM without stopping it."""
            status.message = "Detached."
            stop_event.set()
            event.app.exit()

        @bindings.add(Keys.ControlL)
        def _redraw(event) -> None:
            """Force a redraw of the terminal interface."""
            status.message = "Screen refreshed."
            event.app.invalidate()

        @bindings.add(Keys.Escape, Keys.Up)
        def _scroll_older_line(event) -> None:
            """Scroll one line toward older local terminal output."""
            emulator.scroll_up(1)
            event.app.invalidate()

        @bindings.add(Keys.Escape, Keys.Down)
        def _scroll_newer_line(event) -> None:
            """Scroll one line toward newer local terminal output."""
            emulator.scroll_down(1)
            event.app.invalidate()

        @bindings.add(Keys.Escape, Keys.Left)
        def _scroll_to_top(event) -> None:
            """Jump to the oldest retained local scrollback output."""
            selection_tracker.clear_highlight()
            emulator.scroll_to_top()
            event.app.invalidate()

        @bindings.add(Keys.Escape, Keys.Right)
        def _scroll_to_bottom(event) -> None:
            """Jump back to the live end of local output."""
            selection_tracker.clear_highlight()
            emulator.scroll_to_bottom()
            event.app.invalidate()

        @bindings.add(Keys.ControlC)
        def _copy_or_interrupt(event) -> None:
            """Copy the captured selection or forward an interrupt to the guest.

            Classic terminals deliver ``Ctrl+Shift+C`` and ``Ctrl+C`` as the
            same ``ETX`` byte, so PySnap disambiguates by selection state:
            with a captured selection the chord copies without touching the
            guest, and without one it forwards a real ``Ctrl+C``.
            """
            captured_text = selection_tracker.take_captured_text()
            if captured_text is None:
                writer.write(b"\x03")
                return
            copied = copy_text_to_host_clipboard(captured_text, output=event.app.output)
            status.message = (
                "Selection copied."
                if copied
                else "Selection captured, but clipboard export failed."
            )
            event.app.invalidate()

        @bindings.add(Keys.Any)
        def _forward_key(event) -> None:
            """Forward arbitrary key input to the serial transport."""
            selection_tracker.clear()
            key_press = event.key_sequence[-1]
            payload = key_press_to_bytes(key_press)
            if payload is None:
                return
            writer.write(payload)

        app = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=_should_use_full_screen(),
            mouse_support=True,
            terminal_size_polling_interval=0.25,
        )
        # Guest-side editors rely on a snappy lone Escape key. The default
        # prompt-toolkit flush timeouts hold Escape for up to half a second
        # because of the Alt+Arrow scrollback bindings, which reads as input
        # lag inside Vim, so both timeouts are tightened here.
        app.timeoutlen = 0.5
        app.ttimeoutlen = 0.05

        def report_connection_closed() -> None:
            status.vm_state = "Changing"
            status.message = "Serial connection closed."
            stop_event.set()
            _safe_exit_application(app)

        async def reader_loop() -> None:
            try:
                while not stop_event.is_set():
                    data = await reader.read(_READ_CHUNK_SIZE)
                    if not data:
                        report_connection_closed()
                        return
                    received_output.set()
                    selection_tracker.clear_highlight()
                    responses: list[bytes] = []
                    closed = False
                    while data:
                        emulator.feed(data)
                        responses.extend(
                            query_responder.collect_responses(
                                data,
                                emulator=emulator,
                                columns=_output_columns(app),
                                lines=_output_lines(app),
                            )
                        )
                        data = await _read_pending_burst(reader)
                        if data is None:
                            break
                        if not data:
                            closed = True
                            break
                    if responses and not closed:
                        for response in responses:
                            writer.write(response)
                        await writer.drain()
                    if closed:
                        report_connection_closed()
                        return
                    app.invalidate()
            except OSError as error:
                status.vm_state = "Changing"
                status.message = f"Connection error: {error}"
                stop_event.set()
                _safe_exit_application(app)

        async def resize_loop() -> None:
            try:
                previous_size = _resize_emulator_to_output(app=app, emulator=emulator)
                while not stop_event.is_set():
                    await asyncio.sleep(0.25)
                    current_size = _resize_emulator_to_output(app=app, emulator=emulator)
                    if current_size != previous_size:
                        previous_size = current_size
                        selection_tracker.clear_highlight()
                        app.invalidate()
            except Exception as error:
                if stop_event.is_set():
                    return
                status.vm_state = "Changing"
                status.message = f"Resize watcher error: {error}"
                stop_event.set()
                _safe_exit_application(app)

        async def watcher_loop() -> None:
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(0.5)
                    current_state = self.service.get_monitor_state_label(vm_name)
                    status.vm_state = current_state
                    if current_state != "Working":
                        status.message = f"VM state changed to {current_state}."
                    app.invalidate()
                    if current_state not in {"Working", "Active"}:
                        stop_event.set()
                        _safe_exit_application(app)
                        return
            except Exception as error:
                status.vm_state = "Changing"
                status.message = f"Watcher error: {error}"
                stop_event.set()
                _safe_exit_application(app)

        async def silence_hint_loop() -> None:
            try:
                await asyncio.wait_for(received_output.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                if stop_event.is_set():
                    return
                status.message = "Connected. Press Enter if the guest stays silent."
                app.invalidate()

        try:
            with self.service.session_registry.register(vm_name, serial_port):
                app.create_background_task(reader_loop())
                app.create_background_task(resize_loop())
                app.create_background_task(watcher_loop())
                app.create_background_task(silence_hint_loop())
                await app.run_async(handle_sigint=False)
        finally:
            stop_event.set()
            writer.close()
            await writer.wait_closed()


_READ_CHUNK_SIZE = 65536
_READ_BURST_TIMEOUT = 0.01


async def _read_pending_burst(reader: asyncio.StreamReader) -> bytes | None:
    """Return one immediately-following chunk of a fast output burst.

    Fast guest output such as full-screen editor redraws arrives as many
    small TCP chunks. Coalescing chunks that follow within a short window
    lets the reader loop redraw once per burst instead of once per chunk.

    :param reader: Async stream reader bound to the serial TCP transport.
    :returns: More bytes, ``b""`` when the connection closed, or ``None``
        when the guest paused.
    """
    try:
        return await asyncio.wait_for(
            reader.read(_READ_CHUNK_SIZE),
            timeout=_READ_BURST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None


async def _wake_serial_console(writer) -> None:
    """Prompt the guest serial console to emit its current login screen.

    Many guest systems connected through VirtualBox ``UART1`` stay visually
    silent until they receive an initial newline. Sending one after attach
    mirrors the manual ``Enter`` press a user would otherwise need.

    :param writer: Async stream writer bound to the serial TCP transport.
    """
    writer.write(b"\r\n")
    await writer.drain()


def _terminal_content_size(*, columns: int, rows: int) -> tuple[int, int]:
    """Convert outer terminal geometry into the visible guest text area.

    One row is reserved for PySnap's local status bar.

    :param columns: Outer terminal width.
    :param rows: Outer terminal height.
    :returns: Visible guest area as ``(columns, lines)``.
    """
    return max(columns, 1), max(rows - 1, 1)


def _output_columns(app: Application) -> int:
    """Return the current guest-visible width reported by prompt-toolkit."""
    columns, _ = _terminal_content_size_from_output(app=app)
    return columns


def _output_lines(app: Application) -> int:
    """Return the current guest-visible height reported by prompt-toolkit."""
    _, lines = _terminal_content_size_from_output(app=app)
    return lines


def _terminal_content_size_from_output(app: Application) -> tuple[int, int]:
    """Return the current visible guest area from the prompt-toolkit output.

    :param app: Running prompt-toolkit application.
    :returns: Visible guest area as ``(columns, lines)``.
    """
    size = app.output.get_size()
    return _terminal_content_size(columns=size.columns, rows=size.rows)


def _resize_emulator_to_output(
    *,
    app: Application,
    emulator: TerminalEmulator,
) -> tuple[int, int]:
    """Resize the emulator to the current outer terminal dimensions.

    :param app: Running prompt-toolkit application.
    :param emulator: Guest terminal emulator.
    :returns: Applied visible guest area as ``(columns, lines)``.
    """
    columns, lines = _terminal_content_size_from_output(app=app)
    emulator.resize(columns=columns, lines=lines)
    return columns, lines


def _should_use_full_screen() -> bool:
    """Return whether the prompt-toolkit UI should use alternate-screen mode.

    Git Bash on Windows commonly runs inside ``mintty``, where full-screen
    alternate-screen applications are more fragile than in native consoles.

    :returns: ``True`` when full-screen mode is preferred.
    """
    return not (sys.platform == "win32" and os.environ.get("MSYSTEM"))


def _should_enable_mouse_scrolling() -> bool:
    """Return whether local wheel scrolling should be enabled.

    Linux terminals provide the most predictable wheel-event handling in the
    current PySnap UI, so wheel-based scrollback stays Linux-only for now.

    :returns: ``True`` when Linux mouse-wheel scrolling is preferred.
    """
    return sys.platform.startswith("linux")


def _safe_exit_application(app: Application) -> None:
    """Exit a prompt-toolkit application only when it is still active.

    Background reader and watcher tasks can race with a user-triggered detach.
    In that case, prompt-toolkit may raise benign exceptions because the
    application has already been closed by another code path.

    :param app: Running prompt-toolkit application.
    :raises Exception: Propagated when prompt-toolkit reports an unexpected
        failure unrelated to a repeated exit attempt.
    """
    if not app.is_running or app.is_done:
        return

    try:
        app.exit()
    except Exception as error:
        message = str(error)
        if message in {
            "Application is not running. Application.exit() failed.",
            "Return value already set. Application.exit() failed.",
        }:
            return
        raise
