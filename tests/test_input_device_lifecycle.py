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
    _EI_EVENT_DEVICE_PAUSED,
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


class FakeLibei:
    """Minimal libei stub covering device lifecycle calls and injection."""

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
    ) -> None:
        self._events = list(events)
        self._event_meta: dict[int, tuple[int, int]] = {}
        self._next_id = 0
        self.device_caps = device_caps
        self.dispatch_result = 0  # ei_dispatch return value (negative = failure)
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
        self.touch_downs: list[tuple[int, float, float]] = []
        self.touch_motions: list[tuple[int, float, float]] = []
        self.touch_ups: list[int] = []

    def ei_get_fd(self, ei: int) -> int:
        # Guard for F1: after a failed reconnect the client must never probe
        # libei with a NULL (0) EI context — the real libei 1.6.0 segfaults
        # (exit 139) on ei_get_fd(NULL)/ei_dispatch(NULL), killing the whole
        # MCP server.
        assert ei != 0, "ei_get_fd called with NULL EI context (segfault guard)"
        return 9

    def ei_dispatch(self, ei: int) -> int:
        assert ei != 0, "ei_dispatch called with NULL EI context (segfault guard)"
        return self.dispatch_result

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

    def ei_setup_backend_fd(self, ei: int, fd: int) -> int:
        self.setup_fds.append(fd)
        return 0


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

    def take(self) -> int:
        return 11


class FakeRemoteDesktopIface:
    """D-Bus EIS interface stub used by real-_setup tests (connectToEIS)."""

    def __init__(self) -> None:
        self.disconnected: list[int] = []

    def connectToEIS(self, caps: int) -> tuple[FakeFd, int]:  # noqa: N802
        return (FakeFd(), 42)

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
    fake = FakeLibei([(_EI_EVENT_DEVICE_PAUSED, POINTER)], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))
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
    # Fast-forward the readiness wait and the 5s negotiation deadline.
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

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
    """DISCONNECT during a drain must not raise; the NEXT injection rebuilds.

    Regression for B3: _handle_event used to raise RuntimeError straight out
    of the drain loop, bypassing the reconnect path entirely.
    """
    fake = FakeLibei([(_EI_EVENT_DISCONNECT, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.keyboard_key(30, _PRESSED)  # flush drains the DISCONNECT event
    assert client._connection_dead is True  # flagged, not raised
    assert fake.key_calls == [(30, _PRESSED)]

    client.keyboard_key(31, _PRESSED)  # readiness wait sees dead → reconnect
    assert setup_calls == [1]
    assert fake.key_calls == [(30, _PRESSED), (31, _PRESSED)]


def test_dispatch_failure_triggers_reconnect_on_next_injection(monkeypatch) -> None:
    """ei_dispatch() < 0 (dead socket, no DISCONNECT event) → reconnect path.

    Regression for B3(a): the negative return used to be ignored, leaving the
    client "emulating" forever and injecting into a void.
    """
    fake = FakeLibei([], {})
    fake.dispatch_result = -1
    _install(monkeypatch, fake)
    client = _client(fake)
    client._setup, setup_calls = _reconnecting_setup(client, emulating=True)

    client.keyboard_key(30, _PRESSED)  # flush marks the connection dead
    assert client._connection_dead is True

    client.keyboard_key(31, _PRESSED)  # next injection rebuilds the connection
    assert setup_calls == [1]
    assert fake.key_calls == [(30, _PRESSED), (31, _PRESSED)]


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
    fake = FakeLibei([(_EI_EVENT_DEVICE_PAUSED, POINTER)], {})
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

    # Fresh gestures work again from ID 0 on the new connection.
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
    # Exactly one reconnect attempt per ensure call (fresh handshake twice).
    assert fresh.setup_fds == [11, 11]


# ── F2: touch gesture calls must fetch the touch pointer AFTER the
#    readiness check, or a reconnect inside it turns the pointer dangling ──


def _touch_client(fake: FakeLibei, touch_id: int = 0, pointer: int = 0x777) -> EISClient:
    """A stalled client holding one active touch gesture."""
    client = _client(fake)
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
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _touch_client(fake)

    client.touch_move(0, 1.0, 2.0)

    assert fake.touch_motions == [(0x777, 1.0, 2.0)]
    assert client._active_touches == {0: 0x777}  # gesture continues


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
    fake = FakeLibei([(_EI_EVENT_DEVICE_PAUSED, TOUCH)], {TOUCH: {_EI_CAP_TOUCH}})
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
    fake = FakeLibei([(_EI_EVENT_DEVICE_PAUSED, POINTER)], {})
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
    """REMOVED(touch device) releases its logically-down touches too."""
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
    assert fake.touch_ups == [0x888]
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
        [(_EI_EVENT_DEVICE_PAUSED, POINTER), (_EI_EVENT_DEVICE_RESUMED, POINTER)],
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
    fake. The clock is fast-forwarded so the 5s stall wait and negotiation
    deadlines burn no wall time.
    """
    stale = FakeLibei(
        [],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    router = SwitchingLibei([stale, fresh])
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

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
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

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
