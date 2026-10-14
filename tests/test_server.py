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


def test_dynamic_profile_lean_start(monkeypatch):
    monkeypatch.setenv("WINRDP_PROFILE", "dynamic")
    mcp, ctx = build_server()
    try:
        names = {t.name for t in _tools(mcp)}
        # discovery meta-tools present; heavy groups not loaded yet
        assert {"list_toolsets", "enable_toolset", "disable_toolset"} <= names
        assert "add_host" in names and "run_powershell" in names  # core
        assert "rdp_enable" not in names and "file_read" not in names  # lazy groups
        assert len(names) < 60
    finally:
        ctx.close()


def test_dynamic_enable_disable_toolset(monkeypatch):
    monkeypatch.setenv("WINRDP_PROFILE", "dynamic")
    mcp, ctx = build_server()

    async def run():
        from fastmcp import Client
        async with Client(mcp) as c:
            before = {t.name for t in await c.list_tools()}
            await c.call_tool("enable_toolset", {"name": "rdp"})
            after = {t.name for t in await c.list_tools()}
            assert "rdp_enable" in after and "rdp_enable" not in before
            await c.call_tool("disable_toolset", {"name": "rdp"})
            final = {t.name for t in await c.list_tools()}
            assert "rdp_enable" not in final

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
        ctx.close()


def test_structured_output_schema():
    mcp, ctx = build_server()
    try:
        by_name = {t.name: t for t in _tools(mcp)}
        # ExecResult is the only typed output (server-constructed, fixed types).
        ec = (by_name["run_cmd"].output_schema or {}).get("properties", {})
        assert {"stdout", "stderr", "rc"} <= set(ec)
        # Live-data tools stay loose on purpose (CIM/registry values can be null), so their
        # output is NOT strictly validated against a per-field schema.
        si = (by_name["system_info"].output_schema or {}).get("properties", {})
        assert "hostname" not in si
    finally:
        ctx.close()


def test_confirm_destructive_declines(monkeypatch):
    monkeypatch.setenv("WINRDP_CONFIRM_DESTRUCTIVE", "1")
    mcp, ctx = build_server()

    async def run():
        from fastmcp import Client
        from fastmcp.client.elicitation import ElicitResult

        async def decline(message, response_type, params, c):
            return ElicitResult(action="decline")

        async def accept(message, response_type, params, c):
            return ElicitResult(action="accept", content={})

        # destructive tool is blocked when the operator declines
        async with Client(mcp, elicitation_handler=decline) as c:
            raised = False
            try:
                await c.call_tool("remove_host", {"alias": "ghost"})
            except Exception:
                raised = True
            assert raised, "declined destructive call should be blocked"
        # ... and proceeds when accepted (unknown alias -> its own benign result)
        async with Client(mcp, elicitation_handler=accept) as c:
            r = await c.call_tool("remove_host", {"alias": "ghost"})
            assert r.data == {"removed": False, "alias": "ghost"}
        # a read-only tool never triggers confirmation
        async with Client(mcp, elicitation_handler=decline) as c:
            assert await c.call_tool("list_hosts", {}) is not None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
        ctx.close()


def test_progress_param_hidden_from_schema():
    """Long-running tools accept an injected Context for progress, which must not leak into
    the model-facing input schema."""
    mcp, ctx = build_server()
    try:
        by_name = {t.name: t for t in _tools(mcp)}
        for name in ["provision_host", "reboot_and_wait", "wait_for_port", "wait_for_service",
                     "wait_for_process", "wait_for_file", "install_software", "windows_update",
                     "file_upload", "download_file", "pip_install", "ensure_runtime",
                     "ensure_package_manager", "enable_ssh", "deploy_ui_agent",
                     "persist_as_service", "install_rdp_wrapper", "windows_features",
                     "defender_scan", "sync_folder", "transfer_between_hosts"]:
            props = set((by_name[name].parameters or {}).get("properties", {}))
            assert "mcp_ctx" not in props, f"{name} leaks mcp_ctx into its schema"
        # the new operational params ARE exposed
        assert "max_bytes" in (by_name["tail_file"].parameters or {}).get("properties", {})
        assert "task_stop" in by_name
    finally:
        ctx.close()


def test_diagnose_host_registered():
    mcp, ctx = build_server()
    try:
        names = {t.name for t in _tools(mcp)}
        assert "diagnose_host" in names
    finally:
        ctx.close()
