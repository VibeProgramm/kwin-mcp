"""Tests for physical screen size auto-detection (session_start 0-semantics).

Adopted from 01SW/kwin-mcp: ``session_start`` used to hardcode a 1920x1080
virtual screen, so a virtual session on a different physical display got a
mismatched size. ``screen_width=0``/``screen_height=0`` (now the default)
means "match the physical display", detected at every session_start via
kscreen-doctor (KDE) → xrandr (X11) → 1920x1080 fallback; explicit values
keep priority.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import kwin_mcp.core as core_module
from kwin_mcp.core import _DEFAULT_VIRTUAL_SIZE, AutomationEngine, _detect_physical_screen_size


def _which(monkeypatch: Any, available: dict[str, str]) -> Any:
    """shutil.which stub restricted to the given tools."""
    return lambda name: available.get(name)


def _run_result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_detect_kscreen_doctor_geometry(monkeypatch) -> None:
    """kscreen-doctor output's Geometry line of an enabled output wins."""
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        assert args[0] == ["kscreen-doctor", "-o"]
        assert kwargs.get("timeout") == 5
        return _run_result(
            "Output: 1 eDP-1 enabled connected priority 1 Panel\n"
            "Geometry: 0,0 2560x1440\n"
            "Mode: 2560x1440@165\n"
        )

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)
    assert _detect_physical_screen_size() == (2560, 1440)


def test_detect_kscreen_doctor_skips_disabled_outputs(monkeypatch) -> None:
    """Only lines starting with 'Geometry:' are parsed; noise is ignored.

    (kscreen-doctor prints a Geometry line per output; a disabled output has
    no enabled marker in our parse — first Geometry line wins, matching the
    01SW reference implementation.)
    """
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result("Output: 1 DP-1 disabled\nGeometry: 0,0 1920x1080\n"),
    )
    assert _detect_physical_screen_size() == (1920, 1080)


def test_detect_falls_back_to_xrandr_primary(monkeypatch) -> None:
    """No kscreen-doctor → xrandr primary monitor line is parsed."""
    monkeypatch.setattr(
        core_module.shutil, "which", _which(monkeypatch, {"xrandr": "/usr/bin/xrandr"})
    )

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        assert args[0] == ["xrandr"]
        return _run_result(
            "DP-2 connected primary 1920x1080+0+0 (normal left inverted right) 527mm x 296mm\n"
            "HDMI-0 disconnected (normal left inverted right)\n"
        )

    monkeypatch.setattr(core_module.subprocess, "run", fake_run)
    assert _detect_physical_screen_size() == (1920, 1080)


def test_detect_falls_back_to_xrandr_current(monkeypatch) -> None:
    """xrandr without a primary line → the Screen current size is parsed."""
    monkeypatch.setattr(
        core_module.shutil, "which", _which(monkeypatch, {"xrandr": "/usr/bin/xrandr"})
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Screen 0: minimum 8 x 8, current 2560 x 1440, maximum 32767 x 32767\n"
        ),
    )
    assert _detect_physical_screen_size() == (2560, 1440)


def test_detect_returns_default_when_no_tools(monkeypatch) -> None:
    """Neither kscreen-doctor nor xrandr exists → the default size."""
    monkeypatch.setattr(core_module.shutil, "which", _which(monkeypatch, {}))

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        msg = "subprocess must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(core_module.subprocess, "run", fail_run)
    assert _detect_physical_screen_size() == _DEFAULT_VIRTUAL_SIZE == (1920, 1080)


def test_detect_returns_default_on_tool_failure(monkeypatch) -> None:
    """A crashing/timing-out detector degrades to the default size."""
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="kscreen-doctor", timeout=5)
        ),
    )
    assert _detect_physical_screen_size() == _DEFAULT_VIRTUAL_SIZE


def test_detect_returns_default_on_garbage_output(monkeypatch) -> None:
    """Unparseable output → the default size, no exception."""
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(core_module.subprocess, "run", lambda *a, **k: _run_result("garbage"))
    assert _detect_physical_screen_size() == _DEFAULT_VIRTUAL_SIZE


