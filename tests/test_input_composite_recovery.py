"""Recovery semantics of composite input operations (issue #20).

``InputBackend.mouse_click(modifiers=[...])`` and ``mouse_drag(...)`` used to
send their transient modifier/button presses outside the client held-state
sets, so an EIS recovery (PAUSED/reconnect) mid-operation silently dropped
them while the operation continued: the click landed without its modifier
and the drag degraded to a button-less motion.

Both operations now register their transient state as temporary held
intents for the operation duration (option (a) of the issue): a
mid-operation recovery replays them through the production
``_replay_held_state`` path, the operation completes with modifiers/button
intact, and the held sets are empty afterwards. A failed operation releases
its intents instead of leaking them.

The recovery is injected from inside ``ei_dispatch``
(``RecoverySwitchingLibei`` queues a DISCONNECT on the Nth global dispatch):
pre-queued events would be consumed by the pre-send readiness drain and miss
the mid-operation window. The reconnect runs the REAL
``_setup``/``_negotiate_devices`` against the fresh fake, so the replay
under test is the production path, not a stub.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_input_device_lifecycle import (
    KEYBOARD,
    NEW_KEYBOARD,
    NEW_POINTER,
    POINTER,
    FakeBus,
    FakeLibei,
    FakeRemoteDesktopIface,
    SwitchingLibei,
    _client,
)

import kwin_mcp.input as input_module
from kwin_mcp.input import (
    _EI_CAP_KEYBOARD,
    _EI_CAP_POINTER_ABSOLUTE,
    _EI_EVENT_DEVICE_ADDED,
    _EI_EVENT_DEVICE_RESUMED,
    _EI_EVENT_DISCONNECT,
    _PRESSED,
    _RELEASED,
    EISClient,
    InputBackend,
)

CTRL = 29
ALT = 56
LEFT = 0x110


class RecoverySwitchingLibei(SwitchingLibei):
    """Route each EI context to its own fake; inject server events mid-operation.

    ``triggers`` maps a global 0-based ``ei_dispatch`` count to server events
    queued into the then-current connection, so the drain inside that call's
    post-send ``_flush`` observes a mid-operation DISCONNECT — the window a
    recovery-injecting fake must hit.
    """

    def __init__(
        self,
        fakes: list[FakeLibei],
        triggers: dict[int, list[tuple[int, int]]],
    ) -> None:
        super().__init__(fakes)
        self._triggers = dict(triggers)
        self.dispatches = 0

    def ei_dispatch(self, ei: int) -> int:
        assert ei != 0, "ei_dispatch called with NULL EI context (segfault guard)"
        pending = self._triggers.pop(self.dispatches, None)
        self.dispatches += 1
        if pending is not None:
            self.current.queue_events(pending)
        return self.current.dispatch_result


def _composite_setup(
    monkeypatch: Any,
    triggers: dict[int, list[tuple[int, int]]],
) -> tuple[InputBackend, EISClient, FakeLibei, FakeLibei]:
    """A live backend on the stale connection with a fresh handshake queued.

    Only the D-Bus layer is faked; ``_reconnect`` runs the real ``_setup``
    against the fresh fake, so a mid-operation recovery replays through the
    production ``_resume_device``/``_replay_held_state`` path. Sleeps are
    no-ops (timing is not under test); ``select`` is patched out like in the
    lifecycle harness.
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
    router = RecoverySwitchingLibei([stale, fresh], triggers)
    monkeypatch.setattr(input_module, "_get_libei", lambda: router)
    monkeypatch.setattr(input_module.select, "select", lambda *a, **k: ([], [], []))
    monkeypatch.setattr(input_module.dbus, "Interface", lambda *a, **k: FakeRemoteDesktopIface())
    monkeypatch.setattr(input_module.time, "sleep", lambda *_: None)

    client = _client(stale)
    client._bus = FakeBus()  # ty: ignore[invalid-assignment]
    backend = InputBackend.__new__(InputBackend)
    backend._client = client
    return backend, client, stale, fresh


def test_click_with_modifiers_survives_mid_operation_reconnect(monkeypatch: Any) -> None:
    """Ctrl+click with a DISCONNECT between the modifier DOWN and the click.

    Dispatches: move(0), modifier burst(1, injects DISCONNECT), click
    DOWN(2, reconnects first). The reconnect must replay the transient
    modifier so the click lands WITH Ctrl: the stale connection saw only
    the initial press, the fresh handshake replays it before the click
    frames, and the operation-end release clears the set.
    """
    backend, client, stale, fresh = _composite_setup(monkeypatch, {1: [(_EI_EVENT_DISCONNECT, 0)]})

    backend.mouse_click(10, 20, modifiers=["ctrl"])

    assert stale.key_calls == [(CTRL, _PRESSED)]
    assert stale.button_calls == []
    assert fresh.key_calls == [(CTRL, _PRESSED), (CTRL, _RELEASED)]
    assert fresh.button_calls == [(LEFT, _PRESSED), (LEFT, _RELEASED)]
    assert client._held_keys == set()
    assert client._held_buttons == set()
    assert client._connection_dead is False


