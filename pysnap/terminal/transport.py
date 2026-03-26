"""TCP transport helpers for VM serial console connections."""

from __future__ import annotations

import asyncio
from time import monotonic

from pysnap.errors import PySnapError


async def open_serial_connection(
    host: str,
    port: int,
    timeout: float = 30.0,
    retry_delay: float = 0.25,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wait for and open a TCP connection to the VM serial port.

    :param host: Target host name.
    :param port: Target TCP port.
    :param timeout: Maximum time to wait.
    :param retry_delay: Delay between connection attempts.
    :returns: Reader and writer stream objects.
    :raises PySnapError: If the TCP server does not become available in time.
    """
    deadline = monotonic() + timeout
    last_error: OSError | None = None

    while monotonic() < deadline:
        try:
            return await asyncio.open_connection(host, port)
        except OSError as error:
            last_error = error
            await asyncio.sleep(retry_delay)

    if last_error is None:
        raise PySnapError(f"Timed out while waiting for serial TCP port {port}.")
    raise PySnapError(
        f"Timed out while waiting for serial TCP port {port}: {last_error}"
    )
