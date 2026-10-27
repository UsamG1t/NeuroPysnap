"""Tests for the VM state checks of a pysnap connect session."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pysnap.core.service import PySnapService
from pysnap.runtime.sessions import SessionRecord
from pysnap.terminal.session import SessionTransport, TerminalSession
from pysnap.tests.test_connect_ssh import _Service, _Writer
from pysnap.vbox.client import VBoxManageClient


class _CountingRunner:
    """Answer VBoxManage calls for one running VM and count them."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, arguments, **kwargs) -> str:
        self.commands.append(tuple(arguments))
        if arguments[:1] == ["showvminfo"]:
            return 'name="vm"\nUUID="u"\nVMState="running"\n'
        if arguments[:2] == ["list", "vms"]:
            return '"vm" {u}\n'
        return ""


class _Registry:
    def __init__(self, live: bool) -> None:
        self.live = live

    def get_live_session(self, vm_name: str):
        return SessionRecord(vm_name, 2326, 1, "now") if self.live else None


class StateLabelTests(unittest.TestCase):
    """Verify the cheap state check used by sessions."""

    def test_one_vboxmanage_call_per_check(self) -> None:
        """Ask VBoxManage once and tell Working from Active."""
        for live, label in ((True, "Working"), (False, "Active")):
            with self.subTest(live=live):
                runner = _CountingRunner()
                service = PySnapService(client=VBoxManageClient(runner=runner), session_registry=_Registry(live))
                self.assertEqual(service.get_session_state_label("vm"), label)
                self.assertEqual(runner.commands, [("showvminfo", "vm", "--machinereadable")])


class SlowStateTests(unittest.TestCase):
    """Keep keys flowing while VBoxManage is slow."""

    def test_keys_do_not_wait_for_the_state_check(self) -> None:
        """Send every key at once although each state check takes 0.4 seconds."""

        class SlowService(_Service):
            def get_session_state_label(self, vm_name: str) -> str:
                time.sleep(0.4)
                return "Working"

        delays: list[float] = []
        sent: dict[bytes, float] = {}

        class TimedWriter(_Writer):
            def write(self, data: bytes) -> None:
                if data in sent:
                    delays.append(time.monotonic() - sent[data])
                super().write(data)

        async def scenario() -> None:
            reader = asyncio.StreamReader()
            writer = TimedWriter()

            async def opener(columns: int, rows: int):
                return reader, writer, None

            transport = SessionTransport("ssh", "ssh:1", 1, False, opener)
            with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
                loop = asyncio.get_running_loop()

                def type_keys() -> None:
                    # A thread of its own: a blocked event loop cannot delay
                    # the keys or their time stamps.
                    time.sleep(0.3)
                    for key in b"abcdefghij":
                        data = bytes([key])
                        sent[data] = time.monotonic()
                        pipe_input.send_bytes(data)
                        time.sleep(0.11)
                    time.sleep(0.3)
                    loop.call_soon_threadsafe(reader.feed_eof)

                typist = threading.Thread(target=type_keys)
                typist.start()
                await TerminalSession(service=SlowService(["Working"]))._run_async("vm", transport)
                typist.join()

        with patch("pysnap.terminal.session.STATE_POLL_INTERVAL", 0.05):
            asyncio.run(scenario())
        self.assertEqual(len(delays), 10)
        self.assertLess(max(delays), 0.2, delays)


if __name__ == "__main__":
    unittest.main()
