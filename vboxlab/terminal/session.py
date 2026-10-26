"""Interactive terminal session attached to a VM serial TCP console."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import shutil
import sys
from typing import Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from vboxlab.core.service import VBoxLabService
from vboxlab.errors import VBoxLabError
from vboxlab.report.control import ControlServer
from vboxlab.report.extract import ExtractError, QueueConsole, run_transfer
from vboxlab.ssh import client as ssh_client
from vboxlab.ssh.client import SshTarget
from vboxlab.ssh.keys import SshKeyStore
from vboxlab.terminal.clipboard import copy_text_to_host_clipboard
from vboxlab.terminal.emulator import TerminalEmulator
from vboxlab.terminal.keymap import key_press_to_bytes
from vboxlab.terminal.protocol import TerminalQueryResponder
from vboxlab.terminal.transport import open_serial_connection


@dataclass
class SessionStatus:
    """Mutable session status shown in the status bar."""

    vm_name: str
    vm_state: str = "Changing"
    port_label: str = ""
    message: str = "Connecting..."


class PauseTracker:
    """Follow pause and resume of the attached VM.

    The session stays open while the VM is paused: keys are held back and the
    status bar says so. Any state other than running or paused ends it.
    """

    def __init__(self, paused: bool = False) -> None:
        """Initialize the tracker.

        :param paused: Whether the VM was paused when the session started.
        """
        self.paused = paused

    def observe(self, state: str) -> str | None:
        """Record the current monitor state.

        :param state: Monitor label such as ``"Working"`` or ``"Paused"``.
        :returns: ``"paused"`` or ``"resumed"`` on a change, ``"exit"`` when
            the session must end, otherwise ``None``.
        """
        if state == "Paused":
            if self.paused:
                return None
            self.paused = True
            return "paused"
        if state in {"Working", "Active"}:
            if not self.paused:
                return None
            self.paused = False
            return "resumed"
        return "exit"


@dataclass
class SessionTransport:
    """How a terminal session reaches the guest.

    :param name: ``"Serial"`` or ``"ssh"``, used in messages.
    :param label: Status bar label such as ``UART1:2326`` or ``ssh:2400``.
    :param port: Host port, recorded in the session registry.
    :param serial: Whether this is the COM1 console: it is woken with Enter,
        may be attached while paused and serves ``report extract`` through
        the control channel.
    :param opener: Coroutine that connects for a terminal of the given
        columns and rows and returns a reader, a writer and an optional
        coroutine that cleans up afterwards.
    """

    name: str
    label: str
    port: int
    serial: bool
    opener: Callable[[int, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter, Callable[[], Awaitable[None]] | None]]]

    @classmethod
    def for_serial(cls, serial_port: int, *, timeout: float | None = None) -> "SessionTransport":
        """Return the transport for the COM1 TCP server.

        :param serial_port: Host TCP port of COM1.
        :param timeout: Connection timeout; the transport default without.
        :returns: Serial transport.
        """
        async def opener(columns: int, rows: int):
            if timeout is None:
                reader, writer = await open_serial_connection("localhost", serial_port)
            else:
                reader, writer = await open_serial_connection("localhost", serial_port, timeout=timeout)
            return reader, writer, None

        return cls("Serial", f"UART1:{serial_port}", serial_port, True, opener)

    @classmethod
    def for_ssh(cls, target: SshTarget, store: SshKeyStore) -> "SessionTransport":
        """Return the transport for an ssh login shell.

        :param target: ssh connection target.
        :param store: VBoxLab key store.
        :returns: ssh transport.
        """
        async def opener(columns: int, rows: int):
            reader, writer, process = await ssh_client.open_terminal(
                target, store, columns=columns, rows=rows
            )

            async def close() -> None:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

            return reader, writer, close

        return cls("ssh", f"ssh:{target.port}", target.port, False, opener)


DETACH_HINT = "Ctrl-] q detaches."
PAUSED_MESSAGE = f"VM is paused; keys wait for vboxlab resume. {DETACH_HINT}"
COMMAND_MESSAGE = "q detach, Up/Down/PgUp/PgDn/Home/End scroll, Esc back"
# Lines one mouse wheel step scrolls; on the alternate screen the same number
# of cursor keys goes to the guest.
WHEEL_LINES = 3
# A paused VM may not accept the serial connection; do not wait long for it.
PAUSED_CONNECT_TIMEOUT = 3.0


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

    def __init__(self, service: VBoxLabService | None = None) -> None:
        """Initialize the terminal session helper.

        :param service: Optional application service.
        """
        self.service = service or VBoxLabService()

    def run(self, vm_name: str) -> int:
        """Start or attach to a VM and launch the terminal interface.

        :param vm_name: Virtual machine name.
        :returns: Exit code.
        """
        vm_info = self.service.prepare_vm_connection(vm_name)
        paused = (vm_info.vm_state or "").lower() == "paused"
        if self.service.get_vm_access(vm_name).mode == "ssh":
            try:
                target = self.service.prepare_ssh_terminal(vm_name, notify=_notify)
            except KeyboardInterrupt:
                _notify("Stopped waiting for ssh.")
                return 130
            transport = SessionTransport.for_ssh(target, self.service.ssh_key_store)
        else:
            if vm_info.serial_port is None:
                raise VBoxLabError(f'Virtual machine "{vm_name}" does not expose a serial TCP port.')
            transport = SessionTransport.for_serial(
                vm_info.serial_port, timeout=PAUSED_CONNECT_TIMEOUT if paused else None
            )
        try:
            asyncio.run(self._run_async(vm_info.name, transport, paused=paused))
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
        return 0

    async def _run_async(
        self, vm_name: str, transport: SessionTransport, paused: bool = False
    ) -> None:
        """Run the asynchronous terminal session.

        :param vm_name: Virtual machine name.
        :param transport: How the guest is reached.
        :param paused: Whether the VM is paused; the console is then woken
            only after it is resumed.
        """
        columns, rows = shutil.get_terminal_size(fallback=(80, 24))
        content_columns, content_rows = _terminal_content_size(columns=columns, rows=rows)
        try:
            reader, writer, close_transport = await transport.opener(content_columns, content_rows)
        except VBoxLabError:
            if not paused:
                raise
            raise VBoxLabError(
                f'Virtual machine "{vm_name}" is paused and its serial port does not '
                f"accept connections; run vboxlab resume {vm_name}."
            ) from None
        emulator = TerminalEmulator(content_columns, content_rows)
        query_responder = TerminalQueryResponder()
        status = SessionStatus(
            vm_name=vm_name,
            vm_state="Paused" if paused else "Working",
            port_label=transport.label,
            message="Connected. Waking serial console...",
        )
        pause_tracker = PauseTracker(paused)
        stop_event = asyncio.Event()
        received_output = asyncio.Event()
        selection_tracker = SelectionTracker(emulator)
        # Set while ``vboxlab report extract`` uses this connection: guest
        # output goes to the transfer instead of the screen and keys pause.
        transfer: dict[str, QueueConsole | None] = {"console": None}
        transfer_output = bytearray()

        if paused:
            status.message = PAUSED_MESSAGE
        elif transport.serial:
            await _wake_serial_console(writer)
            status.message = f"Connected. {DETACH_HINT}"
        else:
            status.message = f"Connected through ssh. {DETACH_HINT}"

        def begin_selection(x: int, y: int) -> None:
            selection_tracker.begin(x, y)
            status.message = "Selecting text..."
            app.invalidate()

        def update_selection(x: int, y: int) -> None:
            selection_tracker.update(x, y)
            app.invalidate()

        def finish_selection(x: int, y: int) -> None:
            # Copy on release: many terminals keep Ctrl+Shift+C for their own
            # copy command, so it may never reach VBoxLab.
            if selection_tracker.finish(x, y):
                copied = copy_text_to_host_clipboard(
                    selection_tracker.captured_text, output=app.output
                )
                status.message = (
                    "Selection copied."
                    if copied
                    else "Selection captured, but clipboard export failed."
                )
            app.invalidate()

        def guest_accepts_keys() -> bool:
            return transfer["console"] is None and not pause_tracker.paused

        def scroll_up(lines: int = 1) -> None:
            selection_tracker.clear_highlight()
            emulator.scroll_up(lines)
            app.invalidate()

        def scroll_down(lines: int = 1) -> None:
            selection_tracker.clear_highlight()
            emulator.scroll_down(lines)
            app.invalidate()

        def wheel(up: bool) -> None:
            # Full-screen programs have no local history: like xterm, the
            # wheel sends cursor keys to them instead.
            if emulator.in_alternate_screen:
                if guest_accepts_keys():
                    writer.write(_cursor_key(up, emulator.application_cursor_keys) * WHEEL_LINES)
                return
            if up:
                scroll_up(WHEEL_LINES)
            else:
                scroll_down(WHEEL_LINES)

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
                f"{status.port_label} | "
                f"{status.message}{scrollback_suffix}{selection_suffix} "
            )
            return [("reverse", text)]

        terminal_control = ScrollableTerminalControl(
            text=render_terminal,
            focusable=True,
            show_cursor=False,
            on_scroll_up=lambda: wheel(True),
            on_scroll_down=lambda: wheel(False),
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
        # Every VBoxLab command sits behind Ctrl-], which programs in the
        # guest hardly use; all other keys belong to the guest.
        command_mode = {"active": False}
        in_command_mode = Condition(lambda: command_mode["active"])

        def leave_command_mode(message: str | None = None) -> None:
            command_mode["active"] = False
            status.message = message or DETACH_HINT

        @bindings.add(Keys.ControlSquareClose, filter=~in_command_mode)
        def _command_mode(event) -> None:
            """Wait for one VBoxLab command key."""
            command_mode["active"] = True
            status.message = COMMAND_MESSAGE
            event.app.invalidate()

        @bindings.add("q", filter=in_command_mode)
        def _detach(event) -> None:
            """Detach from the VM without stopping it."""
            command_mode["active"] = False
            status.message = "Detached."
            stop_event.set()
            event.app.exit()

        @bindings.add(Keys.ControlSquareClose, filter=in_command_mode)
        def _send_command_key(event) -> None:
            """Send Ctrl-] itself to the guest."""
            leave_command_mode()
            if guest_accepts_keys():
                writer.write(b"\x1d")
            event.app.invalidate()

        def add_scroll_key(key: Keys, action: Callable[[], None]) -> None:
            @bindings.add(key, filter=in_command_mode)
            def _scroll(event) -> None:
                selection_tracker.clear_highlight()
                action()
                event.app.invalidate()

        add_scroll_key(Keys.Up, lambda: emulator.scroll_up(1))
        add_scroll_key(Keys.Down, lambda: emulator.scroll_down(1))
        add_scroll_key(Keys.PageUp, lambda: emulator.scroll_up(emulator.screen.lines))
        add_scroll_key(Keys.PageDown, lambda: emulator.scroll_down(emulator.screen.lines))
        add_scroll_key(Keys.Home, emulator.scroll_to_top)
        add_scroll_key(Keys.End, emulator.scroll_to_bottom)

        @bindings.add(Keys.ControlC)
        def _copy_or_interrupt(event) -> None:
            """Copy the captured selection or forward an interrupt to the guest.

            Classic terminals deliver ``Ctrl+Shift+C`` and ``Ctrl+C`` as the
            same ``ETX`` byte, so VBoxLab disambiguates by selection state:
            with a captured selection the chord copies without touching the
            guest, and without one it forwards a real ``Ctrl+C``.
            """
            if command_mode["active"]:
                leave_command_mode()
                event.app.invalidate()
                return
            captured_text = selection_tracker.take_captured_text()
            if captured_text is None:
                if guest_accepts_keys():
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
            """Forward arbitrary key input to the guest.

            In command mode any other key, Esc included, only leaves the mode.
            """
            if command_mode["active"]:
                leave_command_mode()
                event.app.invalidate()
                return
            selection_tracker.clear()
            if transfer["console"] is not None:
                status.message = "Report transfer in progress; keys are paused."
                event.app.invalidate()
                return
            if pause_tracker.paused:
                status.message = PAUSED_MESSAGE
                event.app.invalidate()
                return
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
        # Guest-side editors rely on a snappy lone Escape key. No binding
        # starts with Escape, so only the input parser waits for the rest of
        # an escape sequence, which arrives in the same read.
        app.ttimeoutlen = 0.05

        def report_connection_closed() -> None:
            if transfer["console"] is not None:
                transfer["console"].close()
            status.vm_state = "Changing"
            status.message = f"{transport.name} connection closed."
            stop_event.set()
            _safe_exit_application(app)

        async def reader_loop() -> None:
            try:
                while not stop_event.is_set():
                    data = await reader.read(_READ_CHUNK_SIZE)
                    if not data:
                        report_connection_closed()
                        return
                    if transfer["console"] is not None:
                        transfer_output.extend(data)
                        transfer["console"].feed(data)
                        continue
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
                    change = pause_tracker.observe(current_state)
                    if change == "exit":
                        status.message = f"VM state changed to {current_state}."
                        app.invalidate()
                        stop_event.set()
                        _safe_exit_application(app)
                        return
                    if change == "paused":
                        status.message = PAUSED_MESSAGE
                    elif change == "resumed":
                        status.message = f"VM resumed. {DETACH_HINT}"
                        if transport.serial and not received_output.is_set():
                            # Connected while paused: the screen is still empty.
                            await _wake_serial_console(writer)
                    app.invalidate()
            except Exception as error:
                status.vm_state = "Changing"
                status.message = f"Watcher error: {error}"
                stop_event.set()
                _safe_exit_application(app)

        async def silence_hint_loop() -> None:
            try:
                await asyncio.wait_for(received_output.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                if stop_event.is_set() or pause_tracker.paused or not transport.serial:
                    return
                status.message = "Connected. Press Enter if the guest stays silent."
                app.invalidate()

        async def run_control_transfer(remote_path: str, max_bytes: int) -> tuple[bytes, str]:
            """Run a ``vboxlab report extract`` transfer on this connection."""
            loop = asyncio.get_running_loop()
            console = QueueConsole(lambda data: loop.call_soon_threadsafe(writer.write, data))
            transfer_output.clear()
            transfer["console"] = console
            status.message = "Extracting a report for vboxlab report extract; keys are paused."
            app.invalidate()
            try:
                result = await asyncio.to_thread(
                    run_transfer, console, vm_name, remote_path, max_bytes=max_bytes
                )
            except ExtractError as error:
                transfer["console"] = None
                status.message = f"Report transfer failed: {error}"
                # Show what the guest printed and send nothing more: after a
                # refusal a report recording may be running.
                emulator.feed(bytes(transfer_output))
                transfer_output.clear()
                app.invalidate()
                raise
            transfer["console"] = None
            transfer_output.clear()
            status.message = f"Report transferred. {DETACH_HINT}"
            if not stop_event.is_set():
                # The transfer ended with ``clear`` on the guest; show a
                # clean screen and ask the shell for a fresh prompt.
                emulator.feed(b"\x1b[H\x1b[2J")
                writer.write(b"\r")
            app.invalidate()
            return result

        # Only the COM1 console needs the control channel: VirtualBox serves one
        # client per serial port, while ssh accepts further connections.
        control = ControlServer(run_control_transfer) if transport.serial else None
        control_port = await control.start() if control is not None else None
        try:
            with self.service.session_registry.register(
                vm_name,
                transport.port,
                control_port=control_port,
                control_token=control.token if control is not None else None,
            ):
                app.create_background_task(reader_loop())
                app.create_background_task(resize_loop())
                app.create_background_task(watcher_loop())
                app.create_background_task(silence_hint_loop())
                await app.run_async(handle_sigint=False)
        finally:
            stop_event.set()
            if control is not None:
                await control.close()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if close_transport is not None:
                await close_transport()


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


def _cursor_key(up: bool, application: bool) -> bytes:
    """Return the cursor key sequence the guest expects.

    :param up: Up instead of Down.
    :param application: Whether the guest enabled application cursor keys.
    :returns: ``ESC O A`` style in application mode, ``ESC [ A`` otherwise.
    """
    return (b"\x1bO" if application else b"\x1b[") + (b"A" if up else b"B")


def _notify(message: str) -> None:
    """Print a progress message before the terminal interface starts."""
    print(message, flush=True)


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

    One row is reserved for VBoxLab's local status bar.

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
    current VBoxLab UI, so wheel-based scrollback stays Linux-only for now.

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
