"""Tests for coordinate clipping in pointer/touch tools (wingman #227, H-6).

Policy: coordinates outside the virtual screen are clipped to the screen
bounds and the response notes the clipping — silently accepting far-off
coordinates (e.g. touch_tap(-5000, -5000)) misleads the calling agent.

The screen size is known for virtual sessions (session_start). Live sessions
do not expose a reliable size, so only the lower bound (>= 0) is enforced
there and no upper-bound clipping note is produced.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kwin_mcp.core import AutomationEngine, clip_to_screen

SCREEN = (1920, 1080)


def _engine_with_backend() -> tuple[AutomationEngine, MagicMock]:
    """An engine with a mocked input backend and a known screen size."""
    engine = AutomationEngine()
    backend = MagicMock()
    engine._input = backend
    engine._screen_size = SCREEN
    return engine, backend


def test_clip_to_screen() -> None:
    """Clipping keeps in-bounds points and clamps out-of-bounds ones."""
    assert clip_to_screen(100, 200, SCREEN) == (100, 200, False)
    assert clip_to_screen(-5, -5, SCREEN) == (0, 0, True)
    assert clip_to_screen(2000, 2000, SCREEN) == (1919, 1079, True)
    assert clip_to_screen(-5000, -5000, SCREEN) == (0, 0, True)
    # Edge pixels are valid.
    assert clip_to_screen(0, 0, SCREEN) == (0, 0, False)
    assert clip_to_screen(1919, 1079, SCREEN) == (1919, 1079, False)


def test_clip_to_screen_unknown_size_only_lower_bound() -> None:
    """Without a known screen size only negative coordinates are clamped."""
    assert clip_to_screen(-5, -5, None) == (0, 0, True)
    assert clip_to_screen(100, 200, None) == (100, 200, False)
    assert clip_to_screen(50000, 50000, None) == (50000, 50000, False)


def test_touch_tap_clips_offscreen_coordinates() -> None:
    """touch_tap far off-screen is clipped and reported."""
    engine, backend = _engine_with_backend()

    result = engine.touch_tap(-5000, -5000)
    assert "(clipped to screen bounds)" in result
    backend.touch_tap.assert_called_once_with(0, 0, hold_ms=0)


def test_mouse_click_clips_offscreen_coordinates() -> None:
    """mouse_click off-screen is clipped and reported."""
    engine, backend = _engine_with_backend()

    result = engine.mouse_click(3000, 500)
    assert "(clipped to screen bounds)" in result
    args = backend.mouse_click.call_args
    assert args.args[:2] == (1919, 500)


def test_mouse_move_clips_offscreen_coordinates() -> None:
    """mouse_move off-screen is clipped and reported."""
    engine, backend = _engine_with_backend()

    result = engine.mouse_move(-100, -100)
    assert "(clipped to screen bounds)" in result
    backend.mouse_move.assert_called_once_with(0, 0)


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected"),
    [
        ("touch_tap", {"x": -5000, "y": -5000}, (0, 0)),
        ("mouse_click", {"x": -1, "y": 900}, (0, 900)),
        ("mouse_move", {"x": 5000, "y": 5000}, (1919, 1079)),
        ("mouse_scroll", {"x": -100, "y": -100, "delta": 2}, (0, 0)),
        ("touch_swipe", {"from_x": -100, "from_y": -100, "to_x": 100, "to_y": 100}, (0, 0)),
    ],
)
def test_offscreen_tools_report_clipping(
    tool: str, kwargs: dict, expected: tuple[int, int]
) -> None:
    """Every coordinate-taking pointer/touch tool clips consistently."""
    engine, backend = _engine_with_backend()

    result = getattr(engine, tool)(**kwargs)
    assert "(clipped to screen bounds)" in result
    # The clipped coordinates reached the input backend.
    target = getattr(backend, tool)
    assert expected in [c.args[: len(expected)] for c in target.call_args_list]
