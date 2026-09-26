"""Tests for extracting reports through an attached ``pysnap connect``."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest

from pysnap.report.control import ControlServer, request_transfer
from pysnap.report.extract import ExtractError, QueueConsole, extract_file, run_transfer
from pysnap.runtime.sessions import SessionRecord, SessionRegistry
from pysnap.tests.test_report_extract import _Service, _ShellServer


class _LoopThread:
    """Run an asyncio loop in a background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self) -> "_LoopThread":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def run(self, coroutine, timeout: float = 30.0):
        """Run a coroutine on the loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)


class ControlServerTests(unittest.TestCase):
    """Verify the control protocol without a guest."""

    def _serve(self, loop: _LoopThread, runner):
        server = ControlServer(runner)
        port = loop.run(server.start())
        return server, port

    def test_returns_the_transferred_file(self) -> None:
        """Send the file and checksum produced by the session's transfer."""
        calls = []

        async def runner(path, max_bytes):
            calls.append((path, max_bytes))
            return b"report bytes", hashlib.sha256(b"report bytes").hexdigest()

        with _LoopThread() as loop:
            server, port = self._serve(loop, runner)
            data, checksum = request_transfer(port, server.token, '"$HOME"/r', max_bytes=100)
            loop.run(server.close())

        self.assertEqual(data, b"report bytes")
        self.assertEqual(calls, [('"$HOME"/r', 100)])
        self.assertEqual(len(checksum), 64)

    def test_rejects_wrong_tokens_and_invalid_requests(self) -> None:
        """Refuse requests without the secret and malformed lines."""
        async def runner(path, max_bytes):
            raise AssertionError("must not run")

        with _LoopThread() as loop:
            server, port = self._serve(loop, runner)
            with self.assertRaisesRegex(ExtractError, "Invalid control token"):
                request_transfer(port, "wrong", "x")
            with socket.create_connection(("127.0.0.1", port)) as connection:
                connection.sendall(b"not json\n")
                answer = json.loads(connection.makefile("rb").readline())
            loop.run(server.close())

        self.assertEqual(answer, {"ok": False, "error": "Invalid control request."})

    def test_passes_transfer_errors_and_refuses_parallel_transfers(self) -> None:
        """Report errors of the session and allow one transfer at a time."""
        started = threading.Event()
        release = threading.Event()

        async def runner(path, max_bytes):
            if path == "fail":
                raise ExtractError("The console is not at a shell prompt.")
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 10)
            return b"x", hashlib.sha256(b"x").hexdigest()

        with _LoopThread() as loop:
            server, port = self._serve(loop, runner)
            with self.assertRaisesRegex(ExtractError, "not at a shell prompt"):
                request_transfer(port, server.token, "fail")
            first = threading.Thread(target=request_transfer, args=(port, server.token, "slow"))
            first.start()
            started.wait(5)
            with self.assertRaisesRegex(ExtractError, "Another transfer is running"):
                request_transfer(port, server.token, "second")
            release.set()
            first.join(5)
            loop.run(server.close())

    def test_reports_sessions_that_do_not_answer(self) -> None:
        """Explain a closed control port."""
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]
        with self.assertRaisesRegex(ExtractError, "did not answer"):
            request_transfer(port, "token", "x")


class SessionRecordTests(unittest.TestCase):
    """Verify the control fields of session records."""

    def test_stores_control_fields_readable_only_by_the_user(self) -> None:
        """Persist the control port and secret with private permissions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = SessionRegistry(root_dir=Path(temp_dir))
            with registry.register("first", 2326, control_port=4242, control_token="secret"):
                record = registry.get_live_session("first")
                files = list(Path(temp_dir).glob("*.json"))
                mode = files[0].stat().st_mode & 0o777

        self.assertEqual((record.control_port, record.control_token), (4242, "secret"))
        if os.name == "posix":
            self.assertEqual(mode, 0o600)


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("base64"),
    "bash and coreutils are required",
)
class AttachedSessionTransferTests(unittest.TestCase):
    """Transfer through a control server that owns a real serial connection."""

    def test_extract_uses_the_attached_session(self) -> None:
        """Let extract ask the session, which runs the protocol on its socket."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guest, host = root / "guest", root / "host"
            guest.mkdir()
            host.mkdir()
            content = os.urandom(5000)
            (guest / "report.01.first").write_bytes(content)

            with _ShellServer(guest) as shell, _LoopThread() as loop:
                serial = shell.connect("localhost", shell.port, 5)
                serial.settimeout(0.05)

                async def runner(path, max_bytes):
                    console = QueueConsole(serial.sendall)
                    stop = threading.Event()

                    def pump() -> None:
                        while not stop.is_set():
                            try:
                                chunk = serial.recv(65536)
                            except socket.timeout:
                                continue
                            if not chunk:
                                console.close()
                                return
                            console.feed(chunk)

                    reader = threading.Thread(target=pump, daemon=True)
                    reader.start()
                    try:
                        return await asyncio.to_thread(
                            run_transfer, console, "first", path, shell_timeout=3.0,
                            idle_timeout=5.0, max_bytes=max_bytes,
                        )
                    finally:
                        stop.set()
                        reader.join(2)

                server = ControlServer(runner)
                port = loop.run(server.start())
                session = SessionRecord("first", 2326, os.getpid(), "now", port, server.token)
                result = extract_file(
                    _Service(session=session), "first", "report.01.first",
                    destination=host / "report.01.first",
                    connector=lambda *args: self.fail("must not open the serial port"),
                )
                loop.run(server.close())
                serial.close()

            self.assertEqual((host / "report.01.first").read_bytes(), content)
            self.assertEqual(result.size, len(content))


if __name__ == "__main__":
    unittest.main()
