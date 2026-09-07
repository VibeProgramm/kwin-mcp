"""F1 crash-contract test: SIGSEGV guard after a failed EIS reconnect.

Regression for F1: a reconnect whose ``_setup`` fails (simulated here at the
``ei_new_sender`` step) must leave ``_ei == 0`` with a clean slate, and the
NEXT injection must surface as ToolError — never ``ei_get_fd(0)``/``ei_dispatch(0)``,
which segfaults real libei 1.6.0 (NULL-context deref, verified: exit 139).
The FakeLibei base class hard-asserts ``ei != 0`` on both calls, so reaching
libei with a released context fails the test instead of silently passing.

fd ownership (attempt 2, issue #229): the ``_setup`` cleanup-guard closes the
fd libei never saw, so the test must hand out an fd it OWNS — a real pipe
read-end via the ``FakeRemoteDesktopIface(fd=...)`` helper (same pattern as
the #229 lifecycle tests) instead of a sentinel number that could collide
with a live descriptor of the test process.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from test_input_device_lifecycle import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_RESUMED,
    KEYBOARD,
    POINTER,
    FakeClock,
    FakeLibei,
    FakeRemoteDesktopIface,
)

import kwin_mcp.input as input_module
from kwin_mcp.errors import ToolError


class ExplodingLibei(FakeLibei):
    """FakeLibei whose ei_new_sender fails after the first success."""

    def __init__(self) -> None:
        super().__init__(
            [
                (_EI_EVENT_DEVICE_ADDED, POINTER),
                (_EI_EVENT_DEVICE_ADDED, KEYBOARD),
                (_EI_EVENT_DEVICE_RESUMED, POINTER),
                (_EI_EVENT_DEVICE_RESUMED, KEYBOARD),
            ],
            {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
        )
        self.new_calls = 0

    def ei_new_sender(self, _a: int) -> int:
        self.new_calls += 1
        if self.new_calls > 1:
            msg = "simulated ei_new_sender failure"
            raise RuntimeError(msg)
        return self._next_id + 1

    def ei_get_fd(self, ei: int) -> int:
        assert ei != 0, "ei_get_fd(NULL) — SIGSEGV in real libei"
        return super().ei_get_fd(ei)

    def ei_dispatch(self, ei: int) -> None:
        assert ei != 0, "ei_dispatch(NULL) — SIGSEGV in real libei"
        super().ei_dispatch(ei)


def test_f1_no_segfault_after_failed_reconnect(monkeypatch) -> None:
    """Failed reconnect -> next injection is ToolError, never ei_get_fd(NULL)."""
    read_fd, write_fd = os.pipe()
    try:
        fake = ExplodingLibei()
        monkeypatch.setattr(input_module, "_get_libei", lambda: fake)
        monkeypatch.setattr(input_module.select, "select", lambda *_a, **_k: ([], [], []))
        # Fast-forward the 5s stall wait of the first injection (sibling tests do
        # the same); the second call short-circuits on the _ei == 0 guard anyway.
        monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

        # A real pipe read-end instead of the sentinel fd 11: the guard closes
        # the fd libei never saw, so the test must own whatever it closes.
        # The fake libei records setup_fds but never closes the fd itself —
        # exactly what real libei would own after ei_setup_backend_fd.
        iface = FakeRemoteDesktopIface(fd=read_fd)

        class FakeBus:
            def get_object(self, *_a: object, **_k: object) -> object:
                return object()

        monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: iface)
        monkeypatch.setattr(input_module.dbus.bus, "BusConnection", lambda _addr: FakeBus())
        monkeypatch.setattr(input_module, "DBusGMainLoop", lambda **_k: None)

        client = input_module.EISClient("unix:path=/tmp/nonexistent")
        assert client._pointer == POINTER
        assert client._keyboard == KEYBOARD
        # First injection: stalled devices (pause without resume) -> reconnect ->
        # teardown ok -> _setup -> ei_new_sender raises -> ToolError (F5 contract).
        client._emulating_devices = set()
        with pytest.raises(ToolError):
            client.keyboard_key(30, 1)
        assert client._ei == 0
        # Second attempt: ToolError again (no SIGSEGV, no infinite retry loop),
        # the NULL context never reached ei_get_fd/ei_dispatch.
        with pytest.raises(ToolError):
            client.keyboard_key(31, 1)
        assert client._ei == 0
        # fd ownership (attempt 2, issue #229): the guard closed the fd libei
        # never saw — the read-end is gone (EBADF) while the never-handed-out
        # write-end lives. Three disconnects, one per handed-out cookie (the
        # initial connection released on the first reconnect + one per failed
        # _setup) — never a double disconnect of the same cookie: the teardown
        # zeroes it, so each 42 is a freshly issued one.
        assert iface.disconnected == [42, 42, 42]
        with pytest.raises(OSError):
            os.fstat(read_fd)
        os.fstat(write_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)  # already closed when the guard works
        os.close(write_fd)
