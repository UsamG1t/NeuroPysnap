"""Interactive terminal session attached to a VM serial TCP console."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import shutil
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from pysnap.core.service import PySnapService
from pysnap.errors import PySnapError
from pysnap.terminal.emulator import TerminalEmulator
from pysnap.terminal.keymap import key_press_to_bytes
from pysnap.terminal.transport import open_serial_connection


@dataclass
class SessionStatus:
    """Mutable session status shown in the status bar."""

    vm_name: str
    vm_state: str = "Changing"
    serial_port: int | None = None
    message: str = "Connecting..."


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
        asyncio.run(self._run_async(vm_info.name, vm_info.serial_port))
        return 0

    async def _run_async(self, vm_name: str, serial_port: int) -> None:
        """Run the asynchronous terminal session.

        :param vm_name: Virtual machine name.
        :param serial_port: Serial TCP port.
        """
        reader, writer = await open_serial_connection("127.0.0.1", serial_port)
        columns, rows = shutil.get_terminal_size(fallback=(80, 24))
        emulator = TerminalEmulator(columns=columns, lines=max(rows - 1, 1))
        status = SessionStatus(
            vm_name=vm_name,
            vm_state="Working",
            serial_port=serial_port,
            message="Connected. Ctrl-Q detaches.",
        )
        stop_event = asyncio.Event()

        def render_terminal() -> list[tuple[str, str]]:
            app = get_app()
            size = app.output.get_size()
            emulator.resize(size.columns, max(size.rows - 1, 1))
            return emulator.as_formatted_text()

        def render_status() -> list[tuple[str, str]]:
            text = (
                f" {status.vm_name} | {status.vm_state} | "
                f"UART1:{status.serial_port} | {status.message} "
            )
            return [("reverse", text)]

        terminal_control = FormattedTextControl(
            text=render_terminal,
            focusable=True,
            show_cursor=False,
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

        @bindings.add(Keys.Any)
        def _forward_key(event) -> None:
            """Forward arbitrary key input to the serial transport."""
            key_press = event.key_sequence[-1]
            payload = key_press_to_bytes(key_press)
            if payload is None:
                return
            writer.write(payload)

        app = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=True,
            mouse_support=False,
        )

        async def reader_loop() -> None:
            try:
                while not stop_event.is_set():
                    data = await reader.read(4096)
                    if not data:
                        status.vm_state = "Changing"
                        status.message = "Serial connection closed."
                        stop_event.set()
                        app.exit()
                        return
                    emulator.feed(data)
                    app.invalidate()
            except OSError as error:
                status.vm_state = "Changing"
                status.message = f"Connection error: {error}"
                stop_event.set()
                app.exit()

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
                        app.exit()
                        return
            except Exception as error:
                status.vm_state = "Changing"
                status.message = f"Watcher error: {error}"
                stop_event.set()
                app.exit()

        try:
            with self.service.session_registry.register(vm_name, serial_port):
                app.create_background_task(reader_loop())
                app.create_background_task(watcher_loop())
                await app.run_async()
        finally:
            stop_event.set()
            writer.close()
            await writer.wait_closed()
