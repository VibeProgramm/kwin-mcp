"""Compositor-side toplevel window geometries via KWin scripting.

Why this module exists: Wayland clients cannot know their own position on
screen, so AT-SPI "screen" coordinates reported by toolkits such as Qt are
relative to the window's client area instead of the actual screen origin.
Clicking those raw coordinates lands on empty desktop while keyboard input
(which needs no coordinates) keeps working.

This module queries the real client-area origins from the compositor (KWin
knows every window position) so that accessibility coordinates can be
translated into true screen coordinates before input injection.

The query is best-effort: any failure (no session bus, scripting disabled,
timeout) yields an empty list and callers must fall back to untranslated
coordinates.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass


@dataclass
class WindowGeometry:
    """Compositor-side geometry of a single toplevel window."""

    caption: str
    resource_class: str
    client_x: int
    client_y: int
    client_w: int
    client_h: int
    frame_x: int
    frame_y: int
    frame_w: int
    frame_h: int


#: How long a fetched geometry list is reused (keyed by session bus address).
GEOMETRY_CACHE_TTL_S = 2.0

#: Upper bound for a single KWin scripting round-trip.
FETCH_TIMEOUT_S = 8.0

_GEOM_SCRIPT_TEMPLATE = """try {
    var wins = workspace.windowList();
    var out = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var f = w.frameGeometry, c = w.clientGeometry;
        out.push(w.caption + "\\t" + f.x + "," + f.y + "," + f.width + "," + f.height
            + "\\t" + c.x + "," + c.y + "," + c.width + "," + c.height
            + "\\t" + w.resourceClass);
    }
    callDBus("@BUS@", "@PATH@", "@IFACE@", "Push", "OK\\n" + out.join("\\n"));
} catch (e) { callDBus("@BUS@", "@PATH@", "@IFACE@", "Push", "ERROR " + e); }
"""

_cache: dict[str, tuple[float, list[WindowGeometry]]] = {}


def get_window_geometries(dbus_address: str = "") -> list[WindowGeometry]:
    """Return cached compositor-side window geometries for a session.

    Args:
        dbus_address: Session bus address. Defaults to
            $DBUS_SESSION_BUS_ADDRESS.

    Returns:
        List of window geometries (possibly empty on any failure).
    """
    address = dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        return []
    now = time.monotonic()
    cached = _cache.get(address)
    if cached is not None and now - cached[0] < GEOMETRY_CACHE_TTL_S:
        return cached[1]
    geometries = fetch_window_geometries(address)
    _cache[address] = (now, geometries)
    return geometries


def resolve_offset(
    geometries: list[WindowGeometry],
    app_name: str,
    window_name: str,
    window_x: int,
    window_y: int,
) -> tuple[int, int]:
    """Compute the screen offset for an AT-SPI top-level window.

    Args:
        geometries: Compositor-side geometries from get_window_geometries().
        app_name: AT-SPI application name (used to disambiguate).
        window_name: AT-SPI window name; matched against KWin captions.
        window_x: Raw AT-SPI window x (client-area origin as seen by the app).
        window_y: Raw AT-SPI window y.

    Returns:
        (dx, dy) to add to raw AT-SPI coordinates. (0, 0) when the window
        cannot be matched unambiguously.
    """
    if not window_name:
        return (0, 0)
    candidates = [g for g in geometries if g.caption == window_name]
    if not candidates:
        return (0, 0)
    if len(candidates) > 1 and app_name:
        narrowed = [g for g in candidates if app_name.lower() in g.resource_class.lower()]
        if narrowed:
            candidates = narrowed
    if len(candidates) != 1:
        return (0, 0)
    geom = candidates[0]
    return (geom.client_x - window_x, geom.client_y - window_y)


def fetch_window_geometries(dbus_address: str) -> list[WindowGeometry]:
    """Query KWin for toplevel client geometries via a one-shot script.

    Loads a temporary KWin script that pushes all window geometries back
    over D-Bus, waits for the reply, then unloads the script again.

    Args:
        dbus_address: Session bus address of the KWin instance.

    Returns:
        List of window geometries (possibly empty on any failure).
    """
    try:
        return _fetch_window_geometries(dbus_address)
    except Exception:
        return []


def _fetch_window_geometries(dbus_address: str) -> list[WindowGeometry]:
    import dbus
    import dbus.bus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(dbus_address)

    bus_name = f"org.kwin_mcp.geom.pid{os.getpid()}"
    object_path = "/org/kwin_mcp/Geom"
    interface = "org.kwin_mcp.Geom"
    bus.request_name(bus_name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)

    received: dict[str, str] = {}
    loop = GLib.MainLoop()

    class _Sink(dbus.service.Object):
        @dbus.service.method(interface, in_signature="s", out_signature="")
        def Push(self, payload: str) -> None:  # noqa: N802 - D-Bus method name must match script call
            received["payload"] = str(payload)
            loop.quit()

    _Sink(bus, object_path)
    worker = threading.Thread(target=loop.run, daemon=True)
    worker.start()

    script_name = f"kwinmcp-geom-pid{os.getpid()}"
    script_text = (
        _GEOM_SCRIPT_TEMPLATE.replace("@BUS@", bus_name)
        .replace("@PATH@", object_path)
        .replace("@IFACE@", interface)
    )
    script_id = -1
    try:
        with tempfile.TemporaryDirectory(prefix="kwinmcp-geom-") as tmpdir:
            script_path = os.path.join(tmpdir, "geom.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(script_text)
            script_id = bus.call_blocking(
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting",
                "loadScript",
                "ss",
                [script_path, script_name],
            )
            if int(script_id) < 0:
                return []
            bus.get_object("org.kde.KWin", f"/Scripting/Script{int(script_id)}").run(
                dbus_interface="org.kde.kwin.Script"
            )
            deadline = time.monotonic() + FETCH_TIMEOUT_S
            while "payload" not in received and time.monotonic() < deadline:
                time.sleep(0.05)
    finally:
        try:
            if int(script_id) >= 0:
                bus.call_blocking(
                    "org.kde.KWin",
                    "/Scripting",
                    "org.kde.kwin.Scripting",
                    "unloadScript",
                    "s",
                    [script_name],
                )
        except Exception:
            pass
        loop.quit()
        worker.join(timeout=2.0)

    return _parse_payload(received.get("payload", ""))


def _parse_payload(payload: str) -> list[WindowGeometry]:
    """Parse the geometry script reply into WindowGeometry entries."""
    lines = payload.splitlines()
    if not lines or lines[0] != "OK":
        return []
    geometries: list[WindowGeometry] = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        caption, frame_s, client_s, resource_class = fields
        try:
            frame = [int(v) for v in frame_s.split(",")]
            client = [int(v) for v in client_s.split(",")]
            if len(frame) != 4 or len(client) != 4:
                continue
        except ValueError:
            continue
        geometries.append(
            WindowGeometry(
                caption=caption,
                resource_class=resource_class,
                client_x=client[0],
                client_y=client[1],
                client_w=client[2],
                client_h=client[3],
                frame_x=frame[0],
                frame_y=frame[1],
                frame_w=frame[2],
                frame_h=frame[3],
            )
        )
    return geometries
