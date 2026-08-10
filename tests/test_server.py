"""Server assembly: tool registration, safety annotations, and the allowlist."""

from __future__ import annotations

import asyncio

from winrdp_mcp.server import DESTRUCTIVE, READONLY, build_server


def _tools(mcp):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(mcp.list_tools())
    finally:
        loop.close()


def test_all_tools_register():
    mcp, ctx = build_server()
    try:
        names = {t.name for t in _tools(mcp)}
        assert len(names) > 100
        for must in ["add_host", "provision_host", "run_powershell", "run_python",
                     "rdp_enable", "run_on_hosts", "stage_tool", "screenshot"]:
            assert must in names
    finally:
        ctx.close()


def test_safety_annotations_applied():
    mcp, ctx = build_server()
    try:
        by_name = {t.name: t for t in _tools(mcp)}
        # a read-only tool is flagged read-only
        assert by_name["system_info"].annotations.readOnlyHint is True
        # a destructive tool is flagged destructive, not read-only
        assert by_name["file_delete"].annotations.destructiveHint is True
        assert by_name["file_delete"].annotations.readOnlyHint is False
        # sanity: the classification sets don't overlap
        assert not (READONLY & DESTRUCTIVE)
    finally:
        ctx.close()


def test_disabled_tools_allowlist(monkeypatch):
    monkeypatch.setenv("WINRDP_DISABLED_TOOLS", "reboot,power_action")
    mcp, ctx = build_server()
    try:
        names = {t.name for t in _tools(mcp)}
        assert "reboot" not in names
        assert "power_action" not in names
        assert "system_info" in names
    finally:
        ctx.close()


def test_enabled_tools_allowlist(monkeypatch):
    monkeypatch.setenv("WINRDP_ENABLED_TOOLS", "system_info,list_hosts")
    mcp, ctx = build_server()
    try:
        names = {t.name for t in _tools(mcp)}
        assert names == {"system_info", "list_hosts"}
    finally:
        ctx.close()