def test_drag_with_modifier_survives_mid_operation_reconnect(monkeypatch: Any) -> None:
    """Alt+drag with a DISCONNECT between the button DOWN and the motion.

    Dispatches: move(0), modifier burst(1), button DOWN(2, injects
    DISCONNECT), motions(3+, reconnect first). Both the transient modifier
    and the drag button must be replayed on the fresh connection: the
    motion frames run with the button logically down, not as a
    button-less motion.
    """
    backend, client, stale, fresh = _composite_setup(monkeypatch, {2: [(_EI_EVENT_DISCONNECT, 0)]})

    backend.mouse_drag(10, 20, 30, 20, modifiers=["alt"])

    assert stale.key_calls == [(ALT, _PRESSED)]
    assert stale.button_calls == [(LEFT, _PRESSED)]
    assert fresh.key_calls == [(ALT, _PRESSED), (ALT, _RELEASED)]
    assert fresh.button_calls == [(LEFT, _PRESSED), (LEFT, _RELEASED)]
    assert client._held_keys == set()
    assert client._held_buttons == set()
    assert client._connection_dead is False
    # (10,20)->(30,20): 10 motion steps, each with the button down.
    assert [d for d in fresh.frames if d == NEW_KEYBOARD] == [NEW_KEYBOARD, NEW_KEYBOARD]
    assert [d for d in fresh.frames if d == NEW_POINTER] == [NEW_POINTER] * 12


def test_drag_button_replayed_after_mid_operation_reconnect(monkeypatch: Any) -> None:
    """Modifier-less drag with a DISCONNECT right after the button DOWN.

    Dispatches: move(0), button DOWN(1, injects DISCONNECT), motions(2+,
    reconnect first). Pins the button leg on its own: the fresh handshake
    replays the transient button press, the release ends the gesture, and
    no modifier traffic exists on either connection.
    """
    backend, client, stale, fresh = _composite_setup(monkeypatch, {1: [(_EI_EVENT_DISCONNECT, 0)]})

    backend.mouse_drag(10, 20, 30, 20)

    assert stale.button_calls == [(LEFT, _PRESSED)]
    assert stale.key_calls == []
    assert fresh.button_calls == [(LEFT, _PRESSED), (LEFT, _RELEASED)]
    assert fresh.key_calls == []
    assert client._held_keys == set()
    assert client._held_buttons == set()
    assert client._connection_dead is False


def test_click_keeps_pre_existing_cross_call_hold(monkeypatch: Any) -> None:
    """A cross-call hold survives a composite op; the op adds nothing lasting.

    Shift held via ``keyboard_key_down`` + Ctrl+click: both presses go out,
    the operation's drop removes only the transient Ctrl, and shift stays
    held for its pairing ``keyboard_key_up``.
    """
    backend, client, stale, _fresh = _composite_setup(monkeypatch, {})

    backend.keyboard_key_down("shift")
    assert client._held_keys == {42}

    backend.mouse_click(10, 20, modifiers=["ctrl"])

    assert stale.key_calls == [(42, _PRESSED), (CTRL, _PRESSED), (CTRL, _RELEASED)]
    assert client._held_keys == {42}
    assert client._held_buttons == set()

    backend.keyboard_key_up("shift")
    assert stale.key_calls[-1] == (42, _RELEASED)
    assert client._held_keys == set()


def test_click_failure_releases_transient_intent(monkeypatch: Any) -> None:
    """A ToolError mid-click releases the transient intent (no phantom hold).

    DISCONNECT injected on the click DOWN's own flush; the click UP then
    fails its readiness gate (reconnect stub raises). The operation's
    ``finally`` already dropped the transient Ctrl, so the held set stays
    empty instead of leaking a phantom hold into the next handshake replay.
    """
    backend, client, _stale, _fresh = _composite_setup(
        monkeypatch, {2: [(_EI_EVENT_DISCONNECT, 0)]}
    )

    def boom_setup() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_setup", boom_setup)

    with pytest.raises(ToolError):
        backend.mouse_click(10, 20, modifiers=["ctrl"])

    assert client._held_keys == set()
    assert client._held_buttons == set()
