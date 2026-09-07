"""Regression tests for the review follow-ups of PR #15 (issue #16).

Each test pins one F-item: lifecycle guarantees in ``kwin_mcp/input.py``
that the review found stated stronger than the code ensures. The tests run
on the FakeLibei harness shared with ``test_input_device_lifecycle.py``
(imported, not copied): device pointers are plain ints, events are queued
(type, device) tuples, ``select`` is patched out unless the test itself
pins select behavior (F8).
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_input_device_lifecycle import (
    COOKIE,
    KEYBOARD,
    POINTER,
    FakeClock,
    FakeIface,
    FakeLibei,
    _client,
    _install,
    _reconnecting_setup,
)

import kwin_mcp.input as input_module
from kwin_mcp.input import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_REMOVED,
    _EI_EVENT_DEVICE_RESUMED,
    _EI_EVENT_DISCONNECT,
    _EI_EVENT_SEAT_REMOVED,
)

MULTI = 0x301  # one handle with several capabilities (several slots)


class CountingUnrefLibei(FakeLibei):
    """FakeLibei additionally recording every ei_event_unref call (F1)."""

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
    ) -> None:
        super().__init__(events, device_caps)
        self.unrefed_events: list[int] = []

    def ei_event_unref(self, event: int) -> None:
        self.unrefed_events.append(event)


def test_drain_events_unref_on_handler_error(monkeypatch: Any) -> None:
    """F1: a raising handler must not leak the event reference.

    ``ei_event_unref`` has a refcount contract — skipping it on the error
    path leaks one FFI reference per unexpected handler failure.
    """
    fake = CountingUnrefLibei([(_EI_EVENT_SEAT_REMOVED, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)

    def boom(event: int) -> None:
        raise RuntimeError("handler boom")

    monkeypatch.setattr(client, "_handle_event", boom)

    with pytest.raises(RuntimeError, match="handler boom"):
        client._drain_events()
    assert len(fake.unrefed_events) == 1


def test_remove_multi_cap_device_unref_once(monkeypatch: Any) -> None:
    """F2: a handle occupying several slots is unref'd exactly once on remove.

    ``_register_device`` places one multi-capability handle into every
    matching slot; ``_remove_device`` must release the unique handle once,
    not once per slot.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._pointer = 0
    client._keyboard = 0
    client._emulating_devices = set()

    client._drain_events()  # DEVICE_ADDED registers MULTI into both slots
    assert client._pointer == MULTI
    assert client._keyboard == MULTI

    fake.queue_events([(_EI_EVENT_DEVICE_REMOVED, MULTI)])
    client._drain_events()

    assert client._pointer == 0
    assert client._keyboard == 0
    assert fake.unrefed_devices == [MULTI]


def test_seat_removed_unref_multi_cap_device_once(monkeypatch: Any) -> None:
    """F2: seat removal releases each unique handle once, not once per slot."""
    fake = FakeLibei([(_EI_EVENT_SEAT_REMOVED, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._pointer = MULTI
    client._keyboard = MULTI
    client._emulating_devices = {MULTI}

    client._drain_events()

    assert client._pointer == 0
    assert client._keyboard == 0
    assert fake.unrefed_devices == [MULTI]


def test_flush_on_released_context_raises_tool_error(monkeypatch: Any) -> None:
    """F3: flushing with a released context reports failure, not success.

    A silent return after ``_ei == 0`` claims an injection reached libei
    that never did — the honest-delivery contract (#234) requires a
    ToolError instead.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._ei = 0

    with pytest.raises(ToolError):
        client._flush()
    assert client._connection_dead is True


def test_teardown_completes_when_cleanup_step_fails(monkeypatch: Any) -> None:
    """F4: one failing cleanup step must not abort the rest of the teardown.

    The docstring promises the cleanup "can never abort halfway": a failing
    ``stop_emulating``/``unref`` is tolerated per step, every slot is still
    cleared and the EI context is still released and reset.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)

    def boom_stop(device: int) -> None:
        raise RuntimeError("stop boom")

    attempted_unref: list[int] = []

    def boom_unref(device: int) -> None:
        attempted_unref.append(device)
        raise RuntimeError("unref boom")

    fake.ei_device_stop_emulating = boom_stop  # type: ignore[method-assign]
    fake.ei_device_unref = boom_unref  # type: ignore[method-assign]
    client = _client(fake)
    client._eis_iface = FakeIface()
    client._cookie = COOKIE

    client._teardown_connection()  # must not raise

    assert client._pointer == 0
    assert client._keyboard == 0
    assert sorted(attempted_unref) == sorted([POINTER, KEYBOARD])
    assert fake.unrefed_ei == [1]
    assert client._ei == 0
    assert client._cookie == 0
    assert client._connection_dead is False


def test_drain_stops_after_disconnect(monkeypatch: Any) -> None:
    """F5: events queued after DISCONNECT belong to the dead connection.

    A RESUMED later in the same queue must not ``start_emulating`` (nor
    replay held state) on the connection DISCONNECT just killed.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DISCONNECT, 0), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()  # paused: a RESUMED would restart it
    client._held_keys = {42}

    client._drain_events()

    assert client._connection_dead is True
    assert fake.started == []
    assert fake.key_calls == []


def test_teardown_sends_no_touch_up_on_dead_connection(monkeypatch: Any) -> None:
    """F6: releasing touches on a dead connection unrefs + drops, never sends.

    ``ei_touch_up`` into a dead context is meaningless; the teardown must
    still release every gesture object and reset the IDs.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._connection_dead = True
    client._active_touches = {0: 0x777}
    client._next_touch_id = 1

    client._teardown_connection()

    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x777]
    assert client._active_touches == {}
    assert client._next_touch_id == 0
    assert client._connection_dead is False


def test_reconnect_loop_bounded_by_wall_clock(monkeypatch: Any) -> None:
    """F7: a wall-clock deadline bounds one recovery call, not just a count.

    With a fast-forwarded clock the retry loop must stop after the deadline
    even though the attempt count is not exhausted — and the ToolError names
    the attempts actually made.
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=1.0))
    client = _client(fake)
    client._emulating_devices = set()  # stalled: every attempt fails
    client._setup, setup_calls = _reconnecting_setup(client, emulating=False)

    with pytest.raises(ToolError, match="after 1 reconnect attempt"):
        client._ensure_devices_ready(timeout_s=0.05)

    assert setup_calls == [1]


def test_wait_emulating_sees_queued_events_without_select(monkeypatch: Any) -> None:
    """F8: already-queued events are drained + probed before the first wait.

    A RESUMED sitting in the queue from a previous dispatch must satisfy
    the readiness probe without paying a select round-trip first.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_RESUMED, POINTER), (_EI_EVENT_DEVICE_RESUMED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)

    def no_select(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("select must not run before queued events are drained")

    monkeypatch.setattr(input_module.select, "select", no_select)
    client = _client(fake)
    client._emulating_devices = set()

    assert client._wait_emulating(0.5) is True
    assert client._emulating_devices == {POINTER, KEYBOARD}
