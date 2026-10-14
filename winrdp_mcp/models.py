"""Typed return shapes so a tool can advertise a precise ``outputSchema`` (MCP 2026
structured output). FastMCP derives the JSON Schema from these annotations and then
*validates every response against it*.

Because validation is strict, a typed shape is only safe for output the server constructs
itself with fixed field types. Live data pulled from CIM / the registry / PowerShell
(``system_info``, ``performance``, ``rdp_status``, ``ping``, ``port_check``, ``event_log``,
``cim_query`` …) returns ``dict``/``list`` ON PURPOSE: those values vary per box and can be
null, so a fixed schema would reject otherwise-valid responses. Only ``ExecResult`` — the
``{stdout, stderr, rc}`` the server builds for every command — lives here.
"""

from __future__ import annotations

from typing import TypedDict


class ExecResult(TypedDict):
    """Result of running a command/script on a box."""

    stdout: str
    stderr: str
    rc: int
