"""Tests for visible desktop size auto-detection (session_start 0-semantics).

Adopted from 01SW/kwin-mcp: ``session_start`` used to hardcode a 1920x1080
virtual screen, so a virtual session on a different physical display got a
mismatched size. ``screen_width=0``/``screen_height=0`` (now the default)
means "match the visible desktop", detected at every session_start via
kscreen-doctor (KDE) → xrandr (X11) → 1920x1080 fallback; explicit values
keep priority.

Parsing specifics covered here (from the 01SW verification round):

- kscreen-doctor colourises its output with ANSI escapes even when piped
  (real dump in /tmp/opencode/verif-kscreen.out) — escapes must be stripped
  before any ``startswith`` check,
- a disabled output's Geometry line must be skipped (it may be stale); the
  enabled output's geometry is used; among several enabled outputs,
  ``priority 1`` wins,
- xrandr prints ``Screen ... current`` BEFORE the per-monitor lines, so the
  primary monitor must win over the desktop-wide current size.
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


def test_detect_kscreen_doctor_real_ansi_output(monkeypatch) -> None:
    """Real kscreen-doctor output (SGR-coloured, piped) parses to its geometry.

    Regression for A1: kscreen-doctor emits ANSI escape sequences even with
    capture_output, so an unstripped ``startswith("Geometry:")`` never
    matched and detection silently fell back to 1920x1080. The fixture is
    the actual byte stream captured from a live KDE session
    (/tmp/opencode/verif-kscreen.out), whose true geometry is 1746x982
    (Scale 1.1) — not the fallback default.
    """
    real_dump = (
        "\x1b[01;32mOutput: \x1b[0;0m1 eDP-1 ebe3aedd-465b-41d0-ade3-79701cdf6a8a\n"
        "\t\x1b[01;32menabled\x1b[0;0m\n"
        "\t\x1b[01;32mconnected\x1b[0;0m\n"
        "\t\x1b[01;32mpriority 1\x1b[0;0m\n"
        "\t\x1b[01;33mPanel\x1b[0;0m\n"
        "\t\x1b[01;33mreplication source:\x1b[0;0m0\n"
        "\t\x1b[01;34mModes: \x1b[0;0m 1:\x1b[01;32m1920x1080@60.01*\x1b[0;0m!  "
        "2:1680x1050@60.01  3:1280x1024@60.01\n"
        "\t\x1b[01;33mCustom modes:\x1b[0;0m None\n"
        "\t\x1b[01;33mGeometry: \x1b[0;0m0,0 1746x982\n"
        "\t\x1b[01;33mScale: \x1b[0;0m1.1\n"
        "\t\x1b[01;33mRotation: \x1b[0;0m1\n"
        "\t\x1b[01;33mOverscan: \x1b[0;0m0\n"
        "\t\x1b[01;33mVrr: \x1b[0;0mincapable\n"
        "\t\x1b[01;33mHdr: \x1b[0;0mincapable\n"
    )
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(core_module.subprocess, "run", lambda *a, **k: _run_result(real_dump))
    assert _detect_physical_screen_size() == (1746, 982)


def test_detect_kscreen_doctor_skips_disabled_output_with_geometry(monkeypatch) -> None:
    """A disabled output WITH a Geometry line is skipped; the enabled one wins.

    Regression for B1: the old parser took the first ``Geometry:`` line in
    the stream without binding it to its Output block, so a disabled output
    carrying a stale geometry shadowed the active one.
    """
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Output: 1 DP-1 disabled\n"
            "Geometry: 0,0 1920x1080\n"
            "Output: 2 eDP-1 enabled\n"
            "priority 1\n"
            "Geometry: 0,0 2560x1440\n"
        ),
    )
    assert _detect_physical_screen_size() == (2560, 1440)


def test_detect_kscreen_doctor_priority_one_wins_among_enabled(monkeypatch) -> None:
    """Two enabled outputs → the priority-1 one is chosen (B1)."""
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Output: 1 HDMI-A-1 enabled\n"
            "priority 2\n"
            "Geometry: 0,0 1680x1050\n"
            "Output: 2 eDP-1 enabled\n"
            "priority 1\n"
            "Geometry: 1920,0 2560x1440\n"
        ),
    )
    assert _detect_physical_screen_size() == (2560, 1440)


def test_detect_kscreen_doctor_no_enabled_outputs_falls_through(monkeypatch) -> None:
    """kscreen-doctor present but all outputs disabled → xrandr/default takes over."""
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
    # No xrandr available → default fallback.
    assert _detect_physical_screen_size() == _DEFAULT_VIRTUAL_SIZE


def test_detect_kscreen_doctor_combined_flags_line(monkeypatch) -> None:
    """Flags combined on one follow-up line are recognised (review follow-up).

    Some kscreen-doctor versions print ``enabled connected priority 1`` as a
    single indented line instead of one flag per line; the old parser only
    matched a bare ``enabled`` line and skipped such outputs as disabled.
    """
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Output: 1 DP-1\n"
            "enabled connected priority 1\n"
            "Geometry: 0,0 1920x1080\n"
        ),
    )
    assert _detect_physical_screen_size() == (1920, 1080)


def test_detect_kscreen_doctor_disabled_token_wins(monkeypatch) -> None:
    """An explicit disabled token keeps the output out of the race."""
    monkeypatch.setattr(
        core_module.shutil,
        "which",
        _which(monkeypatch, {"kscreen-doctor": "/usr/bin/kscreen-doctor"}),
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Output: 1 DP-1\ndisabled connected\nGeometry: 0,0 1920x1080\n"
            "Output: 2 eDP-1\nenabled connected\nGeometry: 0,0 1280x720\n"
        ),
    )
    assert _detect_physical_screen_size() == (1280, 720)


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


def test_detect_xrandr_primary_beats_earlier_screen_current(monkeypatch) -> None:
    """xrandr Screen-current printed BEFORE the primary line → primary wins.

    Regression for B2: xrandr prints ``Screen 0: ... current 4480x1440``
    (the whole desktop) before per-monitor lines, so the old single-pass
    parser returned the desktop size instead of the primary monitor's.
    """
    monkeypatch.setattr(
        core_module.shutil, "which", _which(monkeypatch, {"xrandr": "/usr/bin/xrandr"})
    )
    monkeypatch.setattr(
        core_module.subprocess,
        "run",
        lambda *a, **k: _run_result(
            "Screen 0: minimum 320 x 200, current 4480 x 1440, maximum 32767 x 32767\n"
            "DP-1 connected primary 1920x1080+0+0 (normal left inverted right) 509mm x 286mm\n"
            "DP-2 connected 2560x1440+1920+0 (normal left inverted right) 597mm x 336mm\n"
        ),
    )
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


def _engine(monkeypatch: Any, detected: tuple[int, int]) -> tuple[AutomationEngine, _LazySession]:
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
    """screen 0/0 (the new default) → the detected desktop size is used."""
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
