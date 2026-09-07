"""Cross-check of the EI event-type constants against the installed libei.

The input fakes import the event constants from ``kwin_mcp.input`` and feed
them back into the code under test, so a wrong module constant is invisible
to the lifecycle tests (a circular fake — the PAUSED constant shipped as 9,
actually KEYBOARD_MODIFIERS, and every test passed). These tests pin the
numbering against two independent sources:

1. the installed libei header (``enum ei_event_type``), skipped when the
   header is not installed (e.g. the CI test job),
2. hardcoded literals verified against the libei 1.6.0 sources — independent
   of both the module constants and the header availability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kwin_mcp.input as input_module

#: libei development headers (Arch/Debian/Fedora layout: libei-1.0).
_LIBEI_HEADER = Path("/usr/include/libei-1.0/libei.h")

#: The sender-relevant subset the module tracks (FRAME/PONG are not consumed).
_TRACKED_EVENT_NAMES = (
    "CONNECT",
    "DISCONNECT",
    "SEAT_ADDED",
    "SEAT_REMOVED",
    "DEVICE_ADDED",
    "DEVICE_REMOVED",
    "DEVICE_PAUSED",
    "DEVICE_RESUMED",
    "KEYBOARD_MODIFIERS",
)


def _parse_header_event_enum(text: str) -> dict[str, int]:
    """Extract the ``EI_EVENT_*`` enumerator values from the libei.h C enum."""
    body = text.split("enum ei_event_type", 1)[1]
    body = body.split("};", 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)
    values: dict[str, int] = {}
    current = 0
    for match in re.finditer(r"(EI_EVENT_\w+)\s*(?:=\s*(\d+))?", body):
        explicit = match.group(2)
        current = int(explicit) if explicit is not None else current + 1
        values[match.group(1)] = current
    return values


def test_module_constants_match_installed_header() -> None:
    """Every module constant equals the installed header's enum value."""
    if not _LIBEI_HEADER.is_file():
        pytest.skip(f"libei header not installed: {_LIBEI_HEADER}")
    values = _parse_header_event_enum(_LIBEI_HEADER.read_text(encoding="utf-8"))
    for name in _TRACKED_EVENT_NAMES:
        module_value = getattr(input_module, f"_EI_EVENT_{name}")
        header_value = values[f"EI_EVENT_{name}"]
        assert module_value == header_value, (
            f"_EI_EVENT_{name}={module_value} drifted from libei.h ({header_value})"
        )


def test_event_constants_hardcoded_literals() -> None:
    """Enum truth from libei 1.6.0 sources, as literals (independent guard).

    Uses literal ints, never the module constants, so a drifted constant
    cannot pass by feeding itself back (the circular-fake failure mode).
    """
    assert input_module._EI_EVENT_CONNECT == 1
    assert input_module._EI_EVENT_DISCONNECT == 2
    assert input_module._EI_EVENT_DEVICE_PAUSED == 7
    assert input_module._EI_EVENT_DEVICE_RESUMED == 8
    assert input_module._EI_EVENT_KEYBOARD_MODIFIERS == 9
