"""Tests for the EIS TEXT-capability routing in InputBackend (A-1 fix).

wingman #227 / report #224 A-1: text keys typed via bare evdev keycodes on the
EIS keyboard device were not delivered in the virtual session, because the
client never consulted the server-provided XKB keymap. KWin (libei >= 1.6)
offers an ei "text" device whose events are resolved server-side
(EIS_EVENT_TEXT_KEYSYM -> EisDevice::sendKeySym). The fix routes character
typing and unmodified key presses through the text device, keeping the
existing bare-keycode path for modifier combos (which work and must not be
broken).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kwin_mcp.input import (
    _ASCII_TO_KEYSYM,
    InputBackend,
    ascii_char_to_keysym,
    key_name_to_keysym,
)


@pytest.mark.parametrize(
    ("char", "keysym"),
    [
        ("a", 0x61),
        ("Z", 0x5A),
        ("5", 0x35),
        ("!", 0x21),
        (" ", 0x20),
    ],
)
def test_ascii_char_to_keysym(char: str, keysym: int) -> None:
    """Printable ASCII maps directly to its XKB keysym (same codepoint)."""
    assert ascii_char_to_keysym(char) == keysym


def test_ascii_control_chars() -> None:
    """Newline/tab map to their control keysyms."""
    assert ascii_char_to_keysym("\n") == 0xFF0D  # XK_Return
    assert ascii_char_to_keysym("\t") == 0xFF09  # XK_Tab
    assert ascii_char_to_keysym("\r") is None
    assert ascii_char_to_keysym("\x1b") is None


def test_special_key_names_to_keysym() -> None:
    """Named special keys resolve to XKB keysyms."""
    assert key_name_to_keysym("Return") == 0xFF0D
    assert key_name_to_keysym("Tab") == 0xFF09
    assert key_name_to_keysym("Escape") == 0xFF1B
    assert key_name_to_keysym("BackSpace") == 0xFF08
    assert key_name_to_keysym("Delete") == 0xFFFF
    assert key_name_to_keysym("up") == 0xFF52
    assert key_name_to_keysym("F1") == 0xFFBE
    assert key_name_to_keysym("F12") == 0xFFC9
    assert key_name_to_keysym("space") == 0x20
    assert key_name_to_keysym("Home") == 0xFF50
    assert key_name_to_keysym("End") == 0xFF57
    assert key_name_to_keysym("Page_Up") == 0xFF55
    assert key_name_to_keysym("Page_Down") == 0xFF56
    assert key_name_to_keysym("Insert") == 0xFF63
    assert key_name_to_keysym("Print") == 0xFF61
    assert key_name_to_keysym("Pause") == 0xFF13
    assert key_name_to_keysym("Caps_Lock") == 0xFFE5
    assert key_name_to_keysym("Num_Lock") == 0xFF7F
    assert key_name_to_keysym("Menu") == 0xFF67


def test_ascii_table_covers_printable_ascii() -> None:
    """Every printable ASCII character has a keysym entry."""
    for code in range(0x20, 0x7F):
        assert chr(code) in _ASCII_TO_KEYSYM


def _backend_with_text_device() -> tuple[InputBackend, MagicMock]:
    """Build an InputBackend whose EIS client is a mock with a text device."""
    backend = InputBackend.__new__(InputBackend)
    client = MagicMock()
    client.has_text_device = True
    backend._client = client
    return backend, client


def _backend_without_text_device() -> tuple[InputBackend, MagicMock]:
    """Build an InputBackend whose EIS client is a mock without a text device."""
    backend = InputBackend.__new__(InputBackend)
    client = MagicMock()
    client.has_text_device = False
    backend._client = client
    return backend, client


def test_keyboard_type_routes_through_text_device() -> None:
    """With a text device, typing uses text_keysym per character."""
    backend, client = _backend_with_text_device()
    backend.keyboard_type("hi\n")
    sent = client.text_keysym.call_args_list
    assert [(c.args[0], c.args[1]) for c in sent] == [
        (0x68, True),
        (0x68, False),
        (0x69, True),
        (0x69, False),
        (0xFF0D, True),
        (0xFF0D, False),
    ]
    client.keyboard_key.assert_not_called()


def test_keyboard_type_falls_back_without_text_device() -> None:
    """Without a text device (old libei/KWin), typing uses the keycode path."""
    backend, client = _backend_without_text_device()
    backend.keyboard_type("hi")
    assert client.keyboard_key.call_count > 0
    client.text_keysym.assert_not_called()


def test_keyboard_key_unmodified_uses_text_device() -> None:
    """A bare text key (no modifiers) goes through the text device."""
    backend, client = _backend_with_text_device()
    backend.keyboard_key("t")
    sent = client.text_keysym.call_args_list
    assert [(c.args[0], c.args[1]) for c in sent] == [(0x74, True), (0x74, False)]
    client.keyboard_key.assert_not_called()


def test_keyboard_key_special_key_uses_text_device() -> None:
    """A bare special key (Return) goes through the text device."""
    backend, client = _backend_with_text_device()
    backend.keyboard_key("Return")
    sent = client.text_keysym.call_args_list
    assert [(c.args[0], c.args[1]) for c in sent] == [(0xFF0D, True), (0xFF0D, False)]


def test_keyboard_key_combo_keeps_keycode_path() -> None:
    """Modifier combos must keep the bare-keycode path (not break them)."""
    backend, client = _backend_with_text_device()
    backend.keyboard_key("ctrl+q")
    client.text_keysym.assert_not_called()
    # ctrl (29) + q (16 = KEY_Q) pressed and released via the keyboard device.
    codes = [c.args[0] for c in client.keyboard_key.call_args_list]
    assert codes == [29, 16, 16, 29]  # ctrl down, q down, q up, ctrl up


def test_keyboard_key_unknown_key_is_noop() -> None:
    """A key with neither a keysym nor an evdev mapping is a silent no-op."""
    backend, client = _backend_with_text_device()
    backend.keyboard_key("f13")  # not in any mapping table
    client.text_keysym.assert_not_called()
    client.keyboard_key.assert_not_called()


def test_keyboard_type_unicode_chars_skipped_in_ascii_path() -> None:
    """Non-ASCII characters are skipped by keyboard_type (ASCII-only tool)."""
    backend, client = _backend_with_text_device()
    backend.keyboard_type("hi\u043f\u0440\u0438\u0432\u0435\u0442")
    sent = client.text_keysym.call_args_list
    assert [(c.args[0], c.args[1]) for c in sent] == [
        (0x68, True),
        (0x68, False),
        (0x69, True),
        (0x69, False),
    ]
