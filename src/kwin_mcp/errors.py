"""Centralised tool error contract (wingman #227, H-3/H-4).

Two error channels exist in this codebase and both must deliver their details
to the MCP client:

1. **ToolError** (re-exported from the MCP SDK, ``mcp.server.mcpserver``) —
   an anticipated tool failure. The SDK turns it into an ``is_error=True``
   tool result whose content is the exception message (see
   ``mcp.server.mcpserver.server._handle_call_tool``). Use it for "negative
   result" outcomes: no such app/PID, element not found within the timeout,
   clipboard disabled, invalid arguments. The message is for the calling
   agent to read and act on; the server logs it at INFO.

2. **Anything else** — an unexpected failure (crash, dead compositor, ...).
   The SDK wraps it in ``UnexpectedToolError``: the client sees only
   "Error executing tool <name>" while the traceback stays in the server
   logs. Raise plain exceptions (RuntimeError, ValueError, ...) for these.

Historically, negative results were returned as success-payload strings
("Error: ...", "No ... found", "Timeout after ...") with ``isError=False``,
so agents could not tell a failure from a success (H-4), and real errors were
raised as bare exceptions whose details were swallowed by
``UnexpectedToolError`` (H-3/F5). This module is the single conversion point
used by the server wrapper.
"""

from __future__ import annotations

from typing import NoReturn

from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["TOOL_ERROR", "WAIT_TIMEOUT_PREFIX", "ToolError", "tool_error", "tool_timeout"]

# Canonical timeout message prefix for wait_for_element; kept here so tests
# and callers agree on the contract.
WAIT_TIMEOUT_PREFIX = "TIMEOUT"


def tool_error(message: str) -> NoReturn:
    """Raise a ToolError carrying ``message`` to the MCP client.

    The single conversion point for anticipated tool failures: the SDK turns
    ToolError into an ``is_error=True`` result whose content is exactly this
    message (details no longer vanish into the server's stderr).

    Named ``tool_error`` per the fix contract (``_tool_error``); exported
    without the underscore because it is a cross-module API.
    """
    raise ToolError(message)


def tool_timeout(query: str, timeout_ms: int, states: list[str] | None = None) -> ToolError:
    """Build the canonical timeout ToolError for wait_for_element.

    The message starts with TIMEOUT so agents can branch on it. Per the MCP
    convention chosen here (documented in wait_for_element's docstring), a
    timed-out wait is reported as an anticipated tool failure (isError=True),
    not a crash — the caller's predicate simply never became true.
    """
    criteria = f"query='{query}'"
    if states:
        criteria += f", states={states}"
    return ToolError(f"{WAIT_TIMEOUT_PREFIX} after {timeout_ms}ms: no elements matching {criteria}")


# Alias kept for the server wrapper; semantically the SDK's ToolError.
TOOL_ERROR = ToolError
