"""Tests for bounded session startup and host DISPLAY isolation.

Adopted from upstream isac322/kwin-mcp PR #50:

- ``session_start`` hung forever when the KWin wrapper never printed READY
  (dead KWin, missing binaries): the parent's stdout read loop had no
  deadline. ``_read_startup_lines`` now bounds the read (~25s) and also
  recognizes the wrapper's ``NOSOCKET`` marker.
- The wrapper's Wayland-socket wait was an unbounded loop; it is now bounded
  (150 x 0.1s = 15s), reports ``NOSOCKET`` and exits 1 so the parent fails
  fast with a clear error instead of hanging.
- ``launch_app`` inherited the host ``DISPLAY``, so X11 applications in an
  isolated virtual session silently opened on the user's real desktop.
- Startup failures now include KWin's stderr (pattern from upstream PR #42):
  the old code read stderr after ``stop()``, which had already cleared
  ``self._process``, so the message was always empty.
- The hardcoded ``/usr/lib/at-spi-bus-launcher`` wrapper path (Arch-only)
  silently no-oped on Debian/Ubuntu/Fedora (``/usr/libexec/...``), leaving a
  dead accessibility bus: the launcher is now resolved on the Python side
  before the wrapper is assembled (upstream PR #42, fix c).
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast

import kwin_mcp.core as core_module
import kwin_mcp.session as session_module
from kwin_mcp.core import AutomationEngine
from kwin_mcp.errors import ToolError
from kwin_mcp.session import Session, SessionConfig, SessionInfo


def _fake_process(data: bytes) -> subprocess.Popen[bytes]:
    """A Popen stand-in whose stdout yields ``data`` then EOF, still alive.

    ``_read_startup_lines`` only touches ``stdout`` (iteration) and ``poll()``,
    so a lightweight stub typed as Popen[bytes] keeps the test honest about
    the surface it exercises.
    """
    stub = SimpleNamespace(stdout=io.BytesIO(data), poll=lambda: None)
    return cast("subprocess.Popen[bytes]", stub)


def test_read_startup_lines_parses_ready() -> None:
    """The bounded reader returns the D-Bus address and the READY flag."""
    proc = _fake_process(b"DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-x\nREADY\n")
    dbus_address, got_ready = Session._read_startup_lines(proc, timeout=2.0)
    assert dbus_address == "unix:path=/tmp/dbus-x"
    assert got_ready is True


def test_read_startup_lines_stops_on_nosocket() -> None:
    """The wrapper's NOSOCKET marker ends the read without READY."""
    proc = _fake_process(b"DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/x\nNOSOCKET\n")
    dbus_address, got_ready = Session._read_startup_lines(proc, timeout=2.0)
    assert dbus_address == "unix:path=/tmp/x"
    assert got_ready is False


def test_read_startup_lines_eof_without_ready() -> None:
    """A wrapper that dies before READY yields neither address nor READY."""
    proc = _fake_process(b"partial output\n")
    dbus_address, got_ready = Session._read_startup_lines(proc, timeout=2.0)
    assert dbus_address == ""
    assert got_ready is False


def test_read_startup_lines_ignores_noise() -> None:
    """Lines from D-Bus activation between address and READY are ignored."""
    data = b"DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-y\ndbus-daemon noise\nREADY\n"
    proc = _fake_process(data)
    dbus_address, got_ready = Session._read_startup_lines(proc, timeout=2.0)
    assert dbus_address == "unix:path=/tmp/dbus-y"
    assert got_ready is True


def test_wrapper_script_bounded_socket_wait_and_display_unset() -> None:
    """The wrapper waits for the KWin socket with a deadline, unsets DISPLAY
    for kwin_wayland, and reports NOSOCKET + exit 1 on timeout."""
    session = Session()
    session._socket_name = "wayland-mcp-test"
    script = session._build_wrapper_script(SessionConfig())
    assert "NOSOCKET" in script
    assert "seq 1 150" in script
    assert "exit 1" in script
    assert "-u DISPLAY" in script


def test_at_spi_launcher_prefers_existing_candidate(monkeypatch, tmp_path) -> None:
    """The first existing candidate wins: on Debian/Ubuntu/Fedora the launcher
    lives in /usr/libexec, and that path must land in the wrapper."""
    libexec = tmp_path / "libexec-launcher"
    libexec.write_text("")
    monkeypatch.setattr(
        session_module,
        "_AT_SPI_LAUNCHER_CANDIDATES",
        (str(libexec), "/nonexistent/a", "/nonexistent/b"),
    )
    monkeypatch.setattr(session_module.shutil, "which", lambda name: "/from-which/launcher")
    assert session_module._at_spi_bus_launcher() == str(libexec)


def test_at_spi_launcher_falls_back_to_which(monkeypatch) -> None:
    """No candidate exists → shutil.which, then the Arch default as last resort."""
    monkeypatch.setattr(
        session_module, "_AT_SPI_LAUNCHER_CANDIDATES", ("/nonexistent/a", "/nonexistent/b")
    )
    monkeypatch.setattr(
        session_module.shutil,
        "which",
        lambda name: "/usr/local/bin/at-spi-bus-launcher",
    )
    assert session_module._at_spi_bus_launcher() == "/usr/local/bin/at-spi-bus-launcher"

    monkeypatch.setattr(session_module.shutil, "which", lambda name: None)
    assert session_module._at_spi_bus_launcher() == "/usr/lib/at-spi-bus-launcher"


