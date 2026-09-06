"""Tests for KWin-scripting-based window activation (A-2 fix).

wingman #227 / report #224 A-2: focus_window reported success but did not
actually move compositor-level focus. The AT-SPI grabFocus() call on a
top-level window does not activate Wayland windows (no compositor-side
"activate" — Qt/KWin only mark the widget focused). The fix delegates
activation to the KWin scripting API (workspace.activeWindow = w), the same
mechanism kdotool uses, executed through the org.kde.KWin Scripting D-Bus
interface with a tempfile + loadScript/run/unload cycle (the same pattern
kwin_windows._fetch_window_geometries already uses).

Real delivery is verified by the integration tock-test in the virtual
session; here we validate the script template and result parsing.
"""

from __future__ import annotations

import pytest

from kwin_mcp import kwin_windows


def test_activate_template_markers() -> None:
    """The activation JS template has all substitution markers and the
    compositor-side activation call."""
    assert "__APP_NAME__" in kwin_windows.JS_ACTIVATE_BY_CLASS
    assert "__BUS_NAME__" in kwin_windows.JS_ACTIVATE_BY_CLASS
    assert "__OBJECT_PATH__" in kwin_windows.JS_ACTIVATE_BY_CLASS
    assert "__INTERFACE_NAME__" in kwin_windows.JS_ACTIVATE_BY_CLASS
    # The actual activation must be the KWin scripting API, not AT-SPI.
    assert "workspace.activeWindow" in kwin_windows.JS_ACTIVATE_BY_CLASS
    assert "callDBus" in kwin_windows.JS_ACTIVATE_BY_CLASS
    # Match by resourceClass first, then caption (substring, case-insensitive).
    assert "resourceClass" in kwin_windows.JS_ACTIVATE_BY_CLASS
    assert "caption" in kwin_windows.JS_ACTIVATE_BY_CLASS


def test_list_template_markers() -> None:
    """The window-list JS template enumerates all windows with their class."""
    assert "windowList" in kwin_windows.JS_LIST_WINDOWS
    assert "resourceClass" in kwin_windows.JS_LIST_WINDOWS
    assert "internalId" in kwin_windows.JS_LIST_WINDOWS
    assert "callDBus" in kwin_windows.JS_LIST_WINDOWS


def test_parse_activation_result() -> None:
    """Script result payloads are parsed into tool outcome strings."""
    assert kwin_windows.parse_script_result("OK", "kcalc") == "OK"
    assert (
        kwin_windows.parse_script_result("not_found", "kcalc") == "No window matching 'kcalc' found"
    )
    assert kwin_windows.parse_script_result("ERROR boom", "kcalc") == "KWin script error: boom"
    with pytest.raises(RuntimeError, match="timed out"):
        kwin_windows.parse_script_result(None, "kcalc")
