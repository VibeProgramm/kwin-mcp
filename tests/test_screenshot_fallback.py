"""Tests for the ScreenShot2-first capture with spectacle fallback.

Ported from upstream isac322/kwin-mcp#42: ``capture_screenshot_to_file``
unconditionally called ``_capture_via_spectacle``, contrary to its own
documentation — and minimal/virtual sessions may not have spectacle at all,
while ScreenShot2 works. Now the D-Bus route is tried first and spectacle is
the fallback; when both fail, the error carries both causes.

The frame burst path keeps its historical behavior: empty frames are skipped
(not errors), so ``_capture_raw_frame`` itself does not raise on empty data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import dbus
import pytest

if TYPE_CHECKING:
    from pathlib import Path

import kwin_mcp.screenshot as screenshot_module
from kwin_mcp.screenshot import capture_screenshot_to_file


def test_dbus_success_skips_spectacle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A successful D-Bus capture returns immediately; spectacle is not called."""
    calls: list[str] = []

    def fake_dbus(address: str, path: Path, *, include_cursor: bool = False) -> Path:
        calls.append("dbus")
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(screenshot_module, "capture_screenshot_dbus", fake_dbus)
    monkeypatch.setattr(
        screenshot_module,
        "_capture_via_spectacle",
        lambda *a, **k: calls.append("spectacle"),
    )

    path = capture_screenshot_to_file("unix:path=/tmp/dbus", "wayland-0", output_dir=tmp_path)
    assert calls == ["dbus"]
    assert path.parent == tmp_path


def test_dbus_failure_falls_back_to_spectacle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A D-Bus failure (DBusException) degrades to spectacle, not an error."""
    calls: list[str] = []

    def failing_dbus(address: str, path: Path, *, include_cursor: bool = False) -> Path:
        calls.append("dbus")
        raise dbus.DBusException("not authorized")

    def fake_spectacle(
        address: str,
        socket: str,
        *,
        output_path: Path,
        include_cursor: bool = False,
    ) -> None:
        calls.append("spectacle")
        output_path.write_bytes(b"png")

    monkeypatch.setattr(screenshot_module, "capture_screenshot_dbus", failing_dbus)
    monkeypatch.setattr(screenshot_module, "_capture_via_spectacle", fake_spectacle)

    path = capture_screenshot_to_file("unix:path=/tmp/dbus", "wayland-0", output_dir=tmp_path)
    assert calls == ["dbus", "spectacle"]
    assert path.exists()


def test_both_routes_failing_reports_both_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When D-Bus and spectacle both fail, the error names both causes."""

    def failing_dbus(address: str, path: Path, *, include_cursor: bool = False) -> Path:
        raise dbus.DBusException("not authorized")

    def failing_spectacle(
        address: str,
        socket: str,
        *,
        output_path: Path,
        include_cursor: bool = False,
    ) -> None:
        raise RuntimeError("spectacle not found")

    monkeypatch.setattr(screenshot_module, "capture_screenshot_dbus", failing_dbus)
    monkeypatch.setattr(screenshot_module, "_capture_via_spectacle", failing_spectacle)

    with pytest.raises(RuntimeError, match=r"not authorized.*spectacle not found"):
        capture_screenshot_to_file("unix:path=/tmp/dbus", "wayland-0", output_dir=tmp_path)


def test_frame_burst_skips_empty_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression guard for the frame burst path: an empty frame is skipped in
    phase 2 instead of aborting the burst (pre-refactor behavior)."""

    from PIL import Image

    saved: list[object] = []

    def fake_raw_frame(
        iface: dbus.Interface,
        options: dict[str, dbus.Boolean],
    ) -> tuple[bytes, int, int, int]:
        return b"", 0, 0, 0

    def fake_frombytes(*args: object, **kwargs: object) -> object:
        saved.append(args)
        return object()

    class _StubBus:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_object(self, *args: object, **kwargs: object) -> object:
            return object()

    monkeypatch.setattr(screenshot_module.dbus.bus, "BusConnection", _StubBus)
    monkeypatch.setattr(screenshot_module.dbus, "Interface", lambda *a: object())
    monkeypatch.setattr(screenshot_module, "_capture_raw_frame", fake_raw_frame)
    monkeypatch.setattr(Image, "frombytes", fake_frombytes)
    frames = screenshot_module._capture_frame_burst_dbus(
        "unix:path=/tmp/dbus", tmp_path, [0], include_cursor=False
    )
    assert frames == []
    # Phase 2 must not attempt a PNG conversion of the empty frame either.
    assert saved == []
