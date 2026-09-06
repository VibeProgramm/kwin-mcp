"""Tests for the centralised tool error contract (wingman #227, H-3/H-4).

Contract (documented in kwin_mcp/errors.py):
- Anticipated failures (no such app/PID, invalid arguments, missing
  prerequisites, wait timeout) raise the SDK's ToolError → the MCP client
  receives isError=True with the full message (no more success-payload
  "Error: ..." strings).
- Unexpected crashes keep raising plain exceptions → SDK UnexpectedToolError
  (client sees "Error executing tool <name>").

Tests exercise the AutomationEngine error paths directly (no MCP transport):
importing kwin_mcp.server is avoided where possible, but the contract
conversion happens in the server wrapper, so one test imports it too.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kwin_mcp.core import AutomationEngine
from kwin_mcp.errors import WAIT_TIMEOUT_PREFIX, tool_timeout


def test_tool_timeout_message_contract() -> None:
    """The timeout error starts with the canonical TIMEOUT marker."""
    err = tool_timeout("konsole", 1500, states=["visible"])
    assert isinstance(err, ToolError)
    assert err.args[0].startswith(WAIT_TIMEOUT_PREFIX)
    assert "query='konsole'" in err.args[0]
    assert "1500ms" in err.args[0]


def test_read_app_log_unknown_pid_raises_tool_error() -> None:
    """read_app_log with an unknown PID → ToolError with available PIDs."""
    engine = AutomationEngine()
    fake_session = MagicMock()
    fake_session.is_running = True
    fake_session.read_app_log.side_effect = ValueError("No app with PID 999. Available PIDs: []")
    engine._session = fake_session
    with pytest.raises(ToolError, match="No app with PID"):
        engine.read_app_log(pid=999999)


def test_no_session_raises_tool_error() -> None:
    """Tool calls without a session are anticipated → ToolError."""
    engine = AutomationEngine()
    for call in (
        lambda: engine.screenshot(),
        lambda: engine.accessibility_tree(),
        lambda: engine.list_windows(),
    ):
        with pytest.raises(ToolError, match="No active session"):
            call()


def test_no_input_backend_raises_tool_error() -> None:
    """Input tools without an input backend → ToolError."""
    engine = AutomationEngine()
    for call in (
        lambda: engine.keyboard_type("x"),
        lambda: engine.mouse_click(x=1, y=1),
    ):
        with pytest.raises(ToolError, match="No input backend"):
            call()


def test_invalid_mouse_button_raises_tool_error() -> None:
    """An unknown button name is an invalid argument → ToolError."""
    engine = AutomationEngine()
    engine._input = MagicMock()
    with pytest.raises(ToolError, match="Invalid button"):
        engine.mouse_click(x=10, y=10, button="wheelspin")
    with pytest.raises(ToolError, match="Invalid button"):
        engine.mouse_button_down(x=10, y=10, button="wheelspin")


def test_clipboard_disabled_raises_tool_error() -> None:
    """Clipboard tools without enable_clipboard → ToolError."""
    engine = AutomationEngine()
    with pytest.raises(ToolError, match="Clipboard not enabled"):
        engine.clipboard_get()
    with pytest.raises(ToolError, match="Clipboard not enabled"):
        engine.clipboard_set("text")


def test_server_tool_wrapper_propagates_tool_error() -> None:
    """The MCP tool wrapper lets ToolError through for the SDK to convert
    into isError=True content (the whole point of the H-3/H-4 contract)."""
    import anyio

    import kwin_mcp.server as server_module

    async def _run() -> None:
        with pytest.raises(ToolError, match="No active session"):
            await server_module.screenshot()
        with pytest.raises(ToolError, match="No active session"):
            await server_module.read_app_log(pid=1)

    anyio.run(_run)
