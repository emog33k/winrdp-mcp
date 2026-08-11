"""winrdp-mcp — Zero-config MCP server to provision and administer any Windows RDP box.

Built for Claude & Claude Code. One package, two modes:

* ``serve``  — runs locally (where Claude Code lives) as an MCP server over stdio and
  drives one or many remote Windows boxes over WinRM / SSH / SMB. Nothing needs to be
  pre-installed on the target: the ``provision`` tool makes any box ready by itself.
* ``agent``  — the same package runs *on* the box (transport=local) and exposes the
  identical toolset over stdio or HTTP for maximum local power / lowest latency.

Attribution: the tool surface, the risk-tier model, and the interactive-desktop agent
build on the MIT-licensed projects winremote-mcp (github.com/dddabtc/winremote-mcp) and
windows-admin-mcp (github.com/Cosmicjedi/windows-admin-mcp). See NOTICE.
"""

from __future__ import annotations

__version__ = "0.1.4"
__all__ = ["__version__"]
