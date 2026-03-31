"""Unit tests for terminal TCP transport helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pysnap.terminal.transport import _candidate_hosts, open_serial_connection


class TransportTests(unittest.IsolatedAsyncioTestCase):
    """Verify loopback transport behavior."""

    def test_candidate_hosts_include_ipv4_ipv6_and_localhost(self) -> None:
        """Expand loopback host requests into cross-platform candidates."""
        self.assertEqual(
            _candidate_hosts("127.0.0.1"),
            ("127.0.0.1", "localhost", "::1"),
        )
        self.assertEqual(
            _candidate_hosts("localhost"),
            ("localhost", "127.0.0.1", "::1"),
        )

    async def test_open_serial_connection_falls_back_to_alternate_loopback_host(self) -> None:
        """Try alternate loopback targets when the first address is unavailable."""
        calls: list[tuple[str, int]] = []

        async def fake_open_connection(host: str, port: int) -> tuple[str, str]:
            calls.append((host, port))
            if host == "localhost":
                raise OSError("localhost failed")
            return ("reader", "writer")

        with patch("pysnap.terminal.transport.asyncio.open_connection", fake_open_connection):
            reader, writer = await open_serial_connection("localhost", 2345, timeout=0.1)

        self.assertEqual((reader, writer), ("reader", "writer"))
        self.assertEqual(
            calls,
            [("localhost", 2345), ("127.0.0.1", 2345)],
        )


if __name__ == "__main__":
    unittest.main()
