"""Delay labels of `_with_frame_capture` frame bursts (round-4 BUG-3).

The capture layer (screenshot.py) embeds the TRUE per-frame delay in every
saved file name — ``frame_{i:03d}_{delay_ms}ms.png``, where ``i`` and
``delay_ms`` refer to the frame's position in the sorted delay list — and
silently skips empty (failed) frames inside the capture internals. The
engine therefore cannot recover the delay by zipping the requested delays
against the returned paths positionally: an interior empty frame shifted
every later label (delays [0, 100, 200] with an empty 100ms frame reported
the 200ms file as "100ms"). The report reads the label from the file name
instead; only non-standard names fall back to an honest "?" label.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from kwin_mcp import core as core_module
from kwin_mcp.core import AutomationEngine
from kwin_mcp.session import SessionInfo

if TYPE_CHECKING:
    from pathlib import Path


def _engine_with_frames(monkeypatch: Any, tmp_path: Any, frames: list[tuple[int, str]]) -> str:
    """Run _with_frame_capture against a stubbed burst capture.

    ``frames`` are (delay_ms, file_stem) pairs the capture layer "saved";
    every file is really written into tmp_path so the report's stat() calls
    work. The stub mirrors the real capture contract: skipped (empty)
    frames simply never appear in the returned path list, and the file name
    carries the frame's own delay.
    """
    info = SessionInfo(
        dbus_address="unix:path=/tmp/fake",
        wayland_socket="wayland-fake",
        kwin_pid=1,
        screenshot_dir=tmp_path,
    )
    engine = AutomationEngine()
    engine._session = cast("Any", SimpleNamespace(is_running=True, info=info))

    saved: list[Path] = []
    for delay_ms, stem in frames:
        path = tmp_path / f"{stem}_{delay_ms}ms.png"
        path.write_bytes(b"\x89PNG-fake-frame")
        saved.append(path)

    def fake_capture_frame_burst(**_: Any) -> list[Path]:
        return saved

    monkeypatch.setattr(core_module, "capture_frame_burst", fake_capture_frame_burst)
    return engine._with_frame_capture("action done", [0, 100, 200])


def _frame_lines(report: str) -> list[str]:
    """The report lines after the header (one per captured frame)."""
    lines = report.splitlines()
    return lines[2:]


def test_all_frames_present_keeps_true_delay_labels(monkeypatch: Any, tmp_path: Any) -> None:
    """A full burst labels every frame with its own true delay."""
    report = _engine_with_frames(
        monkeypatch,
        tmp_path,
        [(0, "frame_000"), (100, "frame_001"), (200, "frame_002")],
    )
    lines = report.splitlines()
    assert lines[0] == "action done"
    assert lines[1] == "Captured 3 frames:"
    frames = _frame_lines(report)
    assert len(frames) == 3
    assert frames[0].startswith("  0ms: ") and "frame_000_0ms.png" in frames[0]
    assert frames[1].startswith("  100ms: ") and "frame_001_100ms.png" in frames[1]
    assert frames[2].startswith("  200ms: ") and "frame_002_200ms.png" in frames[2]


def test_trailing_empty_frame_excluded_with_correct_labels(monkeypatch: Any, tmp_path: Any) -> None:
    """A skipped trailing frame just shortens the report; labels stay true."""
    report = _engine_with_frames(
        monkeypatch,
        tmp_path,
        [(0, "frame_000"), (100, "frame_001")],
    )
    lines = report.splitlines()
    assert lines[0] == "action done"
    assert lines[1] == "Captured 2 frames:"
    frames = _frame_lines(report)
    assert len(frames) == 2
    assert frames[0].startswith("  0ms: ")
    assert frames[1].startswith("  100ms: ")
    assert "200ms" not in report


def test_interior_empty_frame_does_not_shift_labels(monkeypatch: Any, tmp_path: Any) -> None:
    """The round-4 BUG-3 regression: an interior empty frame must not
    mislabel the later frames. Positional pairing reported the 200ms file
    as "100ms"; the file name carries the true delay."""
    report = _engine_with_frames(
        monkeypatch,
        tmp_path,
        [(0, "frame_000"), (200, "frame_002")],
    )
    lines = report.splitlines()
    assert lines[0] == "action done"
    assert lines[1] == "Captured 2 frames:"
    frames = _frame_lines(report)
    assert len(frames) == 2
    assert frames[0].startswith("  0ms: ") and "frame_000_0ms.png" in frames[0]
    # The 200ms frame keeps its own delay — not the skipped 100ms slot.
    assert frames[1].startswith("  200ms: ") and "frame_002_200ms.png" in frames[1]


def test_non_standard_frame_name_gets_honest_unknown_label(monkeypatch: Any, tmp_path: Any) -> None:
    """A frame whose name does not match the capture convention must not
    be labelled with a guessed delay — it degrades to an explicit "?" so
    the report never claims a false timing."""
    report = _engine_with_frames(monkeypatch, tmp_path, [(0, "renamed_frame")])
    lines = report.splitlines()
    assert lines[0] == "action done"
    assert lines[1] == "Captured 1 frames:"
    frames = _frame_lines(report)
    assert len(frames) == 1
    assert frames[0].startswith("  ?ms: ") and "renamed_frame_0ms.png" in frames[0]
