"""Regression tests for the review follow-ups of PR #15 (issue #16).

Each test pins one F-item: lifecycle guarantees in ``kwin_mcp/input.py``
that the review found stated stronger than the code ensures. The tests run
on the FakeLibei harness shared with ``test_input_device_lifecycle.py``
(imported, not copied): device pointers are plain ints, events are queued
(type, device) tuples, ``select`` is patched out unless the test itself
pins select behavior (F8).
"""

from __future__ import annotations

import re
from typing import Any

import dbus
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_input_device_lifecycle import (
    COOKIE,
    KEYBOARD,
    POINTER,
    TOUCH,
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
    _EI_CAP_TOUCH,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_REMOVED,
    _EI_EVENT_DEVICE_RESUMED,
    _EI_EVENT_DISCONNECT,
    _EI_EVENT_SEAT_REMOVED,
    _RECONNECT_ATTEMPTS,
    EISClient,
)

MULTI = 0x301  # one handle with several capabilities (several slots)

# Literal event-type int (issue #24): tests must not feed the module
# constant back — a wrong constant would pass its own tests. The value is
# pinned against libei.h by test_libei_constants.py.
EI_PAUSED = 7


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


def test_remove_multi_cap_device_releases_each_slot_ref(monkeypatch: Any) -> None:
    """F2 (round 2, per-slot model): a handle in several slots is unref'd per slot.

    ``_register_device`` takes one ``ei_device_ref`` per occupied slot, so
    ``_remove_device`` must release one per cleared slot — the matching
    count, not one per unique handle.
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
    assert fake.unrefed_devices == [MULTI, MULTI]


def test_seat_removed_releases_each_slot_ref(monkeypatch: Any) -> None:
    """F2 (round 2, per-slot model): seat removal releases each slot's ref."""
    fake = FakeLibei([(_EI_EVENT_SEAT_REMOVED, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    client._pointer = MULTI
    client._keyboard = MULTI
    client._emulating_devices = {MULTI}

    client._drain_events()

    assert client._pointer == 0
    assert client._keyboard == 0
    # NOTE: the pre-seeded slots hold one ref each (as _register_device took
    # them), so both are released — see the counting balance tests below.
    assert fake.unrefed_devices == [MULTI, MULTI]


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

    monkeypatch.setattr(fake, "ei_device_stop_emulating", boom_stop)
    monkeypatch.setattr(fake, "ei_device_unref", boom_unref)
    client = _client(fake)
    monkeypatch.setattr(client, "_eis_iface", FakeIface())
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


class BoomUnrefLibei(FakeLibei):
    """FakeLibei whose ei_device_unref raises on every call (issue #18, R1)."""

    def ei_device_unref(self, device: int) -> None:
        raise RuntimeError("unref boom")


def test_remove_device_completes_when_unref_fails(monkeypatch: Any) -> None:
    """R1: a raising unref in ``_remove_device`` must not escape.

    The sibling release paths (``_handle_seat_removed``,
    ``_teardown_connection``, ``close``) suppress per-slot release failures,
    so every slot is still cleared and the touch cleanup still runs — the
    remove path must match that guarantee instead of aborting at the first
    failing slot.
    """
    fake = BoomUnrefLibei(
        [(_EI_EVENT_DEVICE_REMOVED, POINTER), (_EI_EVENT_DEVICE_REMOVED, KEYBOARD)],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}
    client._next_touch_id = 1

    client._drain_events()  # must not raise

    assert client._pointer == 0
    assert client._keyboard == 0
    # The pointer slot is cleared even though its unref raised, so the drain
    # reaches the keyboard event afterwards (previously it never did).
    assert client._emulating_devices == {TOUCH}
    # Touch state is untouched: only a touch-slot removal invalidates it.
    assert client._active_touches == {0: 0x888}
    assert client._next_touch_id == 1


def test_remove_touch_device_with_failing_unref_still_invalidates_touches(
    monkeypatch: Any,
) -> None:
    """R1 (touch half): the touch cleanup runs even when its unref raises.

    A failing ``ei_device_unref`` on the touch slot must not skip
    ``_invalidate_touches`` — the stored gesture pointers would otherwise
    dangle into the removed device.
    """
    fake = BoomUnrefLibei(
        [(_EI_EVENT_DEVICE_REMOVED, TOUCH)],
        {TOUCH: {_EI_CAP_TOUCH}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}
    client._next_touch_id = 1

    client._drain_events()  # must not raise

    assert client._touch_device == 0
    assert TOUCH not in client._emulating_devices
    assert client._active_touches == {}
    assert client._next_touch_id == 0
    assert fake.unrefed_touches == [0x888]


def test_remove_touch_device_sends_no_touch_up_but_still_unrefs(
    monkeypatch: Any,
) -> None:
    """R3: REMOVED releases gestures without a ``touch_up`` into the remove.

    Unlike PAUSED (finish + release), a removed device only gets a release:
    ``ei_touch`` is a separate refcounted object, but new events must not be
    sent into a device the server just dropped. The device ``unref`` itself
    still happens.
    """
    fake = FakeLibei(
        [(_EI_EVENT_DEVICE_REMOVED, TOUCH)],
        {TOUCH: {_EI_CAP_TOUCH}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}
    client._next_touch_id = 1

    client._drain_events()

    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x888]
    assert client._active_touches == {}
    assert client._next_touch_id == 0
    assert client._touch_device == 0
    assert TOUCH in fake.unrefed_devices


def test_pause_touch_device_still_sends_touch_up(monkeypatch: Any) -> None:
    """R3 (PAUSED keeps ``up``): a paused touch device finishes its gestures.

    The device handle stays valid on PAUSED — only its logical state resets
    to neutral — so a finishing ``touch_up`` is protocol-correct there, and
    the F6 dead-connection guard still suppresses it when the connection is
    dead instead.
    """
    fake = FakeLibei(
        [(EI_PAUSED, TOUCH)],
        {TOUCH: {_EI_CAP_TOUCH}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}

    client._drain_events()

    assert fake.touch_ups == [0x888]
    assert fake.unrefed_touches == [0x888]


def test_pause_touch_device_sends_no_touch_up_on_dead_connection(
    monkeypatch: Any,
) -> None:
    """F6 covers the teardown path; PAUSED must honour the same liveness rule.

    With ``_connection_dead`` set, pausing the touch device releases the
    gestures (unref + drop) without sending ``touch_up`` into the dead
    context.
    """
    fake = FakeLibei(
        [(EI_PAUSED, TOUCH)],
        {TOUCH: {_EI_CAP_TOUCH}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._connection_dead = True
    client._touch_device = TOUCH
    client._emulating_devices = {POINTER, KEYBOARD, TOUCH}
    client._active_touches = {0: 0x888}
    client._next_touch_id = 1

    client._drain_events()

    assert fake.touch_ups == []
    assert fake.unrefed_touches == [0x888]
    assert client._active_touches == {}
    assert client._next_touch_id == 0


def test_reconnect_loop_bounded_by_wall_clock(monkeypatch: Any) -> None:
    """F7: a wall-clock deadline bounds one recovery call, not just a count.

    With a fast-forwarded clock the retry loop must stop on the deadline
    even though the attempt count is not exhausted: fewer reconnects than
    ``_RECONNECT_ATTEMPTS`` are made, and the ToolError names the attempts
    actually made (consistent with the reconnects performed, not a magic
    constant).
    """
    fake = FakeLibei([], {})
    _install(monkeypatch, fake)
    monkeypatch.setattr(input_module, "time", FakeClock(step=1.0))
    client = _client(fake)
    client._emulating_devices = set()  # stalled: every attempt fails
    client._setup, setup_calls = _reconnecting_setup(client, emulating=False)

    with pytest.raises(ToolError) as exc_info:
        client._ensure_devices_ready(timeout_s=0.05)

    # The wall-clock budget fired before the attempt-count budget.
    assert 0 < len(setup_calls) < _RECONNECT_ATTEMPTS
    # The message reports exactly the attempts actually made.
    match = re.search(r"after (\d+) reconnect attempts?", str(exc_info.value))
    assert match is not None
    assert int(match.group(1)) == len(setup_calls)


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


def test_wait_emulating_fails_on_queued_disconnect(monkeypatch: Any) -> None:
    """Dead wait: a drained DISCONNECT beats a full emulation set.

    The readiness probe must not short-circuit on the stale
    ``_emulating_devices`` after the drain just marked the connection dead —
    the caller must take the reconnect path (honest delivery).
    """
    fake = FakeLibei([(_EI_EVENT_DISCONNECT, 0)], {})
    _install(monkeypatch, fake)
    client = _client(fake)
    assert client._emulating_devices == {POINTER, KEYBOARD}

    assert client._wait_emulating(0.05) is False
    assert client._connection_dead is True


def test_wait_emulating_fails_when_disconnect_follows_resumed(
    monkeypatch: Any,
) -> None:
    """Dead wait: RESUMEDs before a queued DISCONNECT still end the wait dead.

    The pre-DISCONNECT RESUMEDs belong to the live connection (F5
    drain-break semantics — they still start emulation), but the trailing
    DISCONNECT kills it, so the wait reports False with the dead flag set
    for the reconnect path.
    """
    fake = FakeLibei(
        [
            (_EI_EVENT_DEVICE_RESUMED, POINTER),
            (_EI_EVENT_DEVICE_RESUMED, KEYBOARD),
            (_EI_EVENT_DISCONNECT, 0),
        ],
        {POINTER: {_EI_CAP_POINTER_ABSOLUTE}, KEYBOARD: {_EI_CAP_KEYBOARD}},
    )
    _install(monkeypatch, fake)
    client = _client(fake)
    client._emulating_devices = set()  # paused: the RESUMEDs restart emulation

    assert client._wait_emulating(0.5) is False
    assert client._connection_dead is True
    assert [d for d, _ in fake.started] == sorted([POINTER, KEYBOARD])


# ── Round 2, W1: one ownership model on every path ────────────────────────
#
# Ownership is per slot: ``_register_device`` takes one ``ei_device_ref``
# for every slot a handle occupies, so every release path (remove,
# seat-removed, teardown, close) must ``ei_device_unref`` once per cleared
# slot. For each unique handle the taken refs must equal the released
# unrefs — proved below with a counting fake on the real event paths.


class CountingRefLibei(FakeLibei):
    """FakeLibei additionally counting every ei_device_ref per handle (W1)."""

    def __init__(
        self,
        events: list[tuple[int, int]],
        device_caps: dict[int, set[int]],
    ) -> None:
        super().__init__(events, device_caps)
        self.refed_devices: list[int] = []

    def ei_device_ref(self, device: int) -> int:
        self.refed_devices.append(device)
        return device


def _registered_multi_cap_client(monkeypatch: Any, fake: CountingRefLibei) -> EISClient:
    """A client holding MULTI in pointer + keyboard via the real ADDED path."""
    _install(monkeypatch, fake)
    client = _client(fake)
    client._pointer = 0
    client._keyboard = 0
    client._touch_device = 0
    client._text_device = 0
    client._emulating_devices = set()
    client._drain_events()  # DEVICE_ADDED registers MULTI into every matching slot
    assert client._pointer == MULTI
    assert client._keyboard == MULTI
    return client


def _assert_refs_balanced(fake: CountingRefLibei, handle: int) -> None:
    """Every taken ei_device_ref for the handle has a matching unref (W1)."""
    taken = fake.refed_devices.count(handle)
    released = fake.unrefed_devices.count(handle)
    assert taken > 0
    assert released == taken


def test_register_multi_cap_device_refs_once_per_slot(monkeypatch: Any) -> None:
    """W1 (acquire side): one handle in two slots takes two refs."""
    fake = CountingRefLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD}},
    )
    _registered_multi_cap_client(monkeypatch, fake)

    assert fake.refed_devices == [MULTI, MULTI]


def test_remove_multi_cap_device_balances_refs(monkeypatch: Any) -> None:
    """W1 (remove path): taken refs equal released unrefs for the handle."""
    fake = CountingRefLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD}},
    )
    client = _registered_multi_cap_client(monkeypatch, fake)

    fake.queue_events([(_EI_EVENT_DEVICE_REMOVED, MULTI)])
    client._drain_events()

    assert client._pointer == 0
    assert client._keyboard == 0
    _assert_refs_balanced(fake, MULTI)


def test_seat_removed_balances_refs(monkeypatch: Any) -> None:
    """W1 (seat-removed path): taken refs equal released unrefs."""
    fake = CountingRefLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD}},
    )
    client = _registered_multi_cap_client(monkeypatch, fake)
    client._emulating_devices = {MULTI}

    fake.queue_events([(_EI_EVENT_SEAT_REMOVED, 0)])
    client._drain_events()

    assert client._pointer == 0
    assert client._keyboard == 0
    _assert_refs_balanced(fake, MULTI)


