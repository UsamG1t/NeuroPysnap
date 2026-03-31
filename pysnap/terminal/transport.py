"""TCP transport helpers for VM serial console connections."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import contextmanager
import socket
from time import monotonic, sleep
from typing import Iterator

from pysnap.errors import PySnapError


def _candidate_hosts(host: str) -> tuple[str, ...]:
    """Return connection candidates for one requested host value.

    macOS loopback services are not always reachable through the same textual
    loopback address as Linux, so PySnap tries a small set of equivalent
    loopback targets when a local host is requested.

    :param host: Requested host name.
    :returns: Ordered unique host candidates.
    """
    local_hosts = {"127.0.0.1", "::1", "localhost"}
    if host not in local_hosts:
        return (host,)
    return _unique_hosts((host, "localhost", "127.0.0.1", "::1"))


def _unique_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    """Return hosts in input order without duplicates.

    :param hosts: Host candidates.
    :returns: Unique host values.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in hosts:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return tuple(ordered)


def open_serial_socket(
    host: str,
    port: int,
    timeout: float = 30.0,
    retry_delay: float = 0.25,
) -> socket.socket:
    """Wait for and open a blocking TCP socket to the VM serial port.

    :param host: Target host name.
    :param port: Target TCP port.
    :param timeout: Maximum time to wait.
    :param retry_delay: Delay between connection attempts.
    :returns: Connected socket object.
    :raises PySnapError: If the TCP server does not become available in time.
    """
    deadline = monotonic() + timeout
    last_error: OSError | None = None

    while monotonic() < deadline:
        remaining = max(deadline - monotonic(), 0.1)
        connect_timeout = min(remaining, max(retry_delay, 0.1), 1.0)
        for candidate_host in _candidate_hosts(host):
            try:
                connection = socket.create_connection(
                    (candidate_host, port),
                    timeout=connect_timeout,
                )
                connection.settimeout(1.0)
                return connection
            except OSError as error:
                last_error = error
        sleep(retry_delay)

    if last_error is None:
        raise PySnapError(f"Timed out while waiting for serial TCP port {port}.")
    raise PySnapError(
        f"Timed out while waiting for serial TCP port {port}: {last_error}"
    )


@contextmanager
def serial_connection_probe(
    host: str,
    port: int,
    timeout: float = 30.0,
    retry_delay: float = 0.25,
) -> Iterator[socket.socket]:
    """Open a short-lived serial TCP connection for automated checks.

    The probe writes one newline to verify that the transport accepts client
    output and then keeps the connection open for the caller-managed context.

    :param host: Target host name.
    :param port: Target TCP port.
    :param timeout: Maximum time to wait.
    :param retry_delay: Delay between connection attempts.
    :yields: Connected socket object.
    """
    connection = open_serial_socket(
        host=host,
        port=port,
        timeout=timeout,
        retry_delay=retry_delay,
    )
    try:
        connection.sendall(b"\n")
        yield connection
    finally:
        connection.close()


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
        for candidate_host in _candidate_hosts(host):
            try:
                return await asyncio.open_connection(candidate_host, port)
            except OSError as error:
                last_error = error
        await asyncio.sleep(retry_delay)

    if last_error is None:
        raise PySnapError(f"Timed out while waiting for serial TCP port {port}.")
    raise PySnapError(
        f"Timed out while waiting for serial TCP port {port}: {last_error}"
    )
