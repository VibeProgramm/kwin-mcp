"""Regression tests for kwin_mcp.server tool registration.

These tests cover the tool registry structure and the private-API hack
``_apply_live_session_mode`` (it reaches into ``mcp._tool_manager._tools``
and mutates ``Tool.description``), so a refactor of the MCP SDK plumbing
is caught by tests instead of a broken live session.

No real server or KWin session is spawned: import + structure only.
"""

from __future__ import annotations

from typing import Any

import kwin_mcp.server as server_module

EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "session_start",
        "session_connect",
        "session_stop",
        "screenshot",
        "accessibility_tree",
        "find_ui_elements",
        "mouse_click",
        "mouse_move",
        "mouse_scroll",
        "mouse_drag",
        "mouse_button_down",
        "mouse_button_up",
        "keyboard_type",
        "keyboard_type_unicode",
        "keyboard_key",
        "keyboard_key_down",
        "keyboard_key_up",
        "touch_tap",
        "touch_swipe",
        "touch_pinch",
        "touch_multi_swipe",
        "clipboard_get",
        "clipboard_set",
        "wait_for_element",
        "launch_app",
        "list_windows",
        "focus_window",
        "dbus_call",
        "read_app_log",
        "wayland_info",
    }
)

# Tools whose descriptions are rewritten by _apply_live_session_mode.
LIVE_MODE_PATCHED_TOOLS: tuple[str, ...] = ("session_start", "session_connect")

LIVE_MODE_MARKERS: dict[str, str] = {
    "session_start": "The default session tool is session_connect",
    "session_connect": "This is the default session tool",
}


def _tools() -> dict[str, Any]:
    """Return the internal tool registry of the server."""
    tools = server_module.mcp._tool_manager._tools
    assert isinstance(tools, dict)
    return tools


def test_tools_registered() -> None:
    """The server exposes exactly the 30 expected tools, with no extras."""
    tools = _tools()
    assert len(tools) == len(EXPECTED_TOOLS) == 30
    assert set(tools) == EXPECTED_TOOLS


def test_live_session_mode_patch() -> None:
    """In live mode exactly the session tools are rewritten; others untouched."""
    tools = _tools()
    before = {name: tool.description for name, tool in tools.items()}

    server_module._live_session_mode = True
    try:
        server_module._apply_live_session_mode()
    finally:
        server_module._live_session_mode = False

    for name in LIVE_MODE_PATCHED_TOOLS:
        description = tools[name].description
        assert description is not None
        assert LIVE_MODE_MARKERS[name] in description, name

    # The other 28 tools keep their original descriptions.
    for name, original in before.items():
        if name in LIVE_MODE_PATCHED_TOOLS:
            continue
        assert tools[name].description == original, name


def test_tool_schemas_valid() -> None:
    """Every tool has a non-empty name, description, and dict input schema."""
    for name, tool in _tools().items():
        assert isinstance(tool.name, str) and tool.name, name
        assert isinstance(tool.description, str) and tool.description, name
        assert isinstance(tool.parameters, dict) and tool.parameters, name


def test_server_info_version_is_reported() -> None:
    """The MCP serverInfo carries the installed package version (A5).

    The SDK defaults version to '', which left clients without version
    information; the server resolves it from package metadata with an
    explicit "unknown" fallback. Tightened for F6: the serverInfo version
    must be exactly what ``_server_version()`` resolves (the old assertion
    only checked membership in a two-value set — nearly tautological), and
    it must never be empty.
    """
    assert server_module.mcp.version != ""
    assert server_module.mcp.version == server_module._server_version()


def test_server_version_falls_back_when_metadata_missing(monkeypatch) -> None:
    """PackageNotFoundError → the literal 'unknown', never an empty string."""
    import importlib.metadata

    def raise_missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(server_module.importlib.metadata, "version", raise_missing)
    assert server_module._server_version() == "unknown"