def test_teardown_balances_refs_and_stops_once(monkeypatch: Any) -> None:
    """W1 (teardown path): refs balanced; emulation stopped once per handle.

    Ownership is per slot (unref per cleared slot) while emulation state is
    per handle (one stop for the shared handle).
    """
    fake = CountingRefLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD}},
    )
    client = _registered_multi_cap_client(monkeypatch, fake)
    client._emulating_devices = {MULTI}

    client._teardown_connection()

    assert client._pointer == 0
    assert client._keyboard == 0
    _assert_refs_balanced(fake, MULTI)
    assert fake.stopped == [MULTI]


def test_close_balances_refs_and_clears_shared_slots(monkeypatch: Any) -> None:
    """W1 (close path): a 3-slot handle balances; no stale slot survives.

    A handle shared with the touch slot used to leave that slot pointing at
    the freed handle (and was stopped twice); close must clear every slot.
    """
    fake = CountingRefLibei(
        [(_EI_EVENT_DEVICE_ADDED, MULTI)],
        {MULTI: {_EI_CAP_POINTER_ABSOLUTE, _EI_CAP_KEYBOARD, _EI_CAP_TOUCH}},
    )
    client = _registered_multi_cap_client(monkeypatch, fake)
    assert client._touch_device == MULTI
    client._emulating_devices = {MULTI}

    client.close()

    _assert_refs_balanced(fake, MULTI)
    assert fake.stopped == [MULTI]
    assert (client._pointer, client._keyboard, client._touch_device, client._text_device) == (
        0,
        0,
        0,
        0,
    )
    assert client._ei == 0


def test_close_completes_when_cleanup_step_fails(monkeypatch: Any) -> None:
    """M3: one failing close() cleanup step must not abort the rest.

    Mirrors the F4 teardown guarantee: a failing ``stop_emulating``/``unref``
    or D-Bus ``disconnect`` is tolerated per step, every slot is still
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

    def boom_disconnect(cookie: int) -> None:
        raise dbus.DBusException("bus gone")

    monkeypatch.setattr(fake, "ei_device_stop_emulating", boom_stop)
    monkeypatch.setattr(fake, "ei_device_unref", boom_unref)
    client = _client(fake)
    iface = FakeIface()
    monkeypatch.setattr(iface, "disconnect", boom_disconnect)
    monkeypatch.setattr(client, "_eis_iface", iface)
    client._cookie = COOKIE

    client.close()  # must not raise

    assert client._pointer == 0
    assert client._keyboard == 0
    assert client._touch_device == 0
    assert client._text_device == 0
    assert sorted(attempted_unref) == sorted([POINTER, KEYBOARD])
    assert fake.unrefed_ei == [1]
    assert client._ei == 0
    assert client._cookie == 0
