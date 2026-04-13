"""Helpers for terminal control-sequence handling over serial TCP."""

from __future__ import annotations

from dataclasses import dataclass, field

from pysnap.terminal.emulator import TerminalEmulator

_CSI_PREFIX = b"\x1b["


@dataclass
class TerminalQueryResponder:
    """Recognize terminal queries and synthesize compatible replies.

    Raw serial consoles do not provide a PTY-style ``SIGWINCH`` channel into
    the guest system. PySnap therefore acts like a small terminal emulator and
    replies to xterm-compatible size queries that guest-side tools may emit.
    """

    _buffer: bytearray = field(default_factory=bytearray)

    def collect_responses(
        self,
        data: bytes,
        *,
        emulator: TerminalEmulator,
        columns: int,
        lines: int,
    ) -> list[bytes]:
        """Return terminal replies implied by one incoming byte chunk.

        :param data: Newly received bytes from the guest.
        :param emulator: Emulator holding the current cursor position.
        :param columns: Current visible terminal width.
        :param lines: Current visible terminal height.
        :returns: Zero or more response payloads to send back to the guest.
        """
        if not data:
            return []

        self._buffer.extend(data)
        responses: list[bytes] = []

        while self._buffer:
            prefix_index = self._buffer.find(_CSI_PREFIX)
            if prefix_index < 0:
                self._buffer[:] = _tail_fragment(self._buffer)
                break
            if prefix_index > 0:
                del self._buffer[:prefix_index]

            final_index = _find_csi_final_byte(self._buffer)
            if final_index is None:
                break

            parameters = bytes(self._buffer[len(_CSI_PREFIX) : final_index])
            final_byte = self._buffer[final_index]
            response = _build_response(
                parameters=parameters,
                final_byte=final_byte,
                emulator=emulator,
                columns=columns,
                lines=lines,
            )
            if response is not None:
                responses.append(response)

            del self._buffer[: final_index + 1]

        return responses


def _build_response(
    *,
    parameters: bytes,
    final_byte: int,
    emulator: TerminalEmulator,
    columns: int,
    lines: int,
) -> bytes | None:
    """Build one response payload for a parsed CSI query sequence.

    :param parameters: Parameter bytes between ``CSI`` and the final byte.
    :param final_byte: Final CSI byte.
    :param emulator: Terminal emulator with current cursor position.
    :param columns: Current visible width.
    :param lines: Current visible height.
    :returns: Response bytes or ``None`` when the sequence is unsupported.
    """
    if final_byte == ord("n") and parameters == b"6":
        row = emulator.screen.cursor.y + 1
        column = emulator.screen.cursor.x + 1
        return f"\x1b[{row};{column}R".encode()
    if final_byte == ord("n") and parameters == b"?6":
        row = emulator.screen.cursor.y + 1
        column = emulator.screen.cursor.x + 1
        return f"\x1b[?{row};{column}R".encode()
    if final_byte == ord("t") and parameters == b"18":
        return f"\x1b[8;{lines};{columns}t".encode()
    if final_byte == ord("t") and parameters == b"19":
        return f"\x1b[9;{lines};{columns}t".encode()
    return None


def _find_csi_final_byte(buffer: bytearray) -> int | None:
    """Return the index of the next CSI final byte in ``buffer``.

    :param buffer: Pending CSI parser buffer that starts with ``ESC [``.
    :returns: Final-byte index or ``None`` when the sequence is incomplete.
    """
    for index in range(len(_CSI_PREFIX), len(buffer)):
        candidate = buffer[index]
        if 0x40 <= candidate <= 0x7E:
            return index
    return None


def _tail_fragment(buffer: bytearray) -> bytes:
    """Keep only the incomplete CSI prefix tail from a processed buffer.

    :param buffer: Parser buffer.
    :returns: Trailing bytes that may start a CSI sequence.
    """
    if buffer.endswith(_CSI_PREFIX):
        return _CSI_PREFIX
    if buffer.endswith(b"\x1b"):
        return b"\x1b"
    return b""
