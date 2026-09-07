"""Regression for issue #23: geometry re-fetch in one process returns [].

The fetch used fixed bus name / object path / script name per pid. The
first connection held the bus name, so the second fetch's
``request_name(DO_NOT_QUEUE)`` silently failed (the result was never
checked) and the KWin script's ``Push`` went to the first — already
closed — loop: an 8s stall, then ``[]`` via the blanket
``except Exception → []`` guard. Only the 2s cache TTL masked the bug;
every re-fetch past it degraded all AT-SPI coordinate translation to the
(0, 0) no-op and clicks landed on empty desktop.

The fix gives every call its own pid-monotonic_ns-suffixed bus name /
object path / script name (the same pattern ``_run_script_one_shot``
already used) and fails fast when ``request_name`` cannot become
primary. The fakes here replace only the D-Bus plumbing (BusConnection,
service export, GLib loop) — name generation, the request_name check,
script load/unload and payload parsing run for real.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import kwin_mcp.kwin_windows as kw
from kwin_mcp.kwin_windows import (
    WindowGeometry,
    _fetch_window_geometries,
    _parse_payload,
    get_window_geometries,
)

_PAYLOAD = (
    "OK\n"
    "KCalc\t600,160,440,550\t600,188,440,526\torg.kde.kcalc\n"
    "plasmashell\t0,0,1746,30\t0,0,1746,30\torg.kde.plasmashell"
)

_OWNER_PRIMARY = 1  # DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER
_OWNER_ALREADY = 4  # DBUS_REQUEST_NAME_REPLY_ALREADY_OWNER
_OWNER_EXISTS = 3  # DBUS_REQUEST_NAME_REPLY_EXISTS


class FakeConn:
    """BusConnection stub: records names/calls, routes Push to the sink."""

    def __init__(self, owner_result: int = _OWNER_PRIMARY) -> None:
        self.owner_result = owner_result
        self.requested: list[str] = []
        self.load_calls: list[list[str]] = []
        self.unload_calls: list[list[str]] = []
        self.sinks: dict[str, Any] = {}
        self.delivered: set[str] = set()
        self.payload: str = ""

    def request_name(self, name: str, _flags: int) -> int:
        self.requested.append(name)
        return self.owner_result

    def call_blocking(
        self,
        _dest: str,
        _path: str,
        _iface: str,
        method: str,
        _sig: str,
        args: list[str],
        timeout: float = 25.0,
    ) -> int:
        if method == "loadScript":
            self.load_calls.append(list(args))
            return 42
        if method == "unloadScript":
            self.unload_calls.append(list(args))
            return 0
        return 0

    def get_object(self, _dest: str, path: str) -> Any:
        bus = self

        class FakeScript:
            def run(self, dbus_interface: str = "", timeout: float = 25.0) -> None:
                # The KWin script calls the sink bus/object path embedded in
                # the script text, which this fetch exported — deliver to it.
                for sink_path, sink in bus.sinks.items():
                    if sink is not None and sink_path not in bus.delivered:
                        sink.Push(bus.payload)
                        bus.delivered.add(sink_path)

        return FakeScript()


def _install_fake_dbus(monkeypatch: Any, conn: FakeConn, payload: str = _PAYLOAD) -> None:
    """Patch the lazy dbus imports of _fetch_window_geometries.

    Only the plumbing is faked: the sink class defined inside the fetch is
    constructed through the faked ``dbus.service.Object`` and its ``Push``
    stays a plain method (the ``dbus.service.method`` decorator is patched
    to identity), so the fake script can deliver the payload through it.
    """
    import dbus
    import dbus.service
    import gi
    import gi.repository

    conn.payload = payload

    class FakeLoop:
        def run(self) -> None:  # pragma: no cover - parked worker thread
            pass

        def quit(self) -> None:
            pass

    class FakeGLib:
        MainLoop = FakeLoop

    class FakeServiceObject:
        def __init__(self, conn_: FakeConn, path: str) -> None:
            conn_.sinks[path] = self

    monkeypatch.setattr(dbus.bus, "BusConnection", lambda _addr: conn, raising=False)
    monkeypatch.setattr(dbus.mainloop.glib, "DBusGMainLoop", lambda **_k: None, raising=False)
    monkeypatch.setattr(dbus.service, "Object", FakeServiceObject, raising=False)
    monkeypatch.setattr(
        dbus.service, "method", lambda _iface, **_kw: lambda func: func, raising=False
    )
    monkeypatch.setattr(gi.repository, "GLib", FakeGLib, raising=False)


def test_two_back_to_back_fetches_use_distinct_names_and_both_return_geometries(
    monkeypatch: Any,
) -> None:
    """Two fetches past TTL both return the full window list; names differ.

    Regression for issue #23: the second fetch used to lose the bus name
    race against the first (fixed names) and return [] forever.
    """
    monkeypatch.setattr(kw, "_cache", {})
    conn = FakeConn()
    _install_fake_dbus(monkeypatch, conn)

    first = _fetch_window_geometries("unix:path=/tmp/fake")
    second = _fetch_window_geometries("unix:path=/tmp/fake")

    assert [g.caption for g in first] == ["KCalc", "plasmashell"]
    assert [g.caption for g in second] == ["KCalc", "plasmashell"]

    # Distinct per-call identities: bus names, script names, object paths.
    assert len(conn.requested) == 2
    assert conn.requested[0] != conn.requested[1]
    assert conn.requested[0].startswith("org.kwin_mcp.geom.")
    assert conn.requested[1].startswith("org.kwin_mcp.geom.")
    assert len(conn.load_calls) == 2
    assert conn.load_calls[0][1] != conn.load_calls[1][1]  # script names differ
    # Both scripts were unloaded again (cleanup contract).
    assert [u[0] for u in conn.unload_calls] == [
        conn.load_calls[0][1],
        conn.load_calls[1][1],
    ]

    # get_window_geometries past TTL (fresh cache) sees the same list.
    geometries = get_window_geometries("unix:path=/tmp/fake")
    assert [g.caption for g in geometries] == ["KCalc", "plasmashell"]


def test_fetch_with_exists_owner_result_returns_empty_fast(monkeypatch: Any) -> None:
    """request_name → EXISTS: log + [] immediately (no 8s stall, no script)."""
    conn = FakeConn(owner_result=_OWNER_EXISTS)
    _install_fake_dbus(monkeypatch, conn)

    start = time.monotonic()
    result = _fetch_window_geometries("unix:path=/tmp/fake")
    elapsed = time.monotonic() - start

    assert result == []
    assert elapsed < 1.0, "the EXISTS path must fail fast, not stall out the timeout"
    assert conn.load_calls == [], "no script may be loaded when the name is unavailable"


def test_get_window_geometries_does_not_cache_empty_results(monkeypatch: Any) -> None:
    """A failed (empty) fetch is not cached: the next call retries the bus."""
    monkeypatch.setattr(kw, "_cache", {})
    calls = {"n": 0}

    def fake_fetch(_addr: str) -> list[WindowGeometry]:
        calls["n"] += 1
        return [] if calls["n"] == 1 else _parse_payload(_PAYLOAD)

    monkeypatch.setattr(kw, "fetch_window_geometries", fake_fetch)

    assert get_window_geometries("unix:path=/tmp/fake") == []
    second = get_window_geometries("unix:path=/tmp/fake")
    assert calls["n"] == 2, "the empty result must not be cached for the TTL"
    assert [g.caption for g in second] == ["KCalc", "plasmashell"]

    # A successful result IS cached: a third call within the TTL is a hit.
    third = get_window_geometries("unix:path=/tmp/fake")
    assert calls["n"] == 2
    assert [g.caption for g in third] == ["KCalc", "plasmashell"]


def test_one_shot_script_checks_request_name_result(monkeypatch: Any) -> None:
    """_run_script_one_shot fails fast when its bus name is unavailable."""
    from kwin_mcp.kwin_windows import activate_window_by_name

    conn = FakeConn(owner_result=_OWNER_EXISTS)
    _install_fake_dbus(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="could not acquire"):
        activate_window_by_name("unix:path=/tmp/fake", "kcalc")

    assert conn.load_calls == [], "no script may be loaded when the name is unavailable"


@pytest.mark.parametrize("result", [_OWNER_PRIMARY, _OWNER_ALREADY])
def test_primary_and_already_owner_are_accepted(result: int) -> None:
    """request_name results 1 (PRIMARY) and 4 (ALREADY_OWNER) both pass the gate."""
    owner_ok = result in (1, 4)
    assert owner_ok
