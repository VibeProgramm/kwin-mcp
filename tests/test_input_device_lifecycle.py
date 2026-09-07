"""Tests for EIS device lifecycle tracking and stall recovery.

Adopted from 01SW/kwin-mcp: KWin pauses (and sometimes removes/re-adds) its
EIS devices around input bursts — observed right after session start and
after modifier presses. The client must therefore:

- track DEVICE_ADDED/REMOVED/RESUMED/PAUSED events wherever they arrive
  (``_handle_event`` is shared by the handshake, ``_flush`` and the
  readiness wait),
- wait for pointer + keyboard to be emulating before every injection
  (``_ensure_devices_ready``),
- rebuild the whole EIS connection when the compositor stalls without
  resuming (``_reconnect``), including releasing active touches and
  bookkeeping so stale pointers cannot be used afterwards.

The fake libei below follows the same pattern as test_input_resumed.py:
device pointers are plain ints, events are queued (type, device) tuples and
popped in order; ``select`` is patched out so the drain loops run without a
real file descriptor.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import dbus
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import kwin_mcp.input as input_module
from kwin_mcp.input import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_CAP_TEXT,
    _EI_CAP_TOUCH,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_REMOVED,
    _EI_EVENT_DEVICE_RESUMED,
    _EI_EVENT_DISCONNECT,
    _EI_EVENT_SEAT_REMOVED,
    _PRESSED,
    _RELEASED,
    EISClient,
)

POINTER = 0x101
KEYBOARD = 0x102
TOUCH = 0x103
NEW_POINTER = 0x201
NEW_KEYBOARD = 0x202
TEXT_DEV = 0x104  # stale-connection text device (paused)
NEW_TEXT_DEV = 0x204  # fresh-connection text device
NEW_TOUCH_DEV = 0x205  # fresh-connection touch device
COOKIE = 7

# Literal event-type int (issue #24): tests must not feed the module
# constant back — a wrong constant would pass its own tests. The value is
# pinned against libei.h by test_libei_constants.py.
EI_PAUSED = 7


class FakeLibei:
    """Minimal libei stub covering device lifecycle calls and injection.

    ``ei_dispatch`` mirrors the real libei 1.6 API: VOID. Death is modelled
    by queueing a DISCONNECT event (``queue_events`` /
    ``DispatchInjectingLibei``), never by a return code — the old
    ``dispatch_result`` knob modelled an API that does not exist (issue #22).
    """

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
    ) -> None:
        self._events = list(events)
        self._event_meta: dict[int, tuple[int, int]] = {}
        self._next_id = 0
        self.device_caps = device_caps
        self.started: list[tuple[int, int]] = []  # (device, sequence)
        self.stopped: list[int] = []
        self.unrefed_devices: list[int] = []
        self.unrefed_ei: list[int] = []
        self.unrefed_touches: list[int] = []
        self.key_calls: list[tuple[int, int]] = []  # (keycode, state)
        self.button_calls: list[tuple[int, int]] = []
        self.text_keysym_calls: list[tuple[int, int, int]] = []  # (device, keysym, state)
        self.text_utf8_calls: list[tuple[int, bytes]] = []  # (device, encoded text)
        self.frames: list[int] = []  # device per frame call
        self.setup_fds: list[int] = []  # fds handed to ei_setup_backend_fd
        self.setup_backend_fd_result = 0  # ei_setup_backend_fd return value
        self.new_sender_result = 101  # ei_new_sender return value (0 = failure)
        self.touch_downs: list[tuple[int, float, float]] = []
        self.touch_motions: list[tuple[int, float, float]] = []
        self.touch_ups: list[int] = []

    def queue_events(self, events: list[tuple[int, int]]) -> None:
        """Queue server events for the next drain (pause/resume simulations)."""
        self._events.extend(events)

    def ei_get_fd(self, ei: int) -> int:
        # Guard for F1: after a failed reconnect the client must never probe
        # libei with a NULL (0) EI context — the real libei 1.6.0 segfaults
        # (exit 139) on ei_get_fd(NULL)/ei_dispatch(NULL), killing the whole
        # MCP server.
        assert ei != 0, "ei_get_fd called with NULL EI context (segfault guard)"
        return 9

    def ei_dispatch(self, ei: int) -> None:
        """Void dispatch — the real libei 1.6 signature (issue #22)."""
        assert ei != 0, "ei_dispatch called with NULL EI context (segfault guard)"

    def ei_get_event(self, ei: int) -> int:
        if not self._events:
            return 0
        self._next_id += 1
        self._event_meta[self._next_id] = self._events.pop(0)
        return self._next_id

    def ei_event_get_type(self, event: int) -> int:
        return self._event_meta[event][0]

    def ei_event_get_device(self, event: int) -> int:
        return self._event_meta[event][1]

    def ei_event_unref(self, event: int) -> None:
        return None

    def ei_device_has_capability(self, device: int, cap: int) -> int:
        return 1 if cap in self.device_caps.get(device, set()) else 0

    def ei_device_ref(self, device: int) -> int:
        return device

    def ei_device_unref(self, device: int) -> None:
        self.unrefed_devices.append(device)

    def ei_device_start_emulating(self, device: int, sequence: int) -> None:
        self.started.append((device, sequence))

    def ei_device_stop_emulating(self, device: int) -> None:
        self.stopped.append(device)

    def ei_unref(self, ei: int) -> None:
        self.unrefed_ei.append(ei)

    def ei_device_keyboard_key(self, device: int, keycode: int, state: int) -> None:
        self.key_calls.append((keycode, state))

    def ei_device_button_button(self, device: int, button: int, state: int) -> None:
        self.button_calls.append((button, state))

    def ei_device_text_keysym(self, device: int, keysym: int, state: int) -> None:
        self.text_keysym_calls.append((device, keysym, state))

    def ei_device_text_utf8(self, device: int, text: bytes) -> None:
        self.text_utf8_calls.append((device, text))

    def ei_device_pointer_motion_absolute(self, device: int, x: float, y: float) -> None:
        return None

    def ei_device_frame(self, device: int, time_us: int) -> None:
        self.frames.append(device)

    def ei_device_touch_new(self, device: int) -> int:
        return 0x900 + len(self.touch_downs)

    def ei_touch_down(self, touch: int, x: float, y: float) -> None:
        self.touch_downs.append((touch, x, y))

    def ei_touch_motion(self, touch: int, x: float, y: float) -> None:
        self.touch_motions.append((touch, x, y))

    def ei_touch_up(self, touch: int) -> None:
        self.touch_ups.append(touch)

    def ei_touch_unref(self, touch: int) -> None:
        self.unrefed_touches.append(touch)

    # Connection-setup calls exercised by the real-_setup tests (B5).
    def ei_configure_name(self, ei: int, name: bytes) -> None:
        return None

    def ei_new_sender(self, _arg: int) -> int:
        return self.new_sender_result

    def ei_setup_backend_fd(self, ei: int, fd: int) -> int:
        self.setup_fds.append(fd)
        return self.setup_backend_fd_result


class SwitchingLibei:
    """libei router handing each new EI context (ei_new_sender) its own fake.

    Used by the real-``_setup`` reconnect tests: the stale connection's fake
    serves nothing, the fresh connection's fake serves the handshake events.
    """

    def __init__(self, fakes: list[FakeLibei]) -> None:
        self._fakes = fakes
        self._index = 0

    @property
    def current(self) -> FakeLibei:
        return self._fakes[self._index]

    def ei_new_sender(self, _arg: int) -> int:
        if self._index < len(self._fakes) - 1:
            self._index += 1
        return 100 + self._index

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current, name)


class FakeClock:
    """Monotonic clock advancing by a fixed step per call (fast-forwards waits)."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._now += self._step
        return self._now


class LateEventFakeLibei(FakeLibei):
    """FakeLibei appending late events after N drains ended on an empty queue.

    Models a server that advertises/resumes a device only AFTER the pointer +
    keyboard handshake completes (issue #228): ``_drain_events`` empties the
    queue in a single pass, so plain event ordering cannot express "later".
    The trigger fires when ``ei_get_event`` returns empty for the Nth time —
    the N-1-th empty return ended the handshake's last drain, so the late
    events land in the queue after it, awaiting the next drain loop.
    """

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
        late_events: list[tuple[int, int]],
        late_caps: dict[int, set[int]],
        late_after_drains: int,
    ) -> None:
        super().__init__(events, device_caps)
        self._late_events = list(late_events)
        self._late_caps = dict(late_caps)
        self._late_after_drains = late_after_drains
        self._empty_returns = 0

    def ei_get_event(self, ei: int) -> int:
        event = super().ei_get_event(ei)
        if event == 0 and self._late_events:
            self._empty_returns += 1
            if self._empty_returns >= self._late_after_drains:
                self._events.extend(self._late_events)
                self.device_caps.update(self._late_caps)
                self._late_events = []  # fire once
        return event


class FakeIface:
    """D-Bus EIS interface stub recording disconnect(cookie) calls."""

    def __init__(self) -> None:
        self.disconnected: list[int] = []

    def disconnect(self, cookie: int) -> None:
        self.disconnected.append(int(cookie))


class FakeFd:
    """D-Bus unixfd stub for the real-_setup tests."""

    def __init__(self, fd: int = 11) -> None:
        self._fd = fd

    def take(self) -> int:
        return self._fd


class FakeRemoteDesktopIface:
    """D-Bus EIS interface stub used by real-_setup tests (connectToEIS).

    The handed-out fd defaults to the sentinel 11; tests that pin fd
    ownership pass a real (pipe) descriptor instead.
    """

    def __init__(self, fd: int = 11) -> None:
        self.disconnected: list[int] = []
        self._fd = fd

    def connectToEIS(self, caps: int, timeout: float = 25.0) -> tuple[FakeFd, int]:  # noqa: N802
        return (FakeFd(self._fd), 42)

    def disconnect(self, cookie: int) -> None:
        self.disconnected.append(int(cookie))


class FakeBus:
    """BusConnection stub whose get_object always succeeds."""

    def get_object(self, *_args: Any, **_kwargs: Any) -> object:
        return object()


def _client(fake: FakeLibei) -> EISClient:
    """An EISClient that skipped __init__ (no D-Bus, no libei load)."""
    client = EISClient.__new__(EISClient)
    client._ei = 1
    client._pointer = POINTER
    client._keyboard = KEYBOARD
    client._touch_device = 0
    client._text_device = 0
    client._sequence = 0
    client._emulating_devices = {POINTER, KEYBOARD}
    client._active_touches = {}
    client._next_touch_id = 0
    client._held_keys = set()
    client._held_buttons = set()
    client._connection_dead = False
    client._eis_iface = None
    client._cookie = 0
    return client


def _install(monkeypatch: Any, fake: FakeLibei) -> None:
    monkeypatch.setattr(input_module, "_get_libei", lambda: fake)
    # Never readable: the wait loop still drains the event queue below.
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))


def _reconnecting_setup(client: EISClient, emulating: bool) -> tuple[Any, list[int]]:
    """Build a _setup stub simulating a fresh handshake after _reconnect.

    Mirrors the real ``_setup`` contract: a fresh, non-zero EI context is
    installed (101 = the fresh-context id SwitchingLibei hands out), so
    injections after a successful reconnect never run against a NULL context
    (the FakeLibei segfault guard asserts exactly that).
    """
    calls: list[int] = []

    def fake_setup() -> None:
        calls.append(1)
        client._ei = 101
        client._pointer = NEW_POINTER
        client._keyboard = NEW_KEYBOARD
        client._touch_device = 0
        client._text_device = 0
        if emulating:
            client._emulating_devices = {NEW_POINTER, NEW_KEYBOARD}

    return fake_setup, calls


class DispatchInjectingLibei(FakeLibei):
    """FakeLibei queueing server events from inside ``ei_dispatch``.

    Fires once: the first post-send ``_flush`` dispatch of the call under
    test extends the event queue, so the drain inside that same ``_flush``
    observes the injected events — the window pre-queued events cannot hit
    (the pre-send readiness drain, F8, would consume them first).

    This is also the death-injection vehicle since the dispatch-contract
    correction (issue #22): ``ei_dispatch`` is void in libei 1.6, so a
    server/socket death is modelled by queueing a DISCONNECT event from
    inside the dispatch — the flush's own drain then observes it and the
    honest-delivery raise fires.
    """

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
        inject: list[tuple[int, int]],
    ) -> None:
        super().__init__(events, device_caps)
        self._inject = list(inject)
        self._armed = True

    def ei_dispatch(self, ei: int) -> None:
        assert ei != 0, "ei_dispatch called with NULL EI context (segfault guard)"
        if self._armed:
            self._armed = False
            self._events.extend(self._inject)


def test_wait_emulating_true_when_devices_emulating(monkeypatch) -> None:
    """Emulating devices short-circuit the wait without draining anything."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    assert client._wait_emulating(0.05) is True
    assert fake.started == []


def test_wait_emulating_resumes_after_pause_events(monkeypatch) -> None:
    """PAUSED devices become usable again once RESUMED events are drained."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()  # both devices currently paused

    assert client._wait_emulating(0.5) is True
    assert [d for d, _ in fake.started] == sorted([POINTER, KEYBOARD])
    assert client._emulating_devices == {POINTER, KEYBOARD}


def test_wait_emulating_times_out_when_never_resumed(monkeypatch) -> None:
    """No resume events → the wait expires and reports False."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()

    assert client._wait_emulating(0.05) is False
    assert fake.started == []


def test_resume_device_is_idempotent_on_duplicate_resumed(monkeypatch) -> None:
    """A duplicate RESUMED for an emulating device must not restart emulation.

    Regression for B7: the second RESUMED used to call start_emulating again
    (with a burned sequence number), diverging from _pause_device's
    symmetric bookkeeping.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()

    client._wait_emulating(0.5)

    assert fake.started == [(POINTER, 1)]
    assert client._sequence == 1


def test_ensure_devices_ready_recovers_after_pause(monkeypatch) -> None:
    """pause → RESUMED in the queue → injection proceeds without reconnect."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client._ensure_devices_ready(timeout_s=0.5)

    assert setup_calls == []  # no stall → no reconnect
    assert client._emulating_devices == {POINTER, KEYBOARD}


def test_ensure_devices_ready_reconnects_on_stall(monkeypatch) -> None:
    """pause → stall → _reconnect → fresh handshake → devices usable again."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()
    client._eis_iface = FakeIface()
    client._cookie = COOKIE
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client._ensure_devices_ready(timeout_s=0.05)

    assert setup_calls == [1]
    assert client._eis_iface.disconnected == [COOKIE]
    # Old devices and the old EI context were torn down.
    assert POINTER in fake.unrefed_devices
    assert KEYBOARD in fake.unrefed_devices
    assert fake.unrefed_ei == [1]
    # Fresh handshake re-registered and re-started the new devices.
    assert client._pointer == NEW_POINTER
    assert client._keyboard == NEW_KEYBOARD
    assert client._emulating_devices == {NEW_POINTER, NEW_KEYBOARD}
    assert client._sequence == 0


def test_ensure_devices_ready_reconnects_after_unresumed_pause(monkeypatch) -> None:
    """PAUSED queued without a RESUMED → the injection itself rebuilds and lands.

    Covers the B6 reconnect path: _pause_device discards the device from the
    emulating set. Since issue #228 the readiness gate drains the event queue
    BEFORE probing readiness, so the queued pause is observed before the
    injection — the same call rebuilds the connection and the injection is
    delivered to the fresh devices. The old probe-first ordering sent the
    button into the already-paused device (silent drop; libei discards events
    from paused devices) and only recovered on the NEXT injection.
    """
    fake = FakeLibei([(EI_PAUSED, POINTER)], {})
    _install(monkeypatch, fake)
    # Issue #235: the pre-reconnect stall wait is 0.5s now (was 5s), so the
    # clock step must stay below that budget for the wait loop to actually
    # run and observe the queued pause — the test's semantics are unchanged,
    # only the modelled timing constant moved.
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.pointer_button(0x110, _PRESSED)  # gate drains PAUSED, reconnects, delivers
    assert setup_calls == [1]
    assert client._emulating_devices == {NEW_POINTER, NEW_KEYBOARD}
    # The injection landed on the fresh pointer, not the paused stale one.
    assert fake.button_calls == [(0x110, _PRESSED)]
    assert fake.frames == [NEW_POINTER]


def test_ensure_devices_ready_reconnect_runs_real_negotiation(monkeypatch) -> None:
    """Stall → reconnect with the REAL _setup/_negotiate_devices → success.

    Regression for B5(c): only the D-Bus plumbing is faked; the actual
    negotiation (ADDED → RESUMED → start_emulating) runs against the fake
    libei of the fresh connection.
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())

    client = _client(stale)
    client._emulating_devices = set()  # stalled: paused, no resume queued
    client._bus = FakeBus()

    client._ensure_devices_ready(timeout_s=0.05)

    assert client._pointer == NEW_POINTER
    assert client._keyboard == NEW_KEYBOARD
    assert client._emulating_devices == {NEW_POINTER, NEW_KEYBOARD}
    assert fresh.setup_fds == [11]
    assert sorted(device for device, _ in fresh.started) == sorted([NEW_POINTER, NEW_KEYBOARD])
    # The stale connection was fully released before the fresh one.
    assert stale.unrefed_ei == [1]


def test_ensure_devices_ready_reconnect_failure_raises_tool_error(monkeypatch) -> None:
    """Reconnect whose negotiation never completes → ToolError, clean state.

    Regression for B5(a)/(c) + B10: the real _negotiate_devices runs against
    a fresh connection whose keyboard never resumes → the partial handshake
    is torn down (slots 0, EI context 0, started device stopped) and the
    failure surfaces as ToolError("EIS reconnect failed: ...") instead of a
    bare RuntimeError.
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    # Fast-forward the readiness wait and the 5s negotiation deadline (step
    # below the 0.5s stall-wait budget, issue #235, so the wait loop runs).
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))

    client = _client(stale)
    client._emulating_devices = set()
    client._bus = FakeBus()

    with pytest.raises(ToolError, match="EIS reconnect failed"):
        client._ensure_devices_ready(timeout_s=0.05)

    # Nothing left dangling from the failed (partial) negotiation.
    assert client._pointer == 0
    assert client._keyboard == 0
    assert client._ei == 0
    assert client._emulating_devices == set()
    assert NEW_POINTER in fresh.stopped  # the started device was stopped
    assert set(fresh.unrefed_devices) == {NEW_POINTER, NEW_KEYBOARD}
    assert fresh.unrefed_ei == [101]


def test_disconnect_event_flags_dead_connection_instead_of_raising(monkeypatch) -> None:
    """DISCONNECT during a drain must not raise; the injection rebuilds first.

    Regression for B3: _handle_event used to raise RuntimeError straight out
    of the drain loop, bypassing the reconnect path entirely.

    Contract change (issue #16, dead-wait fix — NOT a test weakening): the
    pre-send readiness wait drains the queued DISCONNECT itself and fails
    the wait instead of reporting ready on the stale emulation set, so the
    SAME injection rebuilds the connection (previously it sent into the dead
    connection and only the post-send flush flagged it dead for the NEXT
    injection). No raise, the key is still delivered exactly once, and the
    connection is healthy afterwards.
    """
    fake = FakeLibei([(_EI_EVENT_DISCONNECT, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.keyboard_key(30, _PRESSED)  # wait drains DISCONNECT → reconnect, then send
    assert setup_calls == [1]
    assert fake.key_calls == [(30, _PRESSED)]
    assert client._connection_dead is False

    client.keyboard_key(31, _PRESSED)  # healthy connection: no further reconnect
    assert setup_calls == [1]
    assert fake.key_calls == [(30, _PRESSED), (31, _PRESSED)]


def test_post_send_disconnect_raises_tool_error_then_next_injection_rebuilds(
    monkeypatch,
) -> None:
    """A DISCONNECT drained by the post-send flush → ToolError, then recovery.

    Contract correction (issue #22 — NOT a test weakening): ``ei_dispatch``
    is void in libei 1.6; there is no negative return to branch on. Death is
    signalled by the DISCONNECT event libei synthesizes, here queued from
    inside the flush's own dispatch (the same window a real dead socket
    occupies). The old ``dispatch_result`` knob modelled an API that does
    not exist.

    Honest delivery semantics (#234 on the real API): the first injection
    raises ToolError from the post-send flush (delivery unconfirmed) and
    delivers on the NEXT injection, which rebuilds the connection.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    with pytest.raises(ToolError, match="input delivery failed"):
        client.keyboard_key(30, _PRESSED)  # flush drains the death DISCONNECT
    assert client._connection_dead is True
    assert fake.key_calls == [(30, _PRESSED)]  # sent, delivery unconfirmed

    client.keyboard_key(31, _PRESSED)  # next injection rebuilds and delivers
    assert setup_calls == [1]
    assert fake.key_calls == [(30, _PRESSED), (31, _PRESSED)]


def test_pointer_button_post_send_disconnect_raises_tool_error(monkeypatch) -> None:
    """pointer_button whose flush drains a DISCONNECT raises ToolError (#234).

    Same honest-delivery contract as keyboard_key: the button event was sent
    into a dying connection, delivery is unconfirmed — the caller must see
    the failure instead of a silent success.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)

    with pytest.raises(ToolError, match="input delivery failed"):
        client.pointer_button(0x110, _PRESSED)
    assert client._connection_dead is True
    assert fake.button_calls == [(0x110, _PRESSED)]


def test_paused_after_send_is_not_tool_error(monkeypatch) -> None:
    """A plain PAUSED is NOT a delivery error — normal event flow (issue #234).

    The delivery ToolError is scoped to a drained DISCONNECT (connection
    death): KWin pauses its EIS devices around input bursts as normal
    operation. Here the pause is drained by the readiness gate, the
    connection is rebuilt, and the injection lands on the fresh devices —
    no ToolError, delivery succeeded.
    """
    fake = FakeLibei([(EI_PAUSED, POINTER)], {})
    _install(monkeypatch, fake)
    # Issue #235: stall wait shortened to 0.5s — the clock step must stay
    # below it for the wait loop to run and drain the queued pause
    # (semantics unchanged, modelled timing constant moved).
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.keyboard_key(30, _PRESSED)  # gate drains PAUSED, reconnects, delivers

    assert setup_calls == [1]  # normal reconnect, not a delivery failure
    assert client._connection_dead is False
    assert fake.key_calls == [(30, _PRESSED)]
    assert client._emulating_devices == {NEW_POINTER, NEW_KEYBOARD}


def test_touch_down_unrefs_touch_on_delivery_failure(monkeypatch) -> None:
    """A touch_down whose flush fails unrefs the touch it never registered.

    The touch enters ``_active_touches`` only AFTER a successful flush (issue
    #234): when the flush raises ToolError the orphaned gesture object must
    not leak — it is unref'd and no ID is handed out.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices.add(TOUCH)

    with pytest.raises(ToolError, match="input delivery failed"):
        client.touch_down(5.0, 5.0)

    assert client._active_touches == {}
    assert len(fake.unrefed_touches) == 1
    assert fake.touch_downs == [(0x900, 5.0, 5.0)]


def test_seat_removed_drops_all_devices(monkeypatch) -> None:
    """SEAT_REMOVED → every device slot dropped and unref'd (B9)."""
    fake = FakeLibei([(_EI_EVENT_SEAT_REMOVED, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._pointer == 0
    assert client._keyboard == 0
    assert client._emulating_devices == set()
    assert sorted(fake.unrefed_devices) == sorted([KEYBOARD, POINTER])

    # The next injection sees no devices at all and takes the reconnect path.
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)
    client._ensure_devices_ready(timeout_s=0.05)
    assert setup_calls == [1]


def test_flush_processes_pause_event(monkeypatch) -> None:
    """A PAUSED event arriving after an injection is drained by _flush."""
    fake = FakeLibei([(EI_PAUSED, POINTER)], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert fake.stopped == [POINTER]
    assert client._emulating_devices == {KEYBOARD}


def test_flush_does_not_steal_slot_from_emulating_device(monkeypatch) -> None:
    """ADDED(B) while A is still emulating → slot keeps A; B must not be unref'd.

    Regression for B4: every DEVICE_ADDED used to replace the slot, so a
    second device of the same capability unref'd a working device mid-flight.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_ADDED, NEW_POINTER)],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._pointer == POINTER
    assert fake.unrefed_devices == []


def test_flush_replaces_paused_device_slot(monkeypatch) -> None:
    """A paused (non-emulating) device may be replaced by a re-advertised one."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_ADDED, NEW_POINTER)],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = {KEYBOARD}  # POINTER currently paused

    client._flush()

    assert client._pointer == NEW_POINTER
    assert fake.unrefed_devices == [POINTER]


def test_flush_fills_slot_after_removal(monkeypatch) -> None:
    """REMOVED(A) then ADDED(B) → slot B (the original remove/re-add burst)."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_REMOVED, POINTER), (_EI_EVENT_DEVICE_ADDED, NEW_POINTER)],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._pointer == NEW_POINTER
    assert fake.unrefed_devices == [POINTER]


def test_flush_processes_removed_device(monkeypatch) -> None:
    """DEVICE_REMOVED drops the reference and the emulation state."""
    fake = FakeLibei([(_EI_EVENT_DEVICE_REMOVED, KEYBOARD)], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._keyboard == 0
    assert fake.unrefed_devices == [KEYBOARD]
    assert client._emulating_devices == {POINTER}


def test_reconnect_releases_active_touches(monkeypatch) -> None:
    """Reconnect finishes active touches and resets IDs; stale IDs are safe.

    Regression for A3: active touch pointers referenced the unref'd EI
    context, so touch_motion/touch_up after a reconnect was a use-after-free.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._active_touches = {5: 0x777}
    client._next_touch_id = 6
    client._eis_iface = FakeIface()
    client._cookie = COOKIE
    client._setup, _setup_calls = _reconnecting_setup(client, emulating=True)

    client._reconnect()

    assert client._active_touches == {}
    assert client._next_touch_id == 0
    assert fake.unrefed_touches == [0x777]

    # A stale touch id is rejected instead of touching the dead pointer.
    with pytest.raises(ValueError, match="No active touch"):
        client.touch_move(5, 1.0, 1.0)

    # Fresh gestures work again from ID 0 on the new connection (the fresh
    # connection carries a touch device — the pointer fallback is gone,
    # issue #24).
    client._touch_device = TOUCH
    client._emulating_devices.add(TOUCH)
    touch_id = client.touch_down(10.0, 20.0)
    assert touch_id == 0
    client.touch_move(touch_id, 11.0, 21.0)
    client.touch_up(touch_id)
    # touch_ups = [stale 0x777 (finished by the reconnect), new gesture touch]
    assert fake.touch_ups == [0x777, fake.touch_downs[0][0]]


def test_reconnect_unrefs_old_state(monkeypatch) -> None:
    """_reconnect tears down cookie, devices and EI context before _setup."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._sequence = 5
    client._eis_iface = FakeIface()
    client._cookie = COOKIE
    client._setup, setup_calls = _reconnecting_setup(client, emulating=False)

    client._reconnect()

    assert client._eis_iface.disconnected == [COOKIE]
    assert fake.unrefed_devices.count(POINTER) == 1
    assert fake.unrefed_devices.count(KEYBOARD) == 1
    assert fake.unrefed_ei == [1]
    assert client._emulating_devices == set()
    assert client._sequence == 0
    assert setup_calls == [1]


def test_keyboard_key_waits_for_resume_before_sending(monkeypatch) -> None:
    """Injection on paused devices drains the queue first, then sends."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()

    client.keyboard_key(30, _PRESSED)

    assert fake.key_calls == [(30, _PRESSED)]
    assert [d for d, _ in fake.started] == sorted([POINTER, KEYBOARD])
    assert client._emulating_devices == {POINTER, KEYBOARD}


def test_keyboard_burst_sends_single_frame(monkeypatch) -> None:
    """All burst keys share one EIS frame (KWin unpause-safe combo shape)."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    client.keyboard_burst([(29, _PRESSED), (45, _PRESSED)])

    assert fake.key_calls == [(29, _PRESSED), (45, _PRESSED)]
    assert fake.frames == [KEYBOARD]  # exactly one frame


def test_keyboard_burst_waits_for_resume_before_sending(monkeypatch) -> None:
    """A burst on paused devices drains the resume queue first."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()

    client.keyboard_burst([(29, _PRESSED), (42, _PRESSED), (45, _PRESSED)])

    assert fake.key_calls == [(29, _PRESSED), (42, _PRESSED), (45, _PRESSED)]
    assert fake.frames == [KEYBOARD]
    assert client._emulating_devices == {POINTER, KEYBOARD}


def test_press_key_combo_uses_burst_pairs(monkeypatch) -> None:
    """A modifier combo is two bursts: press strokes, then release strokes."""
    from kwin_mcp.input import InputBackend

    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = InputBackend.__new__(InputBackend)
    backend._client = client

    # ctrl+x: ctrl(29), x(45) — press batched, pause, release batched.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(input_module.time, "sleep", lambda *_: None)
        backend._press_key_combo("ctrl+x")

    assert fake.key_calls == [
        (29, _PRESSED),
        (45, _PRESSED),
        (45, _RELEASED),
        (29, _RELEASED),
    ]
    assert fake.frames == [KEYBOARD, KEYBOARD]  # two frames: press burst, release burst


def test_client_state_fields_exist(monkeypatch) -> None:
    """The EISClient state contract used across the lifecycle helpers."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    assert client._device_emulating(POINTER) is True
    assert client._device_emulating(0) is False
    assert client._device_emulating(0x999) is False


# ── Issue #229: _setup() must be exception-safe on every path after
#    connectToEIS (cookie disconnect + fd/context cleanup before re-raise) ──


def _fresh_setup_client(
    monkeypatch: Any,
    fake: FakeLibei,
    iface: FakeRemoteDesktopIface | None = None,
) -> EISClient:
    """A client whose REAL ``_setup`` runs against the given fake libei.

    Mirrors the plumbing of ``test_ensure_devices_ready_reconnect_runs_real_
    negotiation``: only the D-Bus layer is faked (FakeBus +
    FakeRemoteDesktopIface via the patched ``dbus.Interface``), while the
    actual ``_setup`` body (``ei_new_sender`` → ``ei_setup_backend_fd`` →
    ``_negotiate_devices``) runs for real.
    """
    resolved_iface = iface if iface is not None else FakeRemoteDesktopIface()
    monkeypatch.setattr(input_module, "_get_libei", lambda: fake)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: resolved_iface)

    client = EISClient.__new__(EISClient)
    client._ei = 0
    client._cookie = 0
    client._pointer = 0
    client._keyboard = 0
    client._touch_device = 0
    client._text_device = 0
    client._next_touch_id = 0
    client._active_touches = {}
    client._sequence = 0
    client._emulating_devices = set()
    client._held_keys = set()
    client._held_buttons = set()
    client._connection_dead = False
    client._eis_iface = None
    client._bus = FakeBus()
    return client


def test_setup_ei_new_sender_failure_disconnects_cookie_and_closes_fd(
    monkeypatch: Any,
) -> None:
    """``ei_new_sender() == 0`` → RuntimeError, cookie disconnected, fd closed.

    Regression for issue #229(a): the fd from connectToEIS and the cookie
    were recorded BEFORE the EI context was created. When ``ei_new_sender``
    returned 0, ``_setup`` raised with the fd still open and the cookie
    still connected — KWin kept the EIS session alive and, in the
    ``__init__`` path, nothing ever cleaned them up (permanent leak).

    libei has not seen the fd yet (the failure happened before
    ``ei_setup_backend_fd``), so the fd is the caller's to close: a real
    pipe descriptor proves it was actually closed (fstat → EBADF).
    """
    read_fd, write_fd = os.pipe()
    try:
        iface = FakeRemoteDesktopIface(fd=read_fd)
        fake = FakeLibei([], {})
        fake.new_sender_result = 0
        client = _fresh_setup_client(monkeypatch, fake, iface=iface)

        with pytest.raises(RuntimeError, match="Failed to create EI context"):
            client._setup()

        # The cookie was disconnected (issue #229: this was the missing call).
        assert iface.disconnected == [42]
        # No EI context leaked into the client state.
        assert client._ei == 0
        # libei never saw the fd (ei_setup_backend_fd was not reached).
        assert fake.setup_fds == []
        # libei never saw the fd → the caller closed it (EBADF proves it).
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)  # already closed when the fix works
        os.close(write_fd)


def test_setup_backend_fd_failure_tears_down_cookie_and_context(
    monkeypatch: Any,
) -> None:
    """``ei_setup_backend_fd != 0`` → RuntimeError, cookie disconnected,
    context unref'd exactly once, ``_ei == 0``, fd CLOSED by the caller.

    Regression for issue #229(b) + the issue #24 fd-ownership correction:
    libei takes the fd's ownership ONLY on success (``ei_setup_backend_fd``
    returns 0 or -errno; on failure the fd is NOT closed by libei), so the
    fd must be closed by the caller's guard when the setup fails — the old
    contract ("libei owns it, Python must not touch it") leaked the fd.
    The pipe descriptor must be gone (fstat → EBADF) when the error
    surfaces.
    """
    read_fd, write_fd = os.pipe()
    try:
        iface = FakeRemoteDesktopIface(fd=read_fd)
        fake = FakeLibei([], {})
        fake.setup_backend_fd_result = -12  # e.g. -ENOMEM
        client = _fresh_setup_client(monkeypatch, fake, iface=iface)

        with pytest.raises(RuntimeError, match="ei_setup_backend_fd failed"):
            client._setup()

        assert iface.disconnected == [42]
        # Exactly one context unref — no double release between the old
        # inline cleanup and the teardown path.
        assert fake.unrefed_ei == [101]
        assert client._ei == 0
        # libei never took ownership (failure) — the caller closed the fd.
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)  # already closed when the fix works
        os.close(write_fd)


def test_setup_negotiation_failure_disconnects_cookie_of_fresh_connection(
    monkeypatch: Any,
) -> None:
    """A handshake that never resumes devices → cookie disconnected too.

    Pins the issue #229 done-condition on the third post-connectToEIS path:
    ``_negotiate_devices`` tears down on its own (B10), but its teardown
    must also release the D-Bus cookie of THIS fresh connection — the
    teardown and the outer guard together must stay idempotent (no double
    disconnect, no double unref).
    """
    fake = FakeLibei([], {})  # no events → handshake times out
    client = _fresh_setup_client(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

    with pytest.raises(RuntimeError, match="No pointer device available from EIS"):
        client._setup()

    assert client._eis_iface is not None
    assert client._eis_iface.disconnected == [42]
    assert client._ei == 0
    assert client._cookie == 0
    # Idempotent double teardown: exactly one unref, no duplicates.
    assert fake.unrefed_ei == [101]


# ── F1: a failed reconnect must leave a NULL EI context unusable ──────────


def test_second_ensure_after_failed_reconnect_is_clean_tool_error(monkeypatch) -> None:
    """After a failed reconnect the NEXT readiness check takes the reconnect
    path again — never ei_get_fd(0)/ei_dispatch(0).

    Regression for F1: a failed reconnect used to leave ``_ei == 0`` with
    ``_connection_dead == False``. The next ``_ensure_devices_ready`` then
    fell through to ``ei_get_fd(0)`` and libei 1.6.0 segfaulted (verified
    empirically: exit 139), killing the whole MCP server. The FakeLibei
    segfault guard (assert ei != 0 in ei_get_fd/ei_dispatch) stands in for
    the real crash: one clean ToolError per call, no NULL-context probe.
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    # Small step so the readiness-wait loop actually RUNS (a step larger than
    # the timeout would skip the loop and with it the NULL-context guard).
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.01))

    client = _client(stale)
    client._emulating_devices = set()
    client._bus = FakeBus()

    with pytest.raises(ToolError, match="EIS reconnect failed"):
        client._ensure_devices_ready(timeout_s=0.05)
    # The failed handshake left the EI context NULL — this is the state that
    # used to segfault on the next injection.
    assert client._ei == 0
    assert client._pointer == 0

    # The next call must be one clean ToolError again (no crash, no infinite
    # retry loop): the NULL context fails the wait instantly and the
    # reconnect runs once more.
    with pytest.raises(ToolError, match="EIS reconnect failed"):
        client._ensure_devices_ready(timeout_s=0.05)
    assert client._ei == 0
    # Exactly one reconnect attempt per ensure call (fresh handshake twice):
    # a reconnect that raises inside _setup aborts the retry loop of issue
    # #235 immediately — retrying a hard-failing handshake adds only
    # latency, the error is already honest and complete.
    assert fresh.setup_fds == [11, 11]


# ── F2: touch gesture calls must fetch the touch pointer AFTER the
#    readiness check, or a reconnect inside it turns the pointer dangling ──


def _touch_client(fake: FakeLibei, touch_id: int = 0, pointer: int = 0x777) -> EISClient:
    """A stalled client holding one active touch gesture on a touch device.

    The touch slot is negotiated (TOUCH = 0x103) per the issue #24 contract:
    touch tools require a touchscreen device — the pointer fallback is gone.
    """
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = set()  # paused without resume → stall
    client._active_touches = {touch_id: pointer}
    client._next_touch_id = touch_id + 1
    return client


def test_touch_move_after_reconnect_rejects_stale_id(monkeypatch) -> None:
    """Stall → touch_move(id) → ValueError, no ei_touch_motion on the dead
    pointer.

    Regression for F2/W2: touch_move fetched the touch pointer BEFORE
    ``_ensure_devices_ready``; the reconnect inside it finished and dropped
    all active touches, so the captured pointer dangled and
    ``ei_touch_motion`` wrote into freed memory (use-after-free).
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _touch_client(fake)
    client._setup, _setup_calls = _reconnecting_setup(client, emulating=True)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

    with pytest.raises(ValueError, match="No active touch"):
        client.touch_move(0, 1.0, 2.0)

    # The reconnect finished the gesture (touch_up + unref in the teardown);
    # the gesture method itself never touched the stale pointer.
    assert fake.touch_motions == []
    assert fake.touch_ups == [0x777]
    assert fake.unrefed_touches == [0x777]
    assert client._active_touches == {}


def test_touch_up_after_reconnect_rejects_stale_id(monkeypatch) -> None:
    """Stall → touch_up(id) → ValueError, no second touch_up/unref of the
    dead pointer.

    Regression for F2/W2: touch_up popped the touch BEFORE
    ``_ensure_devices_ready``, so the reconnect's teardown saw an empty dict
    (no cleanup touch_up/unref) and the method then sent ``ei_touch_up`` into
    freed memory — a use-after-free with a double-unref on top.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _touch_client(fake)
    client._setup, _setup_calls = _reconnecting_setup(client, emulating=True)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

    with pytest.raises(ValueError, match="No active touch"):
        client.touch_up(0)

    # Exactly one finish+release — the reconnect's cleanup, not the gesture
    # method operating on the dangling pointer.
    assert fake.touch_ups == [0x777]
    assert fake.unrefed_touches == [0x777]
    assert client._active_touches == {}


def test_touch_move_with_successful_recovery_uses_live_pointer(monkeypatch) -> None:
    """A gesture ID stays valid when the readiness check recovers without a
    reconnect: the dict entry was never invalidated."""
    fake = FakeLibei(
        [
            (_EI_EVENT_DEVICE_RESUMED, POINTER),
            (_EI_EVENT_DEVICE_RESUMED, KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, TOUCH),
        ],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _touch_client(fake)

    client.touch_move(0, 1.0, 2.0)

    assert fake.touch_motions == [(0x777, 1.0, 2.0)]
    assert client._active_touches == {0: 0x777}  # gesture continues


# ── Issue #24: no pointer fallback for touch tools ─────────────────────────


def test_touch_down_without_touch_device_raises_clean_tool_error(monkeypatch) -> None:
    """Touch on a touch-less connection → clean ToolError, connection alive.

    The old pointer fallback created the touch object on a device without
    touchscreen capability — libei resolves that to a NULL touchscreen and
    the wire error ends in ``ei_disconnect``. Now the injection aborts with
    a ToolError and the connection survives (the next keyboard call works).
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)  # _touch_device == 0, pointer emulating

    with pytest.raises(ToolError, match="No EIS touch device on this connection"):
        client.touch_down(5.0, 5.0)

    # No gesture object was ever created (no device to create it on).
    assert fake.touch_downs == []
    assert client._active_touches == {}
    assert client._connection_dead is False

    # The connection survived: the next keyboard injection still works.
    client.keyboard_key(30, _PRESSED)
    assert fake.key_calls == [(30, _PRESSED)]


def test_touch_move_without_touch_device_raises_clean_tool_error(monkeypatch) -> None:
    """touch_move on a fresh connection without a touch device fails cleanly."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _touch_client(fake)
    client._touch_device = 0
    client._emulating_devices = {POINTER, KEYBOARD}  # gate passes on pointer+kbd

    with pytest.raises(ToolError, match="No EIS touch device on this connection"):
        client.touch_move(0, 1.0, 1.0)

    # The gesture object survives the failed call (untouched bookkeeping).
    assert client._active_touches == {0: 0x777}
    assert client._connection_dead is False


def test_touch_up_without_touch_device_raises_clean_tool_error(monkeypatch) -> None:
    """touch_up without a touch device releases the gesture, fails cleanly."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _touch_client(fake)
    client._touch_device = 0
    client._emulating_devices = {POINTER, KEYBOARD}

    with pytest.raises(ToolError, match="No EIS touch device on this connection"):
        client.touch_up(0)

    # The gesture is finished client-side (popped + unref'd) but no device
    # event was sent into the non-touchscreen device.
    assert client._active_touches == {}
    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x777]
    assert client._connection_dead is False


# ── F3: _reconnect must reuse _teardown_connection (full, resilient) ──────


class _DisconnectRaisesIface:
    """D-Bus EIS interface stub whose disconnect() raises (dead bus)."""

    def disconnect(self, cookie: int) -> None:
        raise dbus.DBusException(f"bus gone (cookie {cookie})")


def test_reconnect_survives_failing_touch_up_and_disconnect(monkeypatch) -> None:
    """_reconnect completes the rebuild even when ei_touch_up and the D-Bus
    disconnect both raise.

    Regression for F3: the hand-rolled cleanup in _reconnect (a less
    defensive duplicate of _teardown_connection) called ei_touch_up without
    suppression, never stopped emulating devices before unref and never
    reset the D-Bus cookie. Any failure there aborted the rebuild halfway;
    _ensure_devices_ready caught the RuntimeError but the client stayed
    dirty and the next injection repeated the same failure.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)

    def failing_touch_up(touch: int) -> None:
        raise RuntimeError("EIS connection dead")

    fake.ei_touch_up = failing_touch_up  # type: ignore[method-assign]
    client = _client(fake)
    client._active_touches = {0: 0x777}
    client._sequence = 3
    client._eis_iface = _DisconnectRaisesIface()
    client._cookie = COOKIE
    client._setup, setup_calls = _reconnecting_setup(client, emulating=False)

    client._reconnect()

    # The rebuild ran to completion despite both failures.
    assert setup_calls == [1]
    # Touches: the failing touch_up was suppressed, the touch still unref'd.
    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x777]
    assert client._active_touches == {}
    assert client._next_touch_id == 0
    # Devices: stopped before release, then unref'd.
    assert sorted(fake.stopped) == sorted([POINTER, KEYBOARD])
    assert sorted(fake.unrefed_devices) == sorted([POINTER, KEYBOARD])
    assert fake.unrefed_ei == [1]
    # The cookie is dropped even though disconnect() raised.
    assert client._cookie == 0


# ── F4: server-driven device invalidation must release active touches ────


def test_pause_on_touch_device_invalidates_active_touches(monkeypatch) -> None:
    """PAUSED(touch device) finishes every active touch and resets the IDs.

    Regression for F4: per the libei API docs, pausing a device resets its
    logical state to neutral — "any touches logically down are released".
    The client kept the gestures in ``_active_touches`` though the server
    had already dropped them.
    """
    fake = FakeLibei([(EI_PAUSED, TOUCH)], {TOUCH: {_EI_CAP_TOUCH}})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888, 1: 0x889}
    client._next_touch_id = 2

    client._flush()

    assert fake.stopped == [TOUCH]
    assert fake.touch_ups == [0x888, 0x889]
    assert fake.unrefed_touches == [0x888, 0x889]
    assert client._active_touches == {}
    assert client._next_touch_id == 0


def test_pause_on_pointer_keeps_active_touches(monkeypatch) -> None:
    """PAUSED(pointer) must not finish touches of the touch device."""
    fake = FakeLibei([(EI_PAUSED, POINTER)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}

    client._flush()

    assert fake.stopped == [POINTER]
    assert client._active_touches == {0: 0x888}
    assert fake.touch_ups == []


def test_removed_touch_device_invalidates_active_touches(monkeypatch) -> None:
    """REMOVED(touch device) releases its logically-down touches, no finish.

    Release-only (issue #18, R3): unlike PAUSED (finish + release) the
    removed device must receive no new ``touch_up`` — only the release of
    the stored gesture objects plus the device ``unref``.
    """
    fake = FakeLibei([(_EI_EVENT_DEVICE_REMOVED, TOUCH)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {3: 0x888}
    client._next_touch_id = 4

    client._flush()

    assert client._touch_device == 0
    assert TOUCH in fake.unrefed_devices
    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x888]
    assert client._active_touches == {}
    assert client._next_touch_id == 0


def test_removed_pointer_device_keeps_touches(monkeypatch) -> None:
    """REMOVED(pointer) leaves the touch device's gestures alone."""
    fake = FakeLibei([(_EI_EVENT_DEVICE_REMOVED, POINTER)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}

    client._flush()

    assert client._pointer == 0
    assert client._active_touches == {0: 0x888}
    assert fake.touch_ups == []


def test_seat_removed_invalidates_active_touches(monkeypatch) -> None:
    """SEAT_REMOVED drops every device AND every logically-down touch."""
    fake = FakeLibei([(_EI_EVENT_SEAT_REMOVED, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}
    client._next_touch_id = 1

    client._flush()

    assert client._pointer == 0
    assert client._touch_device == 0
    assert fake.touch_ups == [0x888]
    assert fake.unrefed_touches == [0x888]
    assert client._active_touches == {}
    assert client._next_touch_id == 0


# ── Review follow-ups: extra-slot gating + burst modifiers ────────────────


def test_wait_emulating_require_text_device(monkeypatch: Any) -> None:
    """Pointer + keyboard ready is NOT enough when the text slot is required.

    Regression for the review finding: text injections gated only on
    pointer + keyboard, so a paused text device silently dropped input.
    """

    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._text_device = 0x104

    assert client._wait_emulating(0.05) is True
    assert client._wait_emulating(0.05, ("_text_device",)) is False


def test_wait_emulating_require_text_device_resumed(monkeypatch: Any) -> None:
    """A RESUMED text event satisfies the extra-slot requirement."""
    from kwin_mcp.input import _EI_CAP_TEXT

    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, 0x104)],
        {0x104: {_EI_CAP_TEXT}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._text_device = 0x104
    client._emulating_devices = {POINTER, KEYBOARD}

    assert client._wait_emulating(0.5, ("_text_device",)) is True
    assert 0x104 in client._emulating_devices


def test_drain_events_shared_helper(monkeypatch: Any) -> None:
    """_drain_events processes pause/resume transitions in one place."""
    fake = FakeLibei(
        [(EI_PAUSED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client._drain_events()

    # Pause then resume: net effect is emulating, stop/start each called once.
    assert POINTER in client._emulating_devices
    assert fake.stopped == [POINTER]
    assert [d for d, _ in fake.started] == [POINTER]


def test_mouse_click_modifiers_use_burst(monkeypatch: Any) -> None:
    """mouse_click batches modifier presses/releases (no per-key frames)."""
    from kwin_mcp.input import InputBackend

    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = InputBackend.__new__(InputBackend)
    backend._client = client

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(input_module.time, "sleep", lambda *_: None)
        backend.mouse_click(10, 20, modifiers=["ctrl", "shift"])

    # Two keyboard frames total: press burst + release burst; the pointer
    # button frames sit in between.
    assert fake.key_calls == [
        (29, _PRESSED),
        (42, _PRESSED),
        (42, _RELEASED),
        (29, _RELEASED),
    ]
    assert fake.frames.count(KEYBOARD) == 2


def test_keyboard_hold_uses_single_burst(monkeypatch: Any) -> None:
    """keyboard_key_down/up batch the whole hold set into one frame each."""
    from kwin_mcp.input import InputBackend

    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = InputBackend.__new__(InputBackend)
    backend._client = client

    backend.keyboard_key_down("ctrl+shift")
    backend.keyboard_key_up("ctrl+shift")

    assert fake.key_calls == [
        (29, _PRESSED),
        (42, _PRESSED),
        (42, _RELEASED),
        (29, _RELEASED),
    ]
    assert fake.frames == [KEYBOARD, KEYBOARD]


# ── Issue #228: require_attrs must survive reconnect (text/touch paths) ────


def _reconnect_text_path_setup(
    monkeypatch: Any,
    fresh: FakeLibei,
) -> tuple[EISClient, FakeLibei]:
    """Common plumbing for the issue #228 regression tests.

    Pointer + keyboard work, the stale text device is paused (so the very
    first text injection stalls and takes the reconnect path), and the real
    ``_setup``/``_negotiate_devices`` runs against the fresh connection's
    fake. The clock is fast-forwarded so the stall wait and negotiation
    deadlines burn no wall time (issue #235: the stall wait is 0.5s now, the
    step stays below it so the wait loop actually runs).
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))

    client = _client(stale)
    client._text_device = TEXT_DEV  # stale text device, currently paused
    client._bus = FakeBus()
    return client, stale


def _reconnect_touch_path_setup(
    monkeypatch: Any,
    fresh: FakeLibei,
) -> tuple[EISClient, FakeLibei]:
    """Same as ``_reconnect_text_path_setup`` for the touch path."""
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))

    client = _client(stale)
    client._touch_device = TOUCH  # stale touch device, currently paused
    client._bus = FakeBus()
    return client, stale


def test_text_utf8_after_reconnect_delivered_when_text_device_resumes_later(
    monkeypatch: Any,
) -> None:
    """Reconnect from the text path waits for the late text device (issue #228).

    The fresh connection resumes pointer + keyboard first; the text device is
    advertised + resumed only afterwards (late events). The injection must be
    delivered through the fresh text device — the old code sent it straight
    into the NULL slot the reconnect left behind.
    """
    fresh = LateEventFakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
        late_events=[
            (_EI_EVENT_DEVICE_ADDED, NEW_TEXT_DEV),
            (_EI_EVENT_DEVICE_RESUMED, NEW_TEXT_DEV),
        ],
        late_caps={NEW_TEXT_DEV: {_EI_CAP_TEXT}},
        late_after_drains=1,
    )
    client, stale = _reconnect_text_path_setup(monkeypatch, fresh)

    client.text_utf8("hi")

    assert client._text_device == NEW_TEXT_DEV
    assert NEW_TEXT_DEV in client._emulating_devices
    assert fresh.text_utf8_calls == [(NEW_TEXT_DEV, b"hi")]
    assert NEW_TEXT_DEV in fresh.frames
    # The stale connection (including the paused text device) was released.
    assert stale.unrefed_ei == [1]
    assert TEXT_DEV in stale.unrefed_devices


def test_text_keysym_after_reconnect_delivered_when_text_device_resumes_later(
    monkeypatch: Any,
) -> None:
    """Same guarantee as text_utf8, for the text_keysym path (issue #228)."""
    fresh = LateEventFakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
        late_events=[
            (_EI_EVENT_DEVICE_ADDED, NEW_TEXT_DEV),
            (_EI_EVENT_DEVICE_RESUMED, NEW_TEXT_DEV),
        ],
        late_caps={NEW_TEXT_DEV: {_EI_CAP_TEXT}},
        late_after_drains=1,
    )
    client, _ = _reconnect_text_path_setup(monkeypatch, fresh)

    client.text_keysym(0x74, _PRESSED)  # XK_t press

    assert client._text_device == NEW_TEXT_DEV
    assert fresh.text_keysym_calls == [(NEW_TEXT_DEV, 0x74, _PRESSED)]
    assert NEW_TEXT_DEV in fresh.frames


def test_text_utf8_after_reconnect_delivered_when_text_device_advertised_together(
    monkeypatch: Any,
) -> None:
    """Text device advertised + resumed WITH pointer + keyboard on the fresh
    connection: the handshake drain registers it in the same pass and the
    injection is delivered (pins the contract the late-resume tests fix)."""
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_ADDED, NEW_TEXT_DEV),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_TEXT_DEV),
        ],
        {
            NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE},
            NEW_KEYBOARD: {_EI_CAP_KEYBOARD},
            NEW_TEXT_DEV: {_EI_CAP_TEXT},
        },
    )
    client, _ = _reconnect_text_path_setup(monkeypatch, fresh)

    client.text_utf8("hi")

    assert client._text_device == NEW_TEXT_DEV
    assert fresh.text_utf8_calls == [(NEW_TEXT_DEV, b"hi")]


def test_text_utf8_after_reconnect_tool_error_when_text_device_not_negotiated(
    monkeypatch: Any,
) -> None:
    """Reconnect that comes back WITHOUT a text device → ToolError (issue #228).

    The stale text device was paused, the injection reconnected, but the fresh
    connection never advertises a text device. The old code sent the UTF-8
    text into the NULL device slot (silent drop); the fixed code fails with a
    clean ToolError instead.
    """
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    client, _ = _reconnect_text_path_setup(monkeypatch, fresh)

    with pytest.raises(ToolError, match="text device"):
        client.text_utf8("hi")

    assert fresh.text_utf8_calls == []


def test_touch_down_after_reconnect_delivered_when_touch_device_resumes_later(
    monkeypatch: Any,
) -> None:
    """Touch path: reconnect waits for the late touch device (issue #228).

    The pointer fallback (``_touch_device or _pointer``) stays reserved for
    servers without a touch device — a late-resuming touch device on the
    fresh connection must be waited for, not bypassed.
    """
    fresh = LateEventFakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
        late_events=[
            (_EI_EVENT_DEVICE_ADDED, NEW_TOUCH_DEV),
            (_EI_EVENT_DEVICE_RESUMED, NEW_TOUCH_DEV),
        ],
        late_caps={NEW_TOUCH_DEV: {_EI_CAP_TOUCH}},
        late_after_drains=1,
    )
    client, _ = _reconnect_touch_path_setup(monkeypatch, fresh)

    touch_id = client.touch_down(5.0, 5.0)

    assert touch_id == 0
    assert client._touch_device == NEW_TOUCH_DEV
    assert NEW_TOUCH_DEV in client._emulating_devices
    assert fresh.touch_downs == [(0x900, 5.0, 5.0)]
    assert fresh.frames[-1] == NEW_TOUCH_DEV


# ── Issue #233: held keys/buttons must survive recovery (PAUSED→RESUMED and
#    reconnect) — one replay re-press frame after start_emulating ────────────


def _held_backend(client: EISClient) -> Any:
    """An InputBackend wrapping the given client (stateful-input tests)."""
    from kwin_mcp.input import InputBackend

    backend = InputBackend.__new__(InputBackend)
    backend._client = client
    return backend


def test_held_key_replayed_after_resumed(monkeypatch) -> None:
    """(a) keyboard_key_down("shift") → PAUSED → RESUMED → one re-press frame.

    The server reset its logical state to neutral on pause ("any keys
    logically down are released"); after the device resumes, the client must
    restore its own held state with a single press frame — before the next
    user injection would observe the missing modifier.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = _held_backend(client)

    backend.keyboard_key_down("shift")
    assert fake.key_calls == [(42, _PRESSED)]
    assert client._held_keys == {42}
    assert fake.frames == [KEYBOARD]

    # Recovery: KWin paused the device (client-side stall model — no explicit
    # PAUSED events queued) and later resumed it.
    client._emulating_devices.clear()
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, POINTER)])
    fake.key_calls.clear()
    fake.frames.clear()
    fake.started.clear()

    client._ensure_devices_ready(timeout_s=0.5)

    # Exactly one re-press frame right after the resume; sequence stays
    # monotonic across the replay.
    assert fake.started == [(KEYBOARD, 1), (POINTER, 2)]
    assert fake.key_calls == [(42, _PRESSED)]
    assert fake.frames == [KEYBOARD]
    assert client._held_keys == {42}

    # Until the next user injection nothing further is sent.
    fake.key_calls.clear()
    fake.frames.clear()
    client._drain_events()
    assert fake.key_calls == []
    assert fake.frames == []


def test_held_key_replayed_on_fresh_connection_after_disconnect(monkeypatch) -> None:
    """(b) keyboard_key_down("ctrl") → DISCONNECT → reconnect → re-press on
    the new connection right after the fresh handshake's start_emulating."""
    stale = FakeLibei([], {})
    fresh = FakeLibei(
        [
            (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
            (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
            (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
            (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
        ],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())

    client = _client(stale)
    client._bus = FakeBus()  # ty: ignore[invalid-assignment]
    backend = _held_backend(client)

    backend.keyboard_key_down("ctrl")
    assert stale.key_calls == [(29, _PRESSED)]
    assert client._held_keys == {29}

    # The DISCONNECT event is drained by the flush of the next injection.
    # Contract correction (issue #22): ei_dispatch is void and death is the
    # drained DISCONNECT, so the flush that discovers it raises
    # "delivery unconfirmed" instead of silently flagging (honest delivery).
    stale.queue_events([(_EI_EVENT_DISCONNECT, 0)])
    with pytest.raises(ToolError, match="input delivery failed"):
        client._flush()
    assert client._connection_dead is True

    client._ensure_devices_ready(timeout_s=0.05)

    # The replay went to the FRESH connection's keyboard in the same drain
    # that processed the handshake's RESUMED events (the frame therefore
    # lands after start_emulating, before any user injection).
    assert client._keyboard == NEW_KEYBOARD
    assert fresh.started == [(NEW_POINTER, 1), (NEW_KEYBOARD, 2)]
    assert fresh.key_calls == [(29, _PRESSED)]
    assert fresh.frames == [NEW_KEYBOARD]
    assert client._held_keys == {29}

    # The stale connection saw only the original press.
    assert stale.frames == [KEYBOARD]
    assert stale.key_calls == [(29, _PRESSED)]


def test_held_button_replayed_after_resumed(monkeypatch) -> None:
    """(c) mouse_button_down press → PAUSED → RESUMED → one re-press frame."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = _held_backend(client)

    backend.mouse_button_down(10, 20)
    assert fake.button_calls == [(0x110, _PRESSED)]
    assert client._held_buttons == {0x110}
    assert fake.frames == [POINTER, POINTER]  # mouse_move frame + press frame

    client._emulating_devices.clear()
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)])
    fake.button_calls.clear()
    fake.frames.clear()
    fake.started.clear()

    client._ensure_devices_ready(timeout_s=0.5)

    assert fake.started == [(POINTER, 1), (KEYBOARD, 2)]
    assert fake.button_calls == [(0x110, _PRESSED)]
    assert fake.frames == [POINTER]
    assert client._held_buttons == {0x110}


def test_held_state_cleared_by_paired_release_not_replayed(monkeypatch) -> None:
    """(d) press+release pair → PAUSED → RESUMED → NO re-press (nothing held).

    Ordinary paired operations (combos, click modifiers, legacy typing) must
    not turn into perpetual held state: only presses without an in-call
    release populate the held set.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = _held_backend(client)

    backend.keyboard_key_down("shift")
    backend.keyboard_key_up("shift")
    assert client._held_keys == set()  # the pair released: nothing to restore
    fake.key_calls.clear()
    fake.frames.clear()

    client._emulating_devices.clear()
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, POINTER)])
    fake.started.clear()

    client._ensure_devices_ready(timeout_s=0.5)

    # Devices started emulating, but no replay frames were sent.
    assert fake.started == [(KEYBOARD, 1), (POINTER, 2)]
    assert fake.key_calls == []
    assert fake.frames == []
    assert client._held_keys == set()


def test_held_state_replayed_once_per_recovery(monkeypatch) -> None:
    """(e) RESUMED twice without an intervening PAUSED → the press is sent once.

    Duplicate RESUMED events (KWin re-resumes without an explicit pause) must
    not re-send the replay frame: the held state is restored once per
    actual recovery, and the idempotency of ``_resume_device`` covers the
    replay as well.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = _held_backend(client)

    backend.keyboard_key_down("shift")
    assert fake.key_calls == [(42, _PRESSED)]

    # Recovery: pause → resume (the press is replayed once).
    client._emulating_devices.clear()
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, POINTER)])
    fake.started.clear()
    fake.key_calls.clear()
    fake.frames.clear()

    client._ensure_devices_ready(timeout_s=0.5)
    assert fake.key_calls == [(42, _PRESSED)]
    assert fake.frames == [KEYBOARD]

    # A duplicate RESUMED on the still-emulating devices: no second replay.
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, POINTER)])
    client._drain_events()
    assert fake.key_calls == [(42, _PRESSED)]
    assert fake.frames == [KEYBOARD]
    assert client._held_keys == {42}


def test_held_key_release_after_recovery_does_not_double_release(monkeypatch) -> None:
    """A key replayed on resume is released normally by keyboard_key_up."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    backend = _held_backend(client)

    backend.keyboard_key_down("shift")
    client._emulating_devices.clear()
    fake.queue_events([(_EI_EVENT_DEVICE_RESUMED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, POINTER)])

    client._ensure_devices_ready(timeout_s=0.5)
    assert fake.key_calls == [(42, _PRESSED), (42, _PRESSED)]  # original + replay

    backend.keyboard_key_up("shift")
    assert fake.key_calls[-1] == (42, _RELEASED)
    assert client._held_keys == set()


# ── Issue #235: bounded reconnect retry loop for KWin's pause cycle ────────
#
# Live pattern (ei-debug log): KWin pauses its EIS devices right after every
# handshake — ADDED+RESUMED x3 then PAUSED x2, the fresh connection lives less
# than a second, and the cycle repeats. A single reconnect + one re-check
# window (the #228 shape) cannot win against that: the gate now retries the
# reconnect a bounded number of times, each attempt getting one short
# re-check, and fails loudly with the attempt count when the budget is
# exhausted. The pre-reconnect stall wait is shortened accordingly (0.5s,
# was 5s): a PAUSED stall without a RESUMED leads into a reconnect anyway,
# so waiting longer only burns latency (wingman #231).


def _pause_cycle_setup(
    monkeypatch: Any,
    fresh_count: int,
    pause_cycles: int,
) -> tuple[EISClient, FakeLibei, list[FakeLibei]]:
    """A stalled client + fresh fakes modelling KWin's pause cycle (#235).

    Returns ``(client, stale, fresh)``: the client starts on the stale
    connection with paused devices and no resume queued; reconnect attempt N
    is handed fresh fake N (SwitchingLibei, contexts 101, 102, ...). The
    first ``pause_cycles`` fresh connections re-pause pointer + keyboard
    right after their handshake completes (LateEventFakeLibei with
    ``late_after_drains=1``: the pause lands in the queue after the
    handshake's last drain, so the negotiation itself succeeds and the
    post-reconnect wait observes the pause — exactly the live pattern); the
    remaining fakes keep their devices emulating.
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    handshake = [
        (_EI_EVENT_DEVICE_ADDED, NEW_POINTER),
        (_EI_EVENT_DEVICE_ADDED, NEW_KEYBOARD),
        (_EI_EVENT_DEVICE_RESUMED, NEW_POINTER),
        (_EI_EVENT_DEVICE_RESUMED, NEW_KEYBOARD),
    ]
    caps = {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}, NEW_KEYBOARD: {_EI_CAP_KEYBOARD}}
    fresh: list[FakeLibei] = []
    for index in range(fresh_count):
        if index < pause_cycles:
            fresh.append(
                LateEventFakeLibei(
                    list(handshake),
                    dict(caps),
                    late_events=[
                        (EI_PAUSED, NEW_POINTER),
                        (EI_PAUSED, NEW_KEYBOARD),
                    ],
                    late_caps={},
                    late_after_drains=1,
                )
            )
        else:
            fresh.append(FakeLibei(list(handshake), dict(caps)))
    router = SwitchingLibei([stale, *fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    # Small step so the (shortened) stall and post-reconnect wait loops
    # actually run and observe the queued pauses.
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.05))

    client = _client(stale)
    client._emulating_devices = set()  # paused without resume → stall
    client._bus = FakeBus()  # ty: ignore[invalid-assignment]
    return client, stale, fresh


def test_pause_cycle_delivers_after_bounded_reconnect_attempts(monkeypatch: Any) -> None:
    """(a) Pause cycle → bounded reconnect attempts → delivery on the 3rd.

    Attempts 1-2 land on fresh connections that re-pause their devices right
    after the handshake; the 3rd fresh connection keeps them emulating, the
    post-reconnect re-check succeeds and the injection is delivered — no
    ToolError, exactly three reconnects.
    """
    client, stale, fresh = _pause_cycle_setup(monkeypatch, fresh_count=3, pause_cycles=2)

    client.keyboard_key(30, _PRESSED)

    # Exactly three rebuilds: the stale connection and both pause-cycled
    # fresh ones were released; the third fresh connection is still alive.
    assert stale.unrefed_ei == [1]
    assert fresh[0].unrefed_ei == [101]
    assert fresh[1].unrefed_ei == [102]
    assert fresh[2].unrefed_ei == []
    # The injection landed on the third (still-emulating) connection.
    assert client._keyboard == NEW_KEYBOARD
    assert fresh[2].key_calls == [(30, _PRESSED)]
    assert fresh[2].frames == [NEW_KEYBOARD]


def test_pause_cycle_exhausts_attempts_into_clean_tool_error(monkeypatch: Any) -> None:
    """(b) Every attempt re-paused → ToolError with the attempt budget.

    After the bounded budget the injection fails loudly (the attempt count
    is in the message) and the connection is torn down, so the next call
    starts its reconnect from a clean slate — and nothing was ever sent
    into a paused device.
    """
    client, stale, fresh = _pause_cycle_setup(monkeypatch, fresh_count=3, pause_cycles=3)

    with pytest.raises(ToolError, match="after 3 reconnect attempts"):
        client.keyboard_key(30, _PRESSED)

    # All three fresh connections were built and released again; the stale
    # one went first.
    assert stale.unrefed_ei == [1]
    assert [f.unrefed_ei for f in fresh] == [[101], [102], [103]]
    # Clean slate: the next call starts its reconnect from scratch (F1).
    assert client._ei == 0
    assert client._pointer == 0
    assert client._keyboard == 0
    assert client._emulating_devices == set()
    # No injection was sent into a paused device.
    assert all(f.key_calls == [] for f in fresh)
    # Attempt 2 of #235: with no held state the reset-on-exhaustion path is
    # a no-op — the ordinary retry path's contract is unchanged.
    assert client._held_keys == set()
    assert client._held_buttons == set()


def test_ensure_devices_ready_first_stall_wait_is_half_second(monkeypatch: Any) -> None:
    """(c) The pre-reconnect stall wait is 0.5s, not the old 5s (#231/#235).

    In the pause cycle the devices never resume on their own, so a long
    stall wait only burns latency before the reconnect that is needed
    anyway. FakeClock advances 0.1s per monotonic() call: the reconnect must
    fire within ~1.5s of fake time — the old 5s deadline landed at ~5.5s.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.1))
    client = _client(fake)
    client._emulating_devices = set()  # paused, no resume queued → stall

    reconnect_at: list[float] = []

    def fake_setup() -> None:
        reconnect_at.append(input_module.time.monotonic())
        client._ei = 101
        client._pointer = NEW_POINTER
        client._keyboard = NEW_KEYBOARD
        client._emulating_devices = {NEW_POINTER, NEW_KEYBOARD}

    client._setup = fake_setup  # ty: ignore[invalid-assignment]

    client._ensure_devices_ready()

    assert len(reconnect_at) == 1
    assert reconnect_at[0] <= 1.5  # old deadline: ~5.5 fake seconds
    # The policy constant itself (the behavioral assert above is primary).
    assert input_module._STALL_READY_TIMEOUT_S == 0.5


def test_ensure_devices_ready_happy_path_no_reconnect(monkeypatch: Any) -> None:
    """(d) Emulating devices → zero reconnects, immediate delivery.

    The bounded retry loop must never touch a healthy connection: the first
    readiness probe short-circuits before any reconnect logic runs.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)  # pointer + keyboard already emulating
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.keyboard_key(30, _PRESSED)

    assert setup_calls == []
    assert fake.key_calls == [(30, _PRESSED)]
    assert fake.frames == [KEYBOARD]
    assert client._pointer == POINTER  # same, untouched connection


# ── Issue #235 attempt 2: held-state reset on reconnect budget exhaustion ──
#
# Live evidence (probe10, ei-debug10.log, 13 reconnects): with shift held
# (keyboard_key_down delivered), EVERY fresh handshake's RESUMED replays the
# held modifier press (_replay_held_state, #233) — and this KWin build pauses
# its devices again in response to the replayed modifier press. Each retry
# attempt therefore re-enters the pause cycle through the replay itself:
# attempt 1 → handshake → replayed press → PAUSED → attempt 2 → ... → the
# budget exhausts into a ToolError, and the NEXT call repeats the whole
# pattern forever (the held set still holds the key that was never released).
#
# The libei API contract breaks the loop: after PAUSED the server has already
# reset its logical state to neutral ("any buttons or keys logically down are
# released"). When the reconnect budget is exhausted the client's held sets
# are therefore guaranteed-stale — clearing them aligns the client with the
# server. The next call's fresh handshake then has nothing to replay, the
# devices stay emulating, and delivery works again. This is a reconciliation,
# not a silent loss: the ToolError explicitly reports the reset.


def test_pause_cycle_with_held_key_resets_held_state_on_exhaustion(
    monkeypatch: Any,
) -> None:
    """(a) Held shift + full pause cycle → ToolError, held sets cleared.

    Every reconnect attempt replays the held modifier press (the #233 replay
    is correct per attempt) and the server re-pauses. When the budget
    exhausts, the client must reconcile with the server's neutral state:
    ``_held_keys``/``_held_buttons`` are cleared, the connection is torn down
    (``_ei == 0``), and the ToolError names the reset so the calling agent
    knows to re-press the modifier.
    """
    client, _stale, fresh = _pause_cycle_setup(monkeypatch, fresh_count=3, pause_cycles=3)
    client._held_keys.add(42)  # shift, as delivered by keyboard_key_down
    client._held_buttons.add(0x110)

    with pytest.raises(ToolError, match="after 3 reconnect attempts") as exc_info:
        client.keyboard_key(30, _PRESSED)
    message = str(exc_info.value).lower()
    assert "held" in message
    assert "reset" in message

    # Reconciliation: the server is in neutral after the pauses (libei PAUSED
    # releases all logically-down keys/buttons), so the client's held sets
    # must be empty — a phantom replay would re-enter the pause loop forever.
    assert client._held_keys == set()
    assert client._held_buttons == set()
    # Clean slate for the next call (F1): no context left to probe.
    assert client._ei == 0
    # The #233 replay still ran per attempt BEFORE the reset (not weakened):
    # each of the three fresh handshakes replayed the held press.
    replayed = [f.key_calls for f in fresh]
    assert replayed == [[(42, _PRESSED)], [(42, _PRESSED)], [(42, _PRESSED)]]


def test_call_after_exhaustion_delivers_without_replay_or_new_pauses(
    monkeypatch: Any,
) -> None:
    """(b) The loop is broken: the call after the ToolError recovers cleanly.

    Continuing (a): the 4th fresh connection keeps its devices emulating
    after the handshake (no re-pause queued — the live shape once nothing
    replays a modifier press). The next readiness gate reconnects exactly
    once and succeeds, and — critically — the fresh handshake carries NO
    replayed press (the held sets were cleared): the gate itself injects
    nothing, so the fresh fake logs zero key calls.
    """
    client, _stale, fresh = _pause_cycle_setup(monkeypatch, fresh_count=4, pause_cycles=3)
    client._held_keys.add(42)

    with pytest.raises(ToolError, match="after 3 reconnect attempts"):
        client.keyboard_key(30, _PRESSED)
    assert client._held_keys == set()
    assert client._held_buttons == set()
    assert client._ei == 0

    client._ensure_devices_ready(timeout_s=0.05)

    # First attempt of the next call succeeded: one more fresh handshake
    # (the 4th, context 104) whose devices stayed emulating — no new pause.
    assert client._ei == 104
    assert client._pointer == NEW_POINTER
    assert client._keyboard == NEW_KEYBOARD
    assert client._emulating_devices == {NEW_POINTER, NEW_KEYBOARD}
    # No replayed press: the fresh handshake's key log holds only user
    # injections — and the gate injects nothing, so zero key calls.
    assert fresh[3].key_calls == []
    # The fresh connection stayed alive (no teardown after success).
    assert fresh[3].unrefed_ei == []


def test_exhaustion_without_held_state_reports_no_reset(monkeypatch: Any) -> None:
    """(c) Empty held sets → exhaustion keeps the original message shape.

    The held-reset clause only appears when there was held state to clear;
    the plain retry path (#235 attempt 1) keeps its exact contract.
    """
    client, _stale, _fresh = _pause_cycle_setup(monkeypatch, fresh_count=3, pause_cycles=3)

    with pytest.raises(ToolError, match="after 3 reconnect attempts") as exc_info:
        client.keyboard_key(30, _PRESSED)
    assert "held" not in str(exc_info.value)

    assert client._held_keys == set()
    assert client._held_buttons == set()
    assert client._ei == 0


def test_success_on_retry_attempt_with_held_state_keeps_it(monkeypatch: Any) -> None:
    """(d) Delivery on the k-th attempt with held state → held sets survive.

    The #233 replay contract is not weakened: between attempts the replay
    must keep working, and a successful recovery must NOT clear held state —
    the reset is reserved for budget exhaustion only.
    """
    client, _stale, fresh = _pause_cycle_setup(monkeypatch, fresh_count=3, pause_cycles=2)
    client._held_keys.add(42)
    client._held_buttons.add(0x110)

    client.keyboard_key(30, _PRESSED)  # 3rd connection stays emulating → delivered

    # Held state survives the successful recovery (replay restored the
    # modifier server-side; the pairing keyboard_key_up is still pending).
    assert client._held_keys == {42}
    assert client._held_buttons == {0x110}
    # The replay ran on the successful connection too (before the injection).
    assert fresh[2].key_calls == [(42, _PRESSED), (30, _PRESSED)]
    # The exhausted-connection teardown did not happen (still alive).
    assert fresh[2].unrefed_ei == []


# ── Issue #19: held-state intent race (set mutation after _flush) ──────────
#
# hold_keys/hold_button recorded the held intent only AFTER the post-send
# _flush returned, so a PAUSED→RESUMED drained inside the same call replayed
# the stale set: a hold missed its replay (client held, server neutral —
# silent divergence) while a release re-pressed the just-released key (wire
# UP then DOWN — sticky). The four methods now record/drop the intent BEFORE
# sending and roll it back when the send fails with a ToolError. The probes
# below inject the server events from inside ei_dispatch (a real KWin pushes
# them mid-flush); pre-queued events would be consumed by the pre-send
# readiness drain (F8) and miss the window.


def test_hold_keys_replays_inside_same_call_post_send_drain(monkeypatch) -> None:
    """Hold + PAUSED→RESUMED inside the same _flush → replay DOWN on the wire.

    Regression probe for issue #19 (hold leg): with the intent recorded
    before the send, the resume replayed inside the call's own post-send
    drain re-presses the just-sent key — wire holds DOWN + replay DOWN and
    the client set agrees with the server instead of diverging from it.
    """
    fake = DispatchInjectingLibei(
        [],
        {},
        inject=[(EI_PAUSED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client.hold_keys([42])

    assert fake.key_calls == [(42, _PRESSED), (42, _PRESSED)]
    assert fake.frames == [KEYBOARD, KEYBOARD]
    assert client._held_keys == {42}


def test_release_keys_not_replayed_inside_same_call_post_send_drain(monkeypatch) -> None:
    """Release + PAUSED→RESUMED inside the same _flush → wire ends with UP.

    Regression probe for issue #19 (release leg): with the intent dropped
    before the send, the resume replayed inside the call's own post-send
    drain finds nothing to re-press — no trailing DOWN after the UP (sticky
    modifier) and the client set is empty.
    """
    fake = DispatchInjectingLibei(
        [],
        {},
        inject=[(EI_PAUSED, KEYBOARD), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._held_keys = {42}

    client.release_keys([42])

    assert fake.key_calls == [(42, _RELEASED)]
    assert client._held_keys == set()


def test_hold_button_replays_inside_same_call_post_send_drain(monkeypatch) -> None:
    """Button hold leg of issue #19: same-call replay re-presses the button."""
    fake = DispatchInjectingLibei(
        [],
        {},
        inject=[(EI_PAUSED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client.hold_button(0x110)

    assert fake.button_calls == [(0x110, _PRESSED), (0x110, _PRESSED)]
    assert client._held_buttons == {0x110}


def test_release_button_not_replayed_inside_same_call_post_send_drain(monkeypatch) -> None:
    """Button release leg of issue #19: wire ends with UP, no trailing DOWN."""
    fake = DispatchInjectingLibei(
        [],
        {},
        inject=[(EI_PAUSED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._held_buttons = {0x110}

    client.release_button(0x110)

    assert fake.button_calls == [(0x110, _RELEASED)]
    assert client._held_buttons == set()


def test_hold_keys_rolls_back_on_post_send_disconnect(monkeypatch) -> None:
    """A post-send DISCONNECT drained by _flush (#234) rolls the hold back.

    The DOWN was queued into libei but delivery is unconfirmed (ToolError);
    the client must not claim the key held.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)

    with pytest.raises(ToolError, match="input delivery failed"):
        client.hold_keys([42])

    assert fake.key_calls == [(42, _PRESSED)]
    assert client._held_keys == set()
    assert client._connection_dead is True


def test_release_keys_restores_on_post_send_disconnect(monkeypatch) -> None:
    """A post-send DISCONNECT drained by _flush (#234) restores the release.

    The UP never confirmedly landed, so the key stays held client-side
    instead of silently flipping to released.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)
    client._held_keys = {42}

    with pytest.raises(ToolError, match="input delivery failed"):
        client.release_keys([42])

    assert fake.key_calls == [(42, _RELEASED)]
    assert client._held_keys == {42}
    assert client._connection_dead is True


def test_hold_button_rolls_back_on_post_send_disconnect(monkeypatch) -> None:
    """Button hold leg of the post-send DISCONNECT ToolError rollback."""
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)

    with pytest.raises(ToolError, match="input delivery failed"):
        client.hold_button(0x110)

    assert fake.button_calls == [(0x110, _PRESSED)]
    assert client._held_buttons == set()


def test_hold_button_failure_keeps_pre_existing_hold(monkeypatch) -> None:
    """A failed re-hold of an already-held button keeps the original intent.

    The rollback discards only what the call added: re-pressing a button
    that was already held (e.g. duplicate ``mouse_button_down``) and failing
    the send must not silently un-hold it — the server may still hold the
    earlier press.
    """
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)
    client._held_buttons = {0x110}

    with pytest.raises(ToolError, match="input delivery failed"):
        client.hold_button(0x110)

    assert client._held_buttons == {0x110}


def test_release_button_restores_on_post_send_disconnect(monkeypatch) -> None:
    """Button release leg of the post-send DISCONNECT ToolError rollback."""
    fake = DispatchInjectingLibei([], {}, inject=[(_EI_EVENT_DISCONNECT, 0)])
    _install(monkeypatch, fake)
    client = _client(fake)
    client._held_buttons = {0x110}

    with pytest.raises(ToolError, match="input delivery failed"):
        client.release_button(0x110)

    assert fake.button_calls == [(0x110, _RELEASED)]
    assert client._held_buttons == {0x110}


class DispatchAtLibei(FakeLibei):
    """FakeLibei queueing server events on the Nth ``ei_dispatch`` call.

    Unlike ``DispatchInjectingLibei`` (fires on the FIRST dispatch), this
    targets a specific flush — e.g. the drag's release frame — so the
    recovery window can be placed mid-operation at a known send.
    """

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
        inject_at: int,
        inject: list[tuple[int, int]],
    ) -> None:
        super().__init__(events, device_caps)
        self._inject_at = inject_at
        self._inject = list(inject)
        self._dispatches = 0

    def ei_dispatch(self, ei: int) -> None:
        assert ei != 0, "ei_dispatch called with NULL EI context (segfault guard)"
        if self._dispatches == self._inject_at:
            self._events.extend(self._inject)
        self._dispatches += 1


def test_mouse_drag_release_not_replayed_when_recovery_hits_release_flush(
    monkeypatch,
) -> None:
    """Sticky drag button (issue #24): wire ends UP, no DOWN after the last UP.

    ``mouse_drag`` used to drop the transient button hold in its ``finally``
    — AFTER the release frame was sent — so a PAUSED→RESUMED drained by the
    release flush's own drain replayed the still-registered button DOWN
    after the UP: wire [(DOWN, UP, DOWN)], the server stuck with a pressed
    button while the client considered the operation done. The transient
    button intent is now dropped BEFORE the release frame (issue #19
    release ordering, as release_keys/release_button already do), so the
    replay has nothing to re-press: wire ends (DOWN, UP) with no unpaired
    DOWN after the last UP.
    """
    fake = DispatchAtLibei(
        [],
        {},
        # mouse_drag(10,20 → 30,20) dispatches: move(0), DOWN(1), 10 motions
        # (2-11), RELEASE(12) — inject into the release flush.
        inject_at=12,
        inject=[(EI_PAUSED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    from kwin_mcp.input import InputBackend

    backend = InputBackend.__new__(InputBackend)
    backend._client = client

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(input_module.time, "sleep", lambda *_: None)
        backend.mouse_drag(10, 20, 30, 20)

    # Wire: exactly one DOWN then one UP — no replay press after the UP.
    assert fake.button_calls == [(0x110, _PRESSED), (0x110, _RELEASED)]
    assert client._held_buttons == set()


def test_hold_keys_rolls_back_on_reconnect_failure(monkeypatch: Any) -> None:
    """A reconnect-path ToolError rolls the hold intent back too.

    Stalled devices + a hard-failing rebuild: the DOWN was never queued, so
    the pre-send intent must vanish instead of lingering as phantom held
    state for the next handshake's replay.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))
    client = _client(fake)
    client._emulating_devices = set()

    def boom_setup() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_setup", boom_setup)

    with pytest.raises(ToolError, match="EIS reconnect failed"):
        client.hold_keys([42])

    assert client._held_keys == set()


def test_release_keys_restores_on_reconnect_failure(monkeypatch: Any) -> None:
    """A reconnect-path ToolError restores the release intent too.

    The UP was never queued (the gate failed before any send), so the key
    stays held client-side and the pairing release can be retried.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))
    client = _client(fake)
    client._emulating_devices = set()
    client._held_keys = {42}

    def boom_setup() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_setup", boom_setup)

    with pytest.raises(ToolError, match="EIS reconnect failed"):
        client.release_keys([42])

    assert client._held_keys == {42}
