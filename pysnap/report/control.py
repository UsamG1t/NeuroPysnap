"""Control channel between ``pysnap report extract`` and ``pysnap connect``.

VirtualBox serves one client on a ``UART1`` TCP port, so while
``pysnap connect`` is attached only that process can talk to the console
(decision D39). The connect process listens on ``127.0.0.1`` on a random port
and stores the port and a random secret in its session record, which only the
current user can read. ``pysnap report extract`` sends one JSON request line
with the secret and the quoted file path; the connect process runs the
transfer protocol of :mod:`pysnap.report.extract` on its own serial
connection and answers with one JSON line holding the file as base64 and its
checksum, or an error message.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import socket
from typing import Awaitable, Callable

from pysnap.report.extract import DEFAULT_MAX_BYTES, ExtractError

REQUEST_TIMEOUT = 5.0
MAX_REQUEST_BYTES = 64 * 1024
CONTROL_HOST = "127.0.0.1"

# Runs a transfer for a quoted remote path and returns (data, checksum).
TransferRunner = Callable[[str, int], Awaitable[tuple[bytes, str]]]


class ControlServer:
    """Serve transfer requests for one ``pysnap connect`` session."""

    def __init__(self, run_transfer: TransferRunner) -> None:
        """Initialize the server.

        :param run_transfer: Coroutine performing a transfer on the session's
            serial connection.
        """
        self.token = secrets.token_hex(16)
        self.port: int | None = None
        self._run_transfer = run_transfer
        self._server: asyncio.base_events.Server | None = None
        self._busy = False

    async def start(self) -> int:
        """Listen on a random local port.

        :returns: The port number.
        """
        self._server = await asyncio.start_server(
            self._handle, CONTROL_HOST, 0, limit=MAX_REQUEST_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        """Stop listening."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one request."""
        try:
            response = await self._respond(reader)
            writer.write(json.dumps(response).encode("utf-8") + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _respond(self, reader: asyncio.StreamReader) -> dict:
        """Validate a request and run the transfer."""
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT)
            request = json.loads(line.decode("utf-8"))
        except (asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
            return {"ok": False, "error": "Invalid control request."}
        if not isinstance(request, dict) or not hmac.compare_digest(
            str(request.get("token", "")), self.token
        ):
            return {"ok": False, "error": "Invalid control token."}
        path = request.get("path")
        max_bytes = request.get("max_bytes", DEFAULT_MAX_BYTES)
        if request.get("action") != "extract" or not isinstance(path, str) or not isinstance(max_bytes, int):
            return {"ok": False, "error": "Unsupported control request."}
        if self._busy:
            return {"ok": False, "error": "Another transfer is running in pysnap connect."}
        self._busy = True
        try:
            data, checksum = await self._run_transfer(path, max_bytes)
        except ExtractError as error:
            return {"ok": False, "error": str(error)}
        finally:
            self._busy = False
        return {"ok": True, "data": base64.b64encode(data).decode("ascii"), "sha256": checksum}


def request_transfer(
    port: int,
    token: str,
    remote_path: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    connect_timeout: float = 5.0,
) -> tuple[bytes, str]:
    """Ask an attached ``pysnap connect`` process to transfer a file.

    :param port: Control port from the session record.
    :param token: Control secret from the session record.
    :param remote_path: Quoted shell path of the file.
    :param max_bytes: Largest accepted file size.
    :param connect_timeout: Seconds to wait for the control port.
    :returns: File content and its SHA-256 checksum.
    :raises ExtractError: When the session does not answer or the transfer
        fails.
    """
    request = {"token": token, "action": "extract", "path": remote_path, "max_bytes": max_bytes}
    try:
        with socket.create_connection((CONTROL_HOST, port), timeout=connect_timeout) as connection:
            connection.settimeout(None)  # The transfer ends on the guest's idle timeout.
            connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as error:
        raise ExtractError(f"The attached pysnap connect session did not answer: {error}") from None
    try:
        response = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:
        raise ExtractError("The attached pysnap connect session sent an invalid answer.") from None
    if not response.get("ok"):
        raise ExtractError(str(response.get("error") or "The transfer failed in pysnap connect."))
    try:
        data = base64.b64decode(response["data"], validate=True)
    except (KeyError, TypeError, binascii.Error, ValueError):
        raise ExtractError("The attached pysnap connect session sent damaged data.") from None
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != response.get("sha256"):
        raise ExtractError("The checksum of the received file does not match; try again.")
    return data, checksum
