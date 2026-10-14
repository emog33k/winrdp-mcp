"""Dynamic tool discovery — keep the model's tool list small, grow it on demand.

A full winrdp-mcp exposes ~144 tools. Loading every schema up front costs tens of
thousands of tokens and measurably degrades the model's tool choice (the 2026 MCP guidance
on context bloat). The ``dynamic`` profile instead starts with a focused *core* set plus
three meta-tools — ``list_toolsets`` / ``enable_toolset`` / ``disable_toolset`` — so the
model pulls in a group (rdp, gui, network, …) only when a task needs it. FastMCP emits a
``tools/list_changed`` notification on each change, so the client re-fetches automatically.
"""

from __future__ import annotations

from . import log
from . import tools as _tools

_log = log.get("toolsets")

# Always-on base: register/provision a box, run anything on it, and wait on results.
# Kept deliberately small (~41 tools) so the start-up tool list stays light; the heavier
# groups (files, admin, …) are opt-in below.
CORE = ["hosts", "provisioning", "system", "scripting", "ops", "waiters"]

# Optional groups the model can switch on by name. Values are tool-module short names.
TOOLSETS: dict[str, list[str]] = {
    "files": ["files"],
    "admin": ["admin"],
    "network": ["network"],
    "windows": ["windows"],
    "software": ["software"],
    "scheduling": ["scheduling"],
    "tunnel": ["tunnel"],
    "rdp": ["rdp"],
    "gui": ["gui"],
}

DESCRIPTIONS: dict[str, str] = {
    "files": "Files: list/read/write/edit, copy/move/delete, hash, search, up/download, zip.",
    "admin": "Local admin: users & groups, services, ACLs/ownership, registry, firewall, "
             "Defender, UAC, env vars, startup, failed logons.",
    "network": "Networking: open ports, connections, DNS, ping/port-check, port-proxy.",
    "windows": "Windows features & updates, hotfixes, startup items, clipboard, env vars.",
    "software": "Package managers and software install/uninstall, language runtimes.",
    "scheduling": "Scheduled tasks and deferred/at-time command execution.",
    "tunnel": "SSH port-forwarding to reach a box's localhost-bound services.",
    "rdp": "RDP control: enable/disable, sessions, open/console, multi-session, port.",
    "gui": "Interactive desktop automation: click, type, send-keys, OCR, UI tree, record.",
}


class ToolsetManager:
    """Registers the discovery meta-tools and tracks which optional groups are live."""

    def __init__(self, mcp, ctx) -> None:
        self.mcp = mcp
        self.ctx = ctx
        self.active: set[str] = set()
        self._tools: dict[str, list[str]] = {}  # toolset name -> tool names it added
        self._mod = {_tools._short(m): m for m in _tools.MODULES}

    async def _names(self) -> set[str]:
        return {t.name for t in await self.mcp.list_tools()}

    def _remove(self, name: str) -> None:
        provider = getattr(self.mcp, "local_provider", None)
        remover = getattr(provider, "remove_tool", None) or self.mcp.remove_tool
        remover(name)

    async def enable(self, name: str) -> dict:
        if name not in TOOLSETS:
            raise KeyError(f"unknown toolset '{name}'. Options: {sorted(TOOLSETS)}")
        if name in self.active:
            return {"toolset": name, "already_active": True, "added_tools": self._tools.get(name, [])}
        before = await self._names()
        for short in TOOLSETS[name]:
            self._mod[short].register(self.mcp, self.ctx)
        added = sorted(await self._names() - before)
        self.active.add(name)
        self._tools[name] = added
        _log.info("toolset %s enabled (+%d tools)", name, len(added))
        return {"toolset": name, "added_tools": added, "count": len(added),
                "active_toolsets": sorted(self.active)}

    async def disable(self, name: str) -> dict:
        if name not in self.active:
            return {"toolset": name, "already_inactive": True}
        removed = self._tools.pop(name, [])
        for t in removed:
            try:
                self._remove(t)
            except Exception:  # noqa: BLE001 — best effort; keep tearing down the rest
                _log.debug("remove_tool %s failed", t)
        self.active.discard(name)
        _log.info("toolset %s disabled (-%d tools)", name, len(removed))
        return {"toolset": name, "removed_tools": removed, "count": len(removed),
                "active_toolsets": sorted(self.active)}

    def catalog(self) -> list[dict]:
        return [
            {"name": n, "description": DESCRIPTIONS.get(n, ""), "active": n in self.active,
             "tool_count": len(self._tools.get(n, [])) if n in self.active else None}
            for n in sorted(TOOLSETS)
        ]


def register(mcp, ctx) -> ToolsetManager:
    """Register core modules and the discovery meta-tools; return the manager."""
    for short in CORE:
        mgr_mod = {_tools._short(m): m for m in _tools.MODULES}.get(short)
        if mgr_mod is not None:
            mgr_mod.register(mcp, ctx)

    manager = ToolsetManager(mcp, ctx)

    @mcp.tool
    def list_toolsets() -> list:
        """List the optional tool groups and whether each is currently loaded.

        winrdp-mcp starts lean. Call enable_toolset(name) to pull in a group (its tools then
        appear in your tool list), and disable_toolset(name) to drop it when done. The core
        tools (hosts, provisioning, system, scripting, files, admin) are always available."""
        return manager.catalog()

    @mcp.tool
    async def enable_toolset(name: str) -> dict:
        """Load an optional tool group so its tools become callable. name is one of the
        keys from list_toolsets (e.g. 'rdp', 'gui', 'network'). Idempotent."""
        return await manager.enable(name)

    @mcp.tool
    async def disable_toolset(name: str) -> dict:
        """Unload a previously enabled tool group to keep the tool list focused."""
        return await manager.disable(name)

    return manager
