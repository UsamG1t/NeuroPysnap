"""Translate prompt_toolkit key presses into byte sequences for the guest."""

from __future__ import annotations

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys


SPECIAL_KEY_SEQUENCES: dict[Keys, bytes] = {
    Keys.Up: b"\x1b[A",
    Keys.Down: b"\x1b[B",
    Keys.Right: b"\x1b[C",
    Keys.Left: b"\x1b[D",
    Keys.Home: b"\x1b[H",
    Keys.End: b"\x1b[F",
    Keys.Insert: b"\x1b[2~",
    Keys.Delete: b"\x1b[3~",
    Keys.PageUp: b"\x1b[5~",
    Keys.PageDown: b"\x1b[6~",
    Keys.BackTab: b"\x1b[Z",
    Keys.F1: b"\x1bOP",
    Keys.F2: b"\x1bOQ",
    Keys.F3: b"\x1bOR",
    Keys.F4: b"\x1bOS",
    Keys.F5: b"\x1b[15~",
    Keys.F6: b"\x1b[17~",
    Keys.F7: b"\x1b[18~",
    Keys.F8: b"\x1b[19~",
    Keys.F9: b"\x1b[20~",
    Keys.F10: b"\x1b[21~",
    Keys.F11: b"\x1b[23~",
    Keys.F12: b"\x1b[24~",
}


def key_press_to_bytes(key_press: KeyPress) -> bytes | None:
    """Translate one key press into bytes for the VM serial console.

    :param key_press: Prompt-toolkit key press.
    :returns: Bytes to send or ``None`` when the key is unsupported.
    """
    key = key_press.key
    data = key_press.data

    if isinstance(key, Keys) and data == "":
        # prompt_toolkit splits some sequences, such as Alt+Up from an xterm
        # (``ESC [ 1 ; 3 A``), into several keys; the first one carries all
        # bytes and the following ones carry none.
        return None
    if isinstance(key, Keys):
        if key in SPECIAL_KEY_SEQUENCES:
            return SPECIAL_KEY_SEQUENCES[key]
        if key == Keys.BracketedPaste:
            return paste_to_bytes(data, bracketed=False)
        if data and not data.startswith("<"):
            return data.encode("utf-8", errors="replace")
        return None

    return str(key).encode("utf-8", errors="replace")


PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def paste_to_bytes(text: str, *, bracketed: bool) -> bytes:
    """Return pasted text as the bytes a terminal sends for it.

    Line breaks of any system (``CR LF``, ``LF``) become ``CR``, the Enter key
    of a terminal, so a text copied on Windows does not give empty lines. When
    the guest asked for bracketed paste (``ESC [ ? 2004 h``, as bash and vim
    do), the text is put between ``ESC [ 200 ~`` and ``ESC [ 201 ~``; markers
    inside the text are removed, so that it cannot end the paste early.

    :param text: Pasted text.
    :param bracketed: Whether the guest enabled bracketed paste.
    :returns: UTF-8 bytes for the guest.
    """
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    if bracketed:
        text = PASTE_START + text.replace(PASTE_START, "").replace(PASTE_END, "") + PASTE_END
    return text.encode("utf-8", errors="replace")
