"""Core automation engine for KDE Wayland GUI automation.

Contains all tool logic independent of the MCP transport layer.
Can be used directly from the CLI or wrapped by the MCP server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import dbus

from kwin_mcp import kwin_windows
from kwin_mcp.errors import ToolError, tool_error
from kwin_mcp.input import InputBackend, MouseButton
from kwin_mcp.screenshot import capture_frame_burst, capture_screenshot_to_file
from kwin_mcp.session import LiveSession, Session, SessionConfig

logger = logging.getLogger(__name__)

_DEFAULT_VIRTUAL_SIZE = (1920, 1080)

# kscreen-doctor colourises its output with SGR escape sequences even when
# piped (capture_output), e.g. '\x1b[01;33m\tGeometry: \x1b[0;0m0,0 1746x982' —
# every line must be stripped before any startswith() check (A1).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(line: str) -> str:
    """Strip ANSI escape sequences from one output line."""
    return _ANSI_ESCAPE_RE.sub("", line)


def _parse_kscreen_doctor(output: str) -> tuple[int, int] | None:
    """Return the bounding box of all enabled outputs from ``kscreen-doctor -o``.

    The output is a list of per-output blocks starting with an "Output:" line
    and containing flag lines ("enabled"/"disabled", "priority N") plus a
    "Geometry: x,y WxH" line. Only enabled outputs are considered — a
    disabled output may still print a (stale) Geometry line. The desktop
    size is the axis-aligned bounding box (union) of every enabled output's
    geometry: ``min(x,y) .. max(x+w, y+h)`` — the logical desktop, not a
    single monitor (F3). Mirrored outputs with identical geometry naturally
    collapse into the same rectangle. Sizes are the visible (logical, already
    scaled by the compositor) desktop dimensions.
    """
    boxes: list[tuple[int, int, int, int]] = []  # (x, y, w, h) per enabled output
    enabled = False
    geometry: tuple[int, int, int, int] | None = None

    def finish_block() -> None:
        if enabled and geometry is not None:
            boxes.append(geometry)

    for raw_line in output.splitlines():
        line = _strip_ansi(raw_line).strip()
        if line.startswith("Output:"):
            finish_block()
            enabled = False
            geometry = None
            # Some kscreen-doctor versions put the flags on the "Output:"
            # line itself ("Output: 1 eDP-1 enabled connected priority 1");
            # others print them as indented follow-up lines, either one flag
            # per line or combined ("enabled connected priority 1").
            rest = line[len("Output:") :]
            tokens = rest.split()
            if "enabled" in tokens:
                enabled = True
            elif "disabled" in tokens:
                enabled = False
        elif line.startswith("Geometry:"):
            match = re.search(r"(-?\d+),\s*(-?\d+)\s+(\d+)x(\d+)", line)
            if match:
                geometry = (
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                )
        else:
            # Generic flag line: may hold a bare flag ("enabled"), a combined
            # set ("enabled connected priority 1"), or just a priority.
            tokens = line.split()
            if "enabled" in tokens:
                enabled = True
            elif "disabled" in tokens:
                enabled = False
    finish_block()
    if not boxes:
        return None
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[0] + box[2] for box in boxes)
    y1 = max(box[1] + box[3] for box in boxes)
    return (x1 - x0, y1 - y0)


def _signed_offset(token: str) -> int:
    """Parse an xrandr offset token like ``+0``, ``-1920`` or ``+-1920``.

    xrandr prints negative offsets with a doubled sign (``1920x1080+-1920+0``
    for a monitor left of the origin); ``int()`` would raise ValueError on
    the doubled form.
    """
    return int(token[1:]) if token[0] == "+" and token[1:2] == "-" else int(token)


def _parse_xrandr(output: str) -> tuple[int, int] | None:
    """Return the desktop size from ``xrandr`` output.

    The desktop size is the axis-aligned bounding box (union) of all
    connected monitors' geometries (``WxH+X+Y``); disconnected monitors
    contribute nothing — that union IS the logical desktop (F3). The
    "Screen 0: ... current W x H" line (the whole framebuffer) is only a
    fallback for outputs without usable monitor lines.
    """
    boxes: list[tuple[int, int, int, int]] = []  # (w, h, x, y) per connected monitor
    current: tuple[int, int] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if current is None and line.startswith("Screen ") and "current " in line:
            # xrandr renders the current size with spaces around 'x'.
            match = re.search(r"current\s+(\d+)\s*x\s*(\d+)", line)
            if match:
                current = (int(match.group(1)), int(match.group(2)))
        if re.search(r"\bconnected\b", line) and not re.search(r"\bdisconnected\b", line):
            match = re.search(r"\b(\d+)x(\d+)\s*([+-]-?\d+)\s*([+-]-?\d+)\b", line)
            if match:
                boxes.append(
                    (
                        int(match.group(1)),
                        int(match.group(2)),
                        _signed_offset(match.group(3)),
                        _signed_offset(match.group(4)),
                    )
                )
    if boxes:
        x0 = min(box[2] for box in boxes)
        y0 = min(box[3] for box in boxes)
        x1 = max(box[2] + box[0] for box in boxes)
        y1 = max(box[3] + box[1] for box in boxes)
        return (x1 - x0, y1 - y0)
    return current


def _detect_physical_screen_size() -> tuple[int, int]:
    """Detect the visible desktop resolution of the current session.

    Uses kscreen-doctor (KDE) first, falling back to xrandr for X11 sessions.
    Returns the (width, height) of the bounding box of the logical desktop —
    the union of enabled outputs' (kscreen-doctor) or connected monitors'
    (xrandr) geometries, i.e. the visible (logical) desktop size as the
    compositor scales it, not a single panel's pixel size — or the default
    size when detection fails. Called at every session_start so a changed
    desktop size is picked up by the next virtual session (adopted from
    01SW/kwin-mcp).
    """
    # kscreen-doctor: Geometry lines of enabled outputs, e.g. "Geometry: 0,0 1920x1080"
    if shutil.which("kscreen-doctor"):
        try:
            result = subprocess.run(
                ["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=5
            )
            size = _parse_kscreen_doctor(result.stdout)
            if size is not None:
                return size
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    # xrandr: connected monitors, e.g. "DP-2 connected primary 1920x1080+0+0"
    if shutil.which("xrandr"):
        try:
            result = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=5)
            size = _parse_xrandr(result.stdout)
            if size is not None:
                return size
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    return _DEFAULT_VIRTUAL_SIZE


# Install hints for external binaries
_INSTALL_HINTS: dict[str, str] = {
    "wl-paste": (
        "wl-paste not found. Install wl-clipboard "
        "(e.g. 'sudo pacman -S wl-clipboard' or 'sudo apt install wl-clipboard')."
    ),
    "wl-copy": (
        "wl-copy not found. Install wl-clipboard "
        "(e.g. 'sudo pacman -S wl-clipboard' or 'sudo apt install wl-clipboard')."
    ),
    "wtype": (
        "wtype not found. Install wtype "
        "(e.g. 'sudo pacman -S wtype' or build from https://github.com/atx/wtype)."
    ),
    "dbus-send": (
        "dbus-send not found. Install dbus (e.g. 'sudo pacman -S dbus' or 'sudo apt install dbus')."
    ),
    "spectacle": (
        "spectacle not found. Install spectacle "
        "(e.g. 'sudo pacman -S spectacle' or 'sudo apt install kde-spectacle')."
    ),
    "wayland-info": (
        "wayland-info not found. Install wayland-utils "
        "(e.g. 'sudo pacman -S wayland-utils' or 'sudo apt install wayland-utils')."
    ),
}


def clip_to_screen(x: int, y: int, screen_size: tuple[int, int] | None) -> tuple[int, int, bool]:
    """Clip coordinates to the known screen bounds.

    Args:
        x, y: Requested coordinates.
        screen_size: Known (width, height) of the virtual screen, or None when
            the size is unknown (live sessions): only the lower bound (>= 0)
            is enforced and no upper-bound clipping happens.

    Returns:
        (clipped_x, clipped_y, clipped) where clipped is True when either
        coordinate was adjusted.
    """
    cx = max(0, x)
    cy = max(0, y)
    if screen_size is not None:
        cx = min(cx, screen_size[0] - 1)
        cy = min(cy, screen_size[1] - 1)
    return cx, cy, (cx != x or cy != y)


def _parse_mouse_button(button: str) -> MouseButton:
    """Resolve a button name to MouseButton; invalid names are a ToolError."""
    try:
        return MouseButton(button)
    except ValueError:
        tool_error(f"Invalid button {button!r}: expected 'left', 'right', or 'middle'")


class AutomationEngine:
    """Core automation engine encapsulating all tool logic.

    Manages session lifecycle, input injection, screenshot capture,
    accessibility queries, and clipboard operations.
    """

    def __init__(self) -> None:
        self._session: Session | LiveSession | None = None
        self._input: InputBackend | None = None
        self._clipboard_enabled: bool = False
        self._wl_copy_proc: subprocess.Popen[bytes] | None = None
        self._keep_screenshots: bool = False
        self._screen_size: tuple[int, int] | None = None

    # ── Private helpers ───────────────────────────────────────────────────

    def _get_session(self) -> Session | LiveSession:
        if self._session is None or not self._session.is_running:
            # Anticipated failure → ToolError so the client sees the message
            # (isError=True) instead of a swallowed crash (H-3/H-4).
            tool_error("No active session. Call session_start or session_connect first.")
            raise AssertionError  # unreachable, satisfies type checkers
        return self._session

    def _get_input(self) -> InputBackend:
        if self._input is None:
            tool_error("No input backend. Call session_start or session_connect first.")
            raise AssertionError  # unreachable, satisfies type checkers
        return self._input

    def _session_env(self) -> dict[str, str]:
        """Build environment dict for tools that need the isolated session."""
        session = self._get_session()
        env: dict[str, str] = {**os.environ}
        info = session.info
        if info:
            if info.dbus_address:
                env["DBUS_SESSION_BUS_ADDRESS"] = info.dbus_address
            env["WAYLAND_DISPLAY"] = info.wayland_socket
            if info.home_dir:
                home = str(info.home_dir)
                env["HOME"] = home
                env["XDG_CONFIG_HOME"] = str(info.home_dir / ".config")
                env["XDG_DATA_HOME"] = str(info.home_dir / ".local" / "share")
                env["XDG_CACHE_HOME"] = str(info.home_dir / ".cache")
                env["XDG_STATE_HOME"] = str(info.home_dir / ".local" / "state")
        env["QT_QPA_PLATFORM"] = "wayland"
        env.pop("DISPLAY", None)
        return env

    def _run_atspi(self, op: str, **kwargs: object) -> dict:
        """Run an AT-SPI2 query in a subprocess with the isolated session's D-Bus address.

        The gi.repository.Atspi library caches the D-Bus connection process-wide,
        so we must run queries in a fresh subprocess that inherits the correct
        DBUS_SESSION_BUS_ADDRESS from the isolated dbus-run-session.

        Retries once on failure to handle transient AT-SPI2 bus instability.
        """
        env = self._session_env()
        payload = json.dumps({"op": op, **kwargs})

        last_error = ""
        for attempt in range(2):
            if attempt > 0:
                time.sleep(0.5)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "kwin_mcp.accessibility"],
                    input=payload,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                last_error = f"AT-SPI2 query timed out after 30s (op={op})"
                continue

            if result.returncode != 0:
                last_error = f"AT-SPI2 query failed (exit {result.returncode}): {result.stderr}"
                continue

            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = f"AT-SPI2 query returned invalid JSON: {result.stdout[:200]}"
                continue

        msg = f"{last_error}. Retried once but still failed — the AT-SPI2 bus may be unstable."
        raise ToolError(msg)

    def _with_frame_capture(
        self,
        action_result: str,
        screenshot_after_ms: list[int] | None,
    ) -> str:
        """Append frame captures to an action result if requested."""
        if not screenshot_after_ms:
            return action_result

        session = self._get_session()
        info = session.info
        if info is None:
            return action_result

        frames = capture_frame_burst(
            dbus_address=info.dbus_address,
            output_dir=info.screenshot_dir,
            delays_ms=screenshot_after_ms,
            wayland_socket=info.wayland_socket,
        )

        lines = [action_result, f"Captured {len(frames)} frames:"]
        for delay_ms, path in zip(sorted(screenshot_after_ms), frames, strict=True):
            size_kb = path.stat().st_size / 1024
            lines.append(f"  {delay_ms}ms: {path} ({size_kb:.1f} KB)")
        return "\n".join(lines)

    # ── Session management ────────────────────────────────────────────────

    def session_start(
        self,
        app_command: str = "",
        screen_width: int = 0,
        screen_height: int = 0,
        enable_clipboard: bool = False,
        keep_screenshots: bool = False,
        isolate_home: bool = False,
        keep_home: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start an isolated KWin Wayland session, optionally launching an app."""
        if self._session is not None and self._session.is_running:
            tool_error("Session already running. Call session_stop first.")

        # Auto-detect the visible desktop size when not explicitly requested.
        # 0 in either dimension means "match the current desktop": both
        # dimensions are resolved together from detection, so a partially
        # zero request (e.g. 0x720) does not mix a detected width with a
        # caller height into a nonsensical aspect ratio. Detection runs at
        # every call so a changed desktop size is applied to the next
        # virtual session (adopted from 01SW/kwin-mcp).
        if screen_width <= 0 or screen_height <= 0:
            screen_width, screen_height = _detect_physical_screen_size()

        self._clipboard_enabled = enable_clipboard
        self._screen_size = (screen_width, screen_height)

        self._session = Session()
        config = SessionConfig(
            screen_width=screen_width,
            screen_height=screen_height,
            enable_clipboard=enable_clipboard,
            keep_screenshots=keep_screenshots,
            isolate_home=isolate_home,
            keep_home=keep_home,
        )
        info = self._session.start(config)

        result = f"Session started. Wayland socket: {info.wayland_socket}"
        if info.home_dir:
            result += f"\nIsolated home: {info.home_dir}"

        if app_command:
            cmd = shlex.split(app_command)
            app_info = self._session.launch_app(cmd, extra_env=env)
            result += f"\nApp launched: {app_command} (PID={app_info.pid})"
            result += f"\nApp log: {app_info.log_path}"

        # Set up input backend via KWin's EIS D-Bus interface
        time.sleep(0.5)
        try:
            self._input = InputBackend(info.dbus_address)
        except (RuntimeError, ToolError) as exc:
            # ToolError, not just RuntimeError (F5): a partial EIS handshake
            # (device never resumed) surfaces as ToolError from
            # _negotiate_devices, and ToolError does not inherit RuntimeError
            # — catching only RuntimeError killed session_start after the
            # session was already up, breaking the degrade-to-no-input
            # contract (screenshot/accessibility tools still work without
            # input).
            logger.warning(
                "KWin EIS input backend unavailable, degrading to no input backend: %s", exc
            )
            self._input = None

        input_status = "Input backend: KWin EIS" if self._input else "No input backend available"
        result += f"\n{input_status}"

        return result

    def session_connect(
        self,
        dbus_address: str = "",
        wayland_display: str = "",
        keep_screenshots: bool = False,
    ) -> str:
        """Connect to an existing KWin session (e.g. the real desktop)."""
        if self._session is not None and self._session.is_running:
            tool_error("Session already running. Call session_stop first.")

        dbus_addr = dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        wayland_disp = wayland_display or os.environ.get("WAYLAND_DISPLAY", "")

        if not dbus_addr:
            tool_error(
                "No D-Bus address available. Provide dbus_address parameter "
                "or ensure $DBUS_SESSION_BUS_ADDRESS is set."
            )
        if not wayland_disp:
            tool_error(
                "No Wayland display available. Provide wayland_display parameter "
                "or ensure $WAYLAND_DISPLAY is set."
            )

        # Validate KWin is reachable on the given D-Bus
        import dbus as dbus_module
        import dbus.bus

        try:
            bus = dbus.bus.BusConnection(dbus_addr)
            bus.get_object("org.kde.KWin", "/org/kde/KWin")
        except dbus_module.DBusException as exc:
            tool_error(f"Cannot reach KWin on D-Bus ({dbus_addr}): {exc}")

        screenshot_dir = Path(tempfile.mkdtemp(prefix="kwin-mcp-screenshots-"))

        session = LiveSession(dbus_addr, wayland_disp, screenshot_dir)
        session._keep_screenshots = keep_screenshots
        self._session = session
        self._keep_screenshots = keep_screenshots
        self._screen_size = None  # live sessions: unknown size, lower bound only

        # Clipboard is always available on live sessions
        self._clipboard_enabled = True

        result = f"Connected to live KWin session. D-Bus: {dbus_addr}, Wayland: {wayland_disp}"

        # Set up input backend — EIS first, ydotool fallback
        time.sleep(0.3)
        try:
            self._input = InputBackend(dbus_addr)
            result += "\nInput backend: KWin EIS"
        except (RuntimeError, ToolError) as exc:
            # ToolError, not just RuntimeError (F5): a partial EIS handshake
            # raises ToolError from _negotiate_devices; without it in the
            # tuple the failure escaped session_connect instead of degrading
            # to the ydotool/no-input fallback.
            logger.warning(
                "KWin EIS input backend unavailable, falling back to ydotool if present: %s", exc
            )
            self._input = None
            if shutil.which("ydotool"):
                result += "\nInput backend: ydotool (EIS unavailable)"
            else:
                result += (
                    "\nNo input backend available (EIS connection failed and ydotool not found). "
                    "Screenshot and accessibility tools still work."
                )

        return result

    def session_stop(self) -> str:
        """Stop the current session and clean up."""
        if self._session is None:
            return "No session running."

        # Clean up wl-copy process if active
        if self._wl_copy_proc is not None:
            self._wl_copy_proc.terminate()
            try:
                self._wl_copy_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._wl_copy_proc.kill()
            self._wl_copy_proc = None
        self._clipboard_enabled = False

        if self._input is not None:
            self._input.close()

        is_live = isinstance(self._session, LiveSession)
        if isinstance(self._session, LiveSession):
            self._session.stop(keep_screenshots=self._keep_screenshots)
        else:
            self._session.stop()
        self._session = None
        self._input = None
        self._keep_screenshots = False
        self._screen_size = None

        return "Disconnected from live session." if is_live else "Session stopped."

    # ── Screenshot / Accessibility ────────────────────────────────────────

    def screenshot(self, include_cursor: bool = False) -> str:
        """Capture a screenshot of the isolated session."""
        session = self._get_session()
        info = session.info
        if info is None:
            msg = "No session info available"
            raise RuntimeError(msg)

        path = capture_screenshot_to_file(
            dbus_address=info.dbus_address,
            wayland_socket=info.wayland_socket,
            include_cursor=include_cursor,
            output_dir=info.screenshot_dir,
        )
        size_kb = path.stat().st_size / 1024
        return f"Screenshot saved: {path} ({size_kb:.1f} KB)"

    def accessibility_tree(self, app_name: str = "", max_depth: int = 15, role: str = "") -> str:
        """Get the accessibility tree of apps in the isolated session."""
        self._get_session()
        resp = self._run_atspi("tree", app_name=app_name, max_depth=max_depth, role=role)
        return resp["result"]

    def find_ui_elements(
        self, query: str, app_name: str = "", states: list[str] | None = None
    ) -> str:
        """Find UI elements matching a search query and/or required states."""
        self._get_session()
        resp = self._run_atspi("find", query=query, app_name=app_name, states=states)
        elements = resp["result"]

        # Build descriptive search summary
        criteria: list[str] = []
        if query:
            criteria.append(f"query='{query}'")
        if states:
            criteria.append(f"states={states}")
        search_desc = ", ".join(criteria) if criteria else "(all)"

        if not elements:
            return f"No elements found matching {search_desc}"

        lines = [f"Found {len(elements)} elements matching {search_desc}:\n"]
        for el in elements:
            actions_str = f" [actions: {', '.join(el['actions'])}]" if el["actions"] else ""
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"@ ({el['x']}, {el['y']}, {el['width']}x{el['height']}){actions_str}"
            )
        return "\n".join(lines)

    # ── Mouse tools ───────────────────────────────────────────────────────

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        triple: bool = False,
        modifiers: list[str] | None = None,
        hold_ms: int = 0,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Click at coordinates in the isolated session."""
        inp = self._get_input()
        x, y, clipped = clip_to_screen(x, y, self._screen_size)
        btn = _parse_mouse_button(button)
        click_count = 3 if triple else (2 if double else 1)
        inp.mouse_click(x, y, btn, click_count=click_count, modifiers=modifiers, hold_ms=hold_ms)

        desc = f"Clicked {button} at ({x}, {y})"
        if clipped:
            desc += " (clipped to screen bounds)"
        if triple:
            desc += " (triple)"
        elif double:
            desc += " (double)"
        if modifiers:
            desc += f" with {'+'.join(modifiers)}"
        if hold_ms > 0:
            desc += f" held {hold_ms}ms"

        return self._with_frame_capture(desc, screenshot_after_ms)

    def mouse_move(
        self,
        x: int,
        y: int,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Move the mouse cursor to coordinates without clicking."""
        inp = self._get_input()
        x, y, clipped = clip_to_screen(x, y, self._screen_size)
        inp.mouse_move(x, y)
        result = f"Mouse moved to ({x}, {y})"
        if clipped:
            result += " (clipped to screen bounds)"
        return self._with_frame_capture(result, screenshot_after_ms)

    def mouse_scroll(
        self,
        x: int,
        y: int,
        delta: int,
        horizontal: bool = False,
        discrete: bool = False,
        steps: int = 1,
    ) -> str:
        """Scroll at coordinates in the isolated session."""
        inp = self._get_input()
        x, y, clipped = clip_to_screen(x, y, self._screen_size)
        inp.mouse_scroll(x, y, delta, horizontal=horizontal, discrete=discrete, steps=steps)
        direction = "horizontal" if horizontal else "vertical"
        mode = "discrete" if discrete else "smooth"
        desc = f"Scrolled {direction} ({mode}) by {delta} at ({x}, {y})"
        if clipped:
            desc += " (clipped to screen bounds)"
        if steps > 1:
            desc += f" in {steps} steps"
        return desc

    def mouse_drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        button: str = "left",
        modifiers: list[str] | None = None,
        waypoints: list[list[int]] | None = None,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Drag from one point to another in the isolated session."""
        inp = self._get_input()
        btn = _parse_mouse_button(button)
        wp: list[tuple[int, int, int]] | None = None
        if waypoints:
            wp = [(w[0], w[1], w[2]) for w in waypoints]
        inp.mouse_drag(from_x, from_y, to_x, to_y, button=btn, modifiers=modifiers, waypoints=wp)

        desc = f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        if modifiers:
            desc += f" with {'+'.join(modifiers)}"
        if waypoints:
            desc += f" via {len(waypoints)} waypoints"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def mouse_button_down(
        self,
        x: int,
        y: int,
        button: str = "left",
    ) -> str:
        """Press a mouse button at coordinates without releasing."""
        inp = self._get_input()
        inp.mouse_button_down(x, y, _parse_mouse_button(button))
        return f"Button {button} pressed at ({x}, {y})"

    def mouse_button_up(
        self,
        x: int,
        y: int,
        button: str = "left",
    ) -> str:
        """Release a mouse button at coordinates."""
        inp = self._get_input()
        inp.mouse_button_up(x, y, _parse_mouse_button(button))
        return f"Button {button} released at ({x}, {y})"

    # ── Keyboard tools ────────────────────────────────────────────────────

    def keyboard_type(
        self,
        text: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Type ASCII text into the currently focused element."""
        inp = self._get_input()
        inp.keyboard_type(text)
        result = f"Typed: {text!r}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_type_unicode(
        self,
        text: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Type arbitrary Unicode text including non-ASCII characters."""
        if not shutil.which("wtype") and not shutil.which("wl-copy"):
            tool_error(
                "Neither wtype nor wl-copy found. Install at least one: "
                "wtype (e.g. 'sudo pacman -S wtype') or "
                "wl-clipboard (e.g. 'sudo pacman -S wl-clipboard')."
            )
        inp = self._get_input()
        # _session_env() carries WAYLAND_DISPLAY + XDG_RUNTIME_DIR so wtype and
        # wl-copy connect to the isolated compositor, not the host (H-1 fix).
        ok = inp.keyboard_type_unicode(text, env=self._session_env())
        if not ok:
            tool_error(f"Failed to type unicode text {text!r} (wtype and clipboard both failed)")
        result = f"Typed unicode: {text!r}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_key(
        self,
        key: str,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Press and release a key or key combination."""
        inp = self._get_input()
        inp.keyboard_key(key)
        result = f"Pressed: {key}"
        return self._with_frame_capture(result, screenshot_after_ms)

    def keyboard_key_down(self, key: str) -> str:
        """Press and hold a key without releasing."""
        inp = self._get_input()
        inp.keyboard_key_down(key)
        return f"Key down: {key}"

    def keyboard_key_up(self, key: str) -> str:
        """Release a previously held key."""
        inp = self._get_input()
        inp.keyboard_key_up(key)
        return f"Key up: {key}"

    # ── Touch tools ───────────────────────────────────────────────────────

    def touch_tap(
        self,
        x: int,
        y: int,
        hold_ms: int = 0,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Tap at coordinates using touch input."""
        inp = self._get_input()
        x, y, clipped = clip_to_screen(x, y, self._screen_size)
        inp.touch_tap(x, y, hold_ms=hold_ms)
        desc = f"Touch tap at ({x}, {y})"
        if clipped:
            desc += " (clipped to screen bounds)"
        if hold_ms > 0:
            desc += f" held {hold_ms}ms"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        duration_ms: int = 300,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Swipe from one point to another using single-finger touch input."""
        inp = self._get_input()
        from_x, from_y, clipped = clip_to_screen(from_x, from_y, self._screen_size)
        to_x, to_y, clipped2 = clip_to_screen(to_x, to_y, self._screen_size)
        inp.touch_swipe(from_x, from_y, to_x, to_y, duration_ms=duration_ms)
        desc = f"Touch swipe from ({from_x}, {from_y}) to ({to_x}, {to_y}) in {duration_ms}ms"
        if clipped or clipped2:
            desc += " (clipped to screen bounds)"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_pinch(
        self,
        center_x: int,
        center_y: int,
        start_distance: int,
        end_distance: int,
        duration_ms: int = 500,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Perform a two-finger pinch gesture."""
        inp = self._get_input()
        inp.touch_pinch(center_x, center_y, start_distance, end_distance, duration_ms=duration_ms)
        direction = "in" if end_distance < start_distance else "out"
        desc = f"Pinch {direction} at ({center_x}, {center_y}): {start_distance}→{end_distance}px"
        return self._with_frame_capture(desc, screenshot_after_ms)

    def touch_multi_swipe(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        fingers: int = 3,
        duration_ms: int = 300,
        screenshot_after_ms: list[int] | None = None,
    ) -> str:
        """Perform a multi-finger swipe gesture."""
        inp = self._get_input()
        inp.touch_multi_swipe(from_x, from_y, to_x, to_y, fingers=fingers, duration_ms=duration_ms)
        desc = (
            f"{fingers}-finger swipe from ({from_x}, {from_y}) "
            f"to ({to_x}, {to_y}) in {duration_ms}ms"
        )
        return self._with_frame_capture(desc, screenshot_after_ms)

    # ── Clipboard tools ───────────────────────────────────────────────────

    def clipboard_get(self) -> str:
        """Read the current clipboard content in the isolated session."""
        if not self._clipboard_enabled:
            tool_error(
                "Clipboard not enabled. Pass enable_clipboard=True to session_start, "
                "or use session_connect (clipboard is always enabled for live sessions)."
            )

        env = self._session_env()
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                env=env,
                capture_output=True,
                timeout=5,
            )
        except FileNotFoundError:
            tool_error(_INSTALL_HINTS["wl-paste"])
        if result.returncode != 0:
            tool_error(f"Failed to read clipboard: {result.stderr.decode(errors='replace')}")
        return result.stdout.decode(errors="replace")

    def clipboard_set(self, text: str) -> str:
        """Set the clipboard content in the isolated session."""
        if not self._clipboard_enabled:
            tool_error(
                "Clipboard not enabled. Pass enable_clipboard=True to session_start, "
                "or use session_connect (clipboard is always enabled for live sessions)."
            )

        # Terminate previous wl-copy process (replaced by new content)
        if self._wl_copy_proc is not None:
            self._wl_copy_proc.terminate()
            try:
                self._wl_copy_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._wl_copy_proc.kill()
            self._wl_copy_proc = None

        env = self._session_env()
        try:
            self._wl_copy_proc = subprocess.Popen(
                ["wl-copy", "--", text],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            tool_error(_INSTALL_HINTS["wl-copy"])
        time.sleep(0.1)  # Wait for fork to complete
        return f"Clipboard set: {text!r}"

    # ── Wait-for-UI tools ─────────────────────────────────────────────────

    def wait_for_element(
        self,
        query: str,
        app_name: str = "",
        timeout_ms: int = 5000,
        poll_interval_ms: int = 200,
        expected_states: list[str] | None = None,
    ) -> str:
        """Wait for a UI element to appear in the accessibility tree.

        Error contract (H-3/H-4): a timeout is NOT a crash — it is a
        legitimate negative result and is returned as a normal response
        (isError=False) whose text starts with the canonical "TIMEOUT after
        Nms" marker so agents can branch on it. Hard failures (no session,
        AT-SPI bus down) raise ToolError instead.
        """
        self._get_session()
        resp = self._run_atspi(
            "wait",
            query=query,
            app_name=app_name,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
            states=expected_states,
        )
        if not resp["ok"]:
            # The AT-SPI worker raises TimeoutError for a failed wait: that is
            # the negative-result path, normalised to the TIMEOUT contract.
            return resp["error"].replace("Timeout after", "TIMEOUT after", 1)

        elements = resp["result"]

        # Build descriptive search summary
        criteria: list[str] = []
        if query:
            criteria.append(f"query='{query}'")
        if expected_states:
            criteria.append(f"states={expected_states}")
        search_desc = ", ".join(criteria) if criteria else "(all)"

        lines = [f"Found {len(elements)} elements matching {search_desc}:\n"]
        for el in elements:
            actions_str = f" [actions: {', '.join(el['actions'])}]" if el["actions"] else ""
            lines.append(
                f'- [{el["role"]}] "{el["name"]}" '
                f"@ ({el['x']}, {el['y']}, {el['width']}x{el['height']}){actions_str}"
            )
        return "\n".join(lines)

    # ── Window management tools ───────────────────────────────────────────

    def launch_app(self, command: str, env: dict[str, str] | None = None) -> str:
        """Launch an application inside the running isolated session."""
        session = self._get_session()
        cmd = shlex.split(command)
        app_info = session.launch_app(cmd, extra_env=env)
        return f"App launched: {command} (PID={app_info.pid})\nApp log: {app_info.log_path}"

    def list_windows(self) -> str:
        """List accessible application windows in the isolated session."""
        session = self._get_session()
        info = session.info
        # Prefer the compositor-side enumeration (sees every window KWin
        # knows, including apps without an accessibility tree — H-2); fall
        # back to AT-SPI when scripting is unavailable.
        if info and info.dbus_address:
            try:
                return kwin_windows.list_windows_by_script(info.dbus_address)
            except (RuntimeError, dbus.DBusException):
                pass
        self._get_session()
        resp = self._run_atspi("list_windows")
        return resp["result"]

    def focus_window(self, app_name: str) -> str:
        """Focus a window by application name.

        Activation goes through the KWin scripting API
        (workspace.activeWindow = w) because AT-SPI grabFocus() does not move
        compositor-level focus on Wayland (A-2). Falls back to the AT-SPI
        path when scripting is unavailable.
        """
        session = self._get_session()
        info = session.info
        if info and info.dbus_address:
            try:
                return kwin_windows.activate_window_by_name(info.dbus_address, app_name)
            except (RuntimeError, dbus.DBusException) as exc:
                last_error = str(exc)
        else:
            last_error = "session has no D-Bus address"
        resp = self._run_atspi("focus_window", app_name=app_name)
        result = resp["result"]
        if "Focused" in result:
            note = (
                " (AT-SPI fallback: KWin scripting failed"
                f" — {last_error}; focus may not have moved)"
            )
            return result + note
        return result

    # ── D-Bus tools ───────────────────────────────────────────────────────

    def dbus_call(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        args: list[str] | None = None,
    ) -> str:
        """Call a D-Bus method in the isolated session using dbus-send."""
        env = self._session_env()
        cmd = [
            "dbus-send",
            "--session",
            "--print-reply",
            f"--dest={service}",
            f"{path}",
            f"{interface}.{method}",
        ]
        if args:
            cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError:
            # Anticipated failure → ToolError so the client sees the message
            # (isError=True) instead of a success-payload string (N2).
            tool_error(_INSTALL_HINTS["dbus-send"])
        if result.returncode != 0:
            # Anticipated failure (ServiceUnknown, UnknownMethod, ...) →
            # ToolError (isError=True), matching the read_app_log contract
            # (N2: these used to be returned as success strings).
            tool_error(f"D-Bus call failed: {result.stderr.decode(errors='replace')}")
        return result.stdout.decode(errors="replace")

    def read_app_log(self, pid: int, last_n_lines: int = 50) -> str:
        """Read stdout/stderr output of a launched app."""
        session = self._get_session()
        try:
            return session.read_app_log(pid, last_n_lines=last_n_lines)
        except ValueError as exc:
            # Unknown PID is an anticipated failure → ToolError with the
            # available-PIDs detail (H-3: no longer swallowed).
            tool_error(str(exc))

    def wayland_info(self, filter_protocol: str = "") -> str:
        """List Wayland protocols available in the isolated session."""
        env = self._session_env()
        try:
            result = subprocess.run(
                ["wayland-info"],
                env=env,
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError:
            return _INSTALL_HINTS["wayland-info"]
        if result.returncode != 0:
            return f"wayland-info failed: {result.stderr.decode(errors='replace')}"

        output = result.stdout.decode(errors="replace")
        if filter_protocol:
            lines = [line for line in output.splitlines() if filter_protocol in line]
            if not lines:
                return f"No protocols matching '{filter_protocol}' found."
            return "\n".join(lines)
        return output
