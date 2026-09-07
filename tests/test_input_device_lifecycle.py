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
  resuming (``_reconnect``).

The fake libei below follows the same pattern as test_input_resumed.py:
device pointers are plain ints, events are queued (type, device) tuples and
popped in order; ``select`` is patched out so the drain loops run without a
real file descriptor.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kwin_mcp.input as input_module
from kwin_mcp.input import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_PAUSED,
    _EI_EVENT_DEVICE_REMOVED,
    _EI_EVENT_DEVICE_RESUMED,
    _PRESSED,
    _RELEASED,
    EISClient,
)

POINTER = 0x101
KEYBOARD = 0x102
NEW_POINTER = 0x201
NEW_KEYBOARD = 0x202
COOKIE = 7


class FakeLibei:
    """Minimal libei stub covering device lifecycle calls and key injection."""

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
        self.key_calls: list[tuple[int, int]] = []  # (keycode, state)
        self.frames: list[int] = []  # device per frame call

    def ei_get_fd(self, ei: int) -> int:
        return 9

    def ei_dispatch(self, ei: int) -> int:
        return 0

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

    def ei_device_frame(self, device: int, time_us: int) -> None:
        self.frames.append(device)


class FakeClock:
    """Monotonic clock advancing by a fixed step per call (fast-forwards waits)."""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._now += self._step
        return self._now


class FakeIface:
    """D-Bus EIS interface stub recording disconnect(cookie) calls."""

    def __init__(self) -> None:
        self.disconnected: list[int] = []

    def disconnect(self, cookie: int) -> None:
        self.disconnected.append(int(cookie))


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
    client._eis_iface = None
    client._cookie = 0
    return client


def _install(monkeypatch, fake: FakeLibei) -> None:
    monkeypatch.setattr(input_module, "_get_libei", lambda: fake)
    # Never readable: the wait loop still drains the event queue below.
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))


def _reconnecting_setup(client: EISClient, emulating: bool):
    """Build a _setup stub simulating a fresh handshake after _reconnect."""
    calls: list[int] = []

    def fake_setup() -> None:
        calls.append(1)
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


def test_ensure_devices_ready_raises_after_failed_reconnect(monkeypatch) -> None:
    """Stall persists even after reconnecting → RuntimeError."""
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()
    client._setup, setup_calls = _reconnecting_setup(client, emulating=False)
    # Fast-forward both waits (0.05s + the post-reconnect 3.0s) via a fake clock.
    monkeypatch.setattr(input_module, "time", FakeClock(step=0.5))

    with pytest.raises(RuntimeError, match="not in emulating state"):
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


def test_flush_replaces_readded_device(monkeypatch) -> None:
    """A re-advertised device replaces the stale reference and unrefs it."""
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_ADDED, NEW_POINTER)],
        {NEW_POINTER: {_EI_CAP_POINTER_ABSOLUTE}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._pointer == NEW_POINTER
    assert fake.unrefed_devices == [POINTER]
    assert POINTER not in client._emulating_devices
    assert NEW_POINTER not in client._emulating_devices  # not resumed yet


def test_flush_processes_removed_device(monkeypatch) -> None:
    """DEVICE_REMOVED drops the reference and the emulation state."""
    fake = FakeLibei([(_EI_EVENT_DEVICE_REMOVED, KEYBOARD)], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    client._flush()

    assert client._keyboard == 0
    assert fake.unrefed_devices == [KEYBOARD]
    assert client._emulating_devices == {POINTER}


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


def test_reconnect_stub_accepts_namespace(monkeypatch) -> None:
    """Sanity: the fake _setup mechanism used above behaves as expected."""
    client = _client(FakeLibei([], {}))
    client._setup, calls = _reconnecting_setup(client, emulating=True)
    assert isinstance(calls, list)
    client._setup()
    assert calls == [1]
    assert isinstance(SimpleNamespace, type)  # placeholder guard for imports