def test_wrapper_script_contains_resolved_launcher(monkeypatch) -> None:
    """The wrapper embeds the resolved launcher path, not a hardcoded one, and
    shell-quotes it so paths with special characters stay one argument."""
    monkeypatch.setattr(session_module, "_at_spi_bus_launcher", lambda: "/resolved/launcher")
    session = Session()
    session._socket_name = "wayland-mcp-test"
    script = session._build_wrapper_script(SessionConfig())
    # shlex.quote leaves a plain path untouched (no spurious quotes).
    assert "/resolved/launcher --launch-immediately" in script
    assert "/usr/lib/at-spi-bus-launcher" not in script

    monkeypatch.setattr(session_module, "_at_spi_bus_launcher", lambda: "/opt/my tools/launcher")
    quoted = session._build_wrapper_script(SessionConfig())
    assert "'/opt/my tools/launcher' --launch-immediately" in quoted


def test_launch_app_strips_host_display(monkeypatch, tmp_path) -> None:
    """launch_app removes the host DISPLAY from the app environment so X11
    apps cannot silently open on the user's real desktop."""
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0-host")

    captured: dict[str, object] = {}

    def fake_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        captured["command"] = args[0] if args else kwargs.get("args")
        captured["env"] = kwargs.get("env")
        stub = SimpleNamespace(pid=4242, poll=lambda: None)
        return cast("subprocess.Popen[bytes]", stub)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    session = Session()
    session._socket_name = "wayland-mcp-test"
    session._process = cast("subprocess.Popen[bytes] | None", SimpleNamespace(poll=lambda: None))
    session._config = SessionConfig()
    session._info = SessionInfo(
        dbus_address="unix:path=/tmp/dbus",
        wayland_socket="wayland-mcp-test",
        kwin_pid=1,
        screenshot_dir=tmp_path,
    )

    session.launch_app(["kcalc"])

    env = captured["env"]
    assert isinstance(env, dict)
    assert "DISPLAY" not in env
    assert env["WAYLAND_DISPLAY"] == "wayland-mcp-test"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus"


def test_session_start_degrades_when_input_backend_raises_tool_error(
    monkeypatch, tmp_path, caplog
) -> None:
    """InputBackend raising ToolError degrades to no input backend.

    Regression for F5: a partial EIS handshake raises ToolError from
    ``_negotiate_devices`` (tool_error). ``session_start`` only caught
    RuntimeError, and ToolError does not inherit RuntimeError, so the
    degradation contract of the tool result ("Input backend: ..." status
    line) was violated by a hard crash of session_start after the session
    was already up. Screenshot and accessibility tools still work without
    input; losing them over a broken handshake is wrong.
    """
    info = SessionInfo(
        dbus_address="unix:path=/tmp/dbus",
        wayland_socket="wayland-mcp-test",
        kwin_pid=1,
        screenshot_dir=tmp_path,
    )
    monkeypatch.setattr(session_module.Session, "start", lambda self, config: info)

    class _DegradingInput:
        """InputBackend stand-in that raises ToolError in the constructor."""

        def __init__(self, _dbus_address: str) -> None:
            raise ToolError("EIS input devices did not resume within 5s: keyboard")

    engine = AutomationEngine()
    monkeypatch.setattr(core_module, "InputBackend", _DegradingInput)
    # Auto-detection must not shell out to a real kscreen-doctor.
    monkeypatch.setattr(core_module, "_detect_physical_screen_size", lambda: (800, 600))
    monkeypatch.setattr(core_module.time, "sleep", lambda *_: None)

    with caplog.at_level(logging.WARNING, logger="kwin_mcp.core"):
        result = engine.session_start()

    assert engine._input is None  # degraded, not crashed
    assert "No input backend available" in result
    assert "Session started" in result
    assert "KWin EIS input backend unavailable" in caplog.text


def test_session_connect_degrades_when_input_backend_raises_tool_error(
    monkeypatch, tmp_path, caplog
) -> None:
    """The session_connect twin of the session_start ToolError degradation.

    Same F5 contract at the second InputBackend construction site
    (core.py session_connect): a partial EIS handshake raises ToolError,
    which must degrade to the ydotool/no-input fallback instead of
    escaping session_connect after the live session was already attached.
    """
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/dbus")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-mcp-test")
    monkeypatch.setattr(core_module.time, "sleep", lambda *_: None)

    class FakeBus:
        def get_object(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

    fake_dbus = SimpleNamespace(
        DBusException=type("DBusException", (Exception,), {}),
        bus=SimpleNamespace(BusConnection=lambda _addr: FakeBus()),
    )
    monkeypatch.setitem(sys.modules, "dbus", fake_dbus)
    monkeypatch.setitem(sys.modules, "dbus.bus", fake_dbus.bus)

    class _DegradingInput:
        """InputBackend stand-in that raises ToolError in the constructor."""

        def __init__(self, _dbus_address: str) -> None:
            raise ToolError("EIS input devices did not resume within 5s: keyboard")

    engine = AutomationEngine()
    monkeypatch.setattr(core_module, "InputBackend", _DegradingInput)
    monkeypatch.setattr(core_module.shutil, "which", lambda _name: None)

    with caplog.at_level(logging.WARNING, logger="kwin_mcp.core"):
        result = engine.session_connect()

    assert engine._input is None  # degraded, not crashed
    assert "Connected to live KWin session" in result
    assert "No input backend available" in result
    assert "KWin EIS input backend unavailable" in caplog.text
