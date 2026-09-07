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

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)


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


def _parse_coord(value: str) -> int:
    """Parse one KWin geometry component into device pixels.

    Under fractional scaling KWin reports subpixel (fractional) values such
    as ``601.8031365528899``. They are rounded half-up (ties away from zero)
    to the nearest device pixel; plain truncation would shift the reported
    window origin by up to a pixel and misplace translated clicks.
    """
    return int(Decimal(value.strip()).to_integral_value(rounding=ROUND_HALF_UP))


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
    if geometries:
        # Only successful fetches are cached (issue #24): a transient failure
        # (bus hiccup, scripting disabled) would otherwise pin an empty list
        # for the full TTL and degrade every click in the window to the
        # (0, 0) no-op offset — compounding inside wait_for_element loops.
        # An empty result falls through to the next call, which retries.
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

    # Unique names per call (issue #23): a fixed bus name is held by the
    # first connection, so every later fetch's request_name(DO_NOT_QUEUE)
    # silently fails and the script's Push lands on the zombie loop — 8s
    # stall, then [] via the blanket guard. Same pid-monotonic_ns suffix
    # pattern _run_script_one_shot already uses for activate/list.
    suffix = f"{os.getpid()}-{time.monotonic_ns() % 1_000_000}"
    bus_name = f"org.kwin_mcp.geom.{suffix}"
    object_path = f"/org/kwin_mcp/Geom/{suffix}"
    interface = "org.kwin_mcp.Geom"
    # Request the primary name; fail fast instead of stalling. DBUS_
    # REQUEST_REPLY_PRIMARY_OWNER (1) = we own it, ALREADY_OWNER (4) = this
    # connection holds it already. Any other value (EXISTS/IN_QUEUE) means
    # another connection owns the name and would swallow the script's Push.
    owner = bus.request_name(bus_name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    if owner not in (1, 4):
        logger.warning("geometry fetch: bus name %s unavailable (%s)", bus_name, owner)
        return []

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

    script_name = f"kwinmcp-geom-{suffix}"
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
            # Explicit bounds (issue #24): dbus-python's 25s default per call
            # turns one scripting hiccup into ~58s of stuck tool call
            # (load + run + unload); 10s each is generous for KWin scripting.
            script_id = bus.call_blocking(
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting",
                "loadScript",
                "ss",
                [script_path, script_name],
                timeout=10.0,
            )
            if int(script_id) < 0:
                return []
            bus.get_object("org.kde.KWin", f"/Scripting/Script{int(script_id)}").run(
                dbus_interface="org.kde.kwin.Script", timeout=10.0
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
                    timeout=10.0,
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
            frame = [_parse_coord(v) for v in frame_s.split(",")]
            client = [_parse_coord(v) for v in client_s.split(",")]
            if len(frame) != 4 or len(client) != 4:
                continue
        except (ValueError, InvalidOperation, OverflowError):
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


# ── Window activation + listing via KWin scripting (A-2 / H-2 fix) ───────
#
# AT-SPI grabFocus() on a top-level window does not move compositor-level
# focus (Qt only marks the widget focused), so focus_window reported success
# while the window stayed inactive. The KWin scripting API is the reliable
# activation path (same mechanism kdotool uses): workspace.activeWindow = w.
# Window listing through scripting also sees windows whose apps never
# registered with AT-SPI (e.g. apps running before session_connect), which
# the AT-SPI enumeration misses (H-2).
#
# JS templates written for kwin-mcp following KWin scripting docs; the
# loadScript/run/callDBus cycle mirrors _fetch_window_geometries above
# (same pattern as kdotool's tempfile + loadScript approach).


_ACTIVATE_SCRIPT_TEMPLATE = """try {
    var wanted = "__APP_NAME__".toLowerCase();
    var wins = workspace.windowList();
    var target = null;
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var cls = (w.resourceClass || "").toLowerCase();
        var cap = (w.caption || "").toLowerCase();
        var res = (w.resourceName || "").toLowerCase();
        if (cls.indexOf(wanted) >= 0 || cap.indexOf(wanted) >= 0 || res.indexOf(wanted) >= 0) {
            target = w;
            if (w.normalWindow) { break; }
        }
    }
    if (target) {
        workspace.activeWindow = target;
        callDBus("__BUS_NAME__", "__OBJECT_PATH__", "__INTERFACE_NAME__", "Push", "OK");
    } else {
        callDBus("__BUS_NAME__", "__OBJECT_PATH__", "__INTERFACE_NAME__", "Push", "not_found");
    }
} catch (e) {
    callDBus("__BUS_NAME__", "__OBJECT_PATH__", "__INTERFACE_NAME__", "Push",
        "ERROR " + e);
}
"""

_LIST_SCRIPT_TEMPLATE = """try {
    var wins = workspace.windowList();
    var out = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var active = (workspace.activeWindow === w) ? " [active]" : "";
        var minimized = w.minimized ? " [minimized]" : "";
        out.push(w.resourceClass + "\\t" + w.internalId + "\\t"
            + (w.caption || "(untitled)") + "\\t" + w.pid + active + minimized);
    }
    callDBus("__BUS_NAME__", "__OBJECT_PATH__", "__INTERFACE_NAME__", "Push",
        "OK\\n" + out.join("\\n"));
} catch (e) {
    callDBus("__BUS_NAME__", "__OBJECT_PATH__", "__INTERFACE_NAME__", "Push",
        "ERROR " + e);
}
"""


def parse_script_result(payload: str | None, app_name: str) -> str:
    """Translate a scripting Push payload into a user-facing outcome string.

    Args:
        payload: The string delivered by the script's callDBus, or None on
            timeout.
        app_name: The app name the script searched for (used in messages).

    Returns:
        Outcome string; raises RuntimeError when the script produced no
        result within the timeout.
    """
    if payload is None:
        msg = f"KWin script timed out while activating '{app_name}'"
        raise RuntimeError(msg)
    if payload.startswith("ERROR "):
        return f"KWin script error: {payload[6:]}"
    if payload == "not_found":
        return f"No window matching '{app_name}' found"
    if payload.startswith("OK\n"):
        return payload[3:]
    return payload


def _run_script_one_shot(
    dbus_address: str,
    script_text: str,
    script_name: str,
    *,
    timeout: float = FETCH_TIMEOUT_S,
) -> str | None:
    """Run a one-shot KWin script and return its Push payload.

    Shares the tempfile + loadScript/run/unload + callDBus result flow with
    _fetch_window_geometries; returns the pushed string or None on timeout.
    """
    import dbus
    import dbus.bus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(dbus_address)

    suffix = f"{os.getpid()}-{time.monotonic_ns() % 1_000_000}"
    bus_name = f"org.kwin_mcp.script.{script_name}.pid{suffix}"
    object_path = "/org/kwin_mcp/ScriptResult"
    interface = "org.kwin_mcp.ScriptResult"
    # Fail fast when the (already unique) name is unavailable instead of
    # stalling for the timeout with the Push going nowhere (issue #24, same
    # shape as the geometry fetch). Callers translate the RuntimeError into
    # their AT-SPI fallback, exactly like a scripting failure.
    owner = bus.request_name(bus_name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    if owner not in (1, 4):
        msg = (
            f"could not acquire D-Bus name {bus_name} for the KWin script "
            f"result (request_name={owner})"
        )
        raise RuntimeError(msg)

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

    resolved = (
        script_text.replace("__BUS_NAME__", bus_name)
        .replace("__OBJECT_PATH__", object_path)
        .replace("__INTERFACE_NAME__", interface)
    )
    script_id = -1
    try:
        with tempfile.TemporaryDirectory(prefix="kwinmcp-script-") as tmpdir:
            script_path = os.path.join(tmpdir, "script.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(resolved)
            script_id = bus.call_blocking(
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting",
                "loadScript",
                "ss",
                [script_path, f"{script_name}-{suffix}"],
            )
            if int(script_id) < 0:
                raise RuntimeError("KWin refused to load script (scripting disabled?)")
            bus.get_object("org.kde.KWin", f"/Scripting/Script{int(script_id)}").run(
                dbus_interface="org.kde.kwin.Script"
            )
            deadline = time.monotonic() + timeout
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
                    [f"{script_name}-{suffix}"],
                )
        except Exception:
            pass
        loop.quit()
        worker.join(timeout=2.0)

    return received.get("payload")


# Backwards-compatible aliases for the template names used in tests/docs.
JS_ACTIVATE_BY_CLASS = _ACTIVATE_SCRIPT_TEMPLATE
JS_LIST_WINDOWS = _LIST_SCRIPT_TEMPLATE


def activate_window_by_name(dbus_address: str, app_name: str) -> str:
    """Activate (compositor-focus) a window whose app/class/title matches.

    Args:
        dbus_address: Session bus address of the KWin instance.
        app_name: Case-insensitive substring matched against the window's
            resourceClass, caption and resourceName.

    Returns:
        Human-readable outcome; errors are returned as strings for the caller
        (AutomationEngine) to translate into tool errors.
    """
    payload = _run_script_one_shot(
        dbus_address,
        _ACTIVATE_SCRIPT_TEMPLATE.replace("__APP_NAME__", app_name.replace('"', "")),
        "kwinmcp-activate",
    )
    return parse_script_result(payload, app_name)


def list_windows_by_script(dbus_address: str) -> str:
    """List all compositor windows via KWin scripting.

    Unlike the AT-SPI enumeration this sees every window the compositor knows
    about, including apps that never registered an accessibility tree (H-2).

    Args:
        dbus_address: Session bus address of the KWin instance.

    Returns:
        Formatted window list; raises RuntimeError on scripting failures.
    """
    payload = _run_script_one_shot(dbus_address, _LIST_SCRIPT_TEMPLATE, "kwinmcp-list")
    if payload is None:
        msg = "KWin script timed out while listing windows"
        raise RuntimeError(msg)
    if payload.startswith("ERROR "):
        msg = f"KWin script error: {payload[6:]}"
        raise RuntimeError(msg)
    if payload == "OK\n":
        return "Applications (0):\n"
    if not payload.startswith("OK\n"):
        msg = f"Unexpected KWin script payload: {payload!r}"
        raise RuntimeError(msg)

    lines = ["Applications:"]
    for line in payload[3:].splitlines():
        fields = line.split("\t", 3)
        if len(fields) != 4:
            continue
        resource_class, _internal_id, caption, extras = fields
        lines.append(f'- {resource_class} "{caption}"{extras}')
    return "\n".join(lines)