class FakeSession:
    """Session stub capturing the SessionConfig handed to Session.start."""

    def __init__(self) -> None:
        self.is_running = False
        self.last_config: Any = None

    def start(self, config: Any) -> SimpleNamespace:
        self.last_config = config
        return SimpleNamespace(
            dbus_address="unix:path=/tmp/fake",
            wayland_socket="wayland-fake",
            screenshot_dir=None,
            home_dir=None,
        )

    def stop(self, **kwargs: Any) -> None:
        return None


class FakeInputBackend:
    """InputBackend stub avoiding a real D-Bus/libei connection."""

    def __init__(self, dbus_address: str) -> None:
        self.dbus_address = dbus_address

    def close(self) -> None:
        return None


class _LazySession:
    """Lazy accessor for the FakeSession the engine creates in session_start."""

    def __init__(self, holder: dict[str, FakeSession]) -> None:
        self._holder = holder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._holder["session"], name)


def _engine(monkeypatch, detected: tuple[int, int]) -> tuple[AutomationEngine, _LazySession]:
    """An engine with detection, session and input backend faked out.

    core.py constructs ``Session()`` itself, so the stub is installed as a
    factory; the returned accessor resolves the instance lazily (it exists
    after the first session_start call).
    """
    holder: dict[str, FakeSession] = {}

    def session_factory() -> FakeSession:
        holder["session"] = FakeSession()
        return holder["session"]

    monkeypatch.setattr(core_module, "_detect_physical_screen_size", lambda: detected)
    monkeypatch.setattr(core_module, "Session", session_factory)
    monkeypatch.setattr(core_module, "InputBackend", FakeInputBackend)
    monkeypatch.setattr(core_module.time, "sleep", lambda *_: None)

    engine = AutomationEngine()
    return engine, _LazySession(holder)


def test_session_start_zero_resolves_detected_size(monkeypatch) -> None:
    """screen 0/0 (the new default) → the detected physical size is used."""
    engine, session = _engine(monkeypatch, detected=(2560, 1440))

    engine.session_start()

    config = session.last_config
    assert config.screen_width == 2560
    assert config.screen_height == 1440
    assert engine._screen_size == (2560, 1440)


def test_session_start_explicit_size_keeps_priority(monkeypatch) -> None:
    """Explicit non-zero values bypass detection (backwards compatible)."""
    engine, session = _engine(monkeypatch, detected=(2560, 1440))

    def fail_detect() -> tuple[int, int]:
        msg = "detection must not run for explicit sizes"
        raise AssertionError(msg)

    monkeypatch.setattr(core_module, "_detect_physical_screen_size", fail_detect)

    engine.session_start(screen_width=1280, screen_height=720)

    config = session.last_config
    assert config.screen_width == 1280
    assert config.screen_height == 720
    assert engine._screen_size == (1280, 720)


def test_session_start_detection_runs_on_every_call(monkeypatch) -> None:
    """0-sizes are re-resolved per call: a changed display is picked up."""
    engine, session = _engine(monkeypatch, detected=(1920, 1080))

    engine.session_start()
    assert session.last_config.screen_width == 1920

    monkeypatch.setattr(core_module, "_detect_physical_screen_size", lambda: (3840, 2160))
    engine.session_stop()
    engine.session_start()
    assert session.last_config.screen_width == 3840
    assert session.last_config.screen_height == 2160


def test_session_start_partial_zero_resolves_both(monkeypatch) -> None:
    """A partially zero request (0 x 720) resolves BOTH dimensions together —
    mixing a detected width with a caller height would produce a nonsensical
    aspect ratio (matches the 01SW reference behaviour)."""
    engine, session = _engine(monkeypatch, detected=(2560, 1440))

    engine.session_start(screen_width=0, screen_height=720)

    config = session.last_config
    assert config.screen_width == 2560
    assert config.screen_height == 1440
