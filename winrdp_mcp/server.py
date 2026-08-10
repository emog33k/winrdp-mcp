"""FastMCP server assembly, tool safety annotations, and tool allowlisting."""

from __future__ import annotations

import os

from fastmcp import FastMCP

from . import __version__, log
from .context import Context
from .tools import register_all
from .vault import Host

try:
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover
    ToolAnnotations = None

_log = log.get("server")

INSTRUCTIONS = """\
winrdp-mcp — provision and fully administer Windows RDP boxes (Win10/11/Server).

Typical flow:
  1. add_host(alias, host, username, password)      register a box (creds encrypted at rest)
  2. provision_host()                               make it manageable with zero manual setup
  3. system_info() / run_powershell(...) / ...      administer it

`host` is optional on every tool: omit it to target the active host (set with use_host),
or pass an alias to hit a specific box. Manage several boxes from one server.

Power features:
  * run_powershell(elevated=True)   full-token execution (real UAC bypass via Scheduled Task)
  * run_powershell(as_user=True)    run inside the interactive RDP desktop session
  * run_python(code, pip=...)       run a script on the box; runtime auto-installed
  * run_on_hosts(script, tag=...)   fan a command across the fleet in parallel
  * stage_tool("psexec" | URL | local-file)   pull helpers onto a box on demand
  * rdp_enable / rdp_sessions / rdp_open       first-class RDP control + connect
  * screenshot()                    capture the live RDP desktop
  * send_keys / mouse_click / ui_invoke / gui_script   drive the interactive desktop GUI
"""

# Read-only tools: never mutate box state. Safe to auto-run.
READONLY = {
    "list_hosts", "test_host", "system_info", "performance", "event_log", "screenshot",
    "file_list", "file_read", "file_search", "file_hash", "file_download", "get_acl",
    "list_services", "list_processes", "list_tasks", "list_users", "firewall_rules",
    "reg_read", "uac_get", "list_staged_tools", "rdp_status", "rdp_sessions",
    "net_info", "ping", "port_check", "net_connections", "port_proxy_list",
    "cim_query", "hotfixes", "defender_status", "env_get", "list_startup",
    "list_installed_software", "rdp_connection_file", "list_windows", "ui_find",
    "wait_for_port", "wait_for_service", "wait_for_process", "wait_for_file",
    "wait_for_window", "tail_file", "ocr_screen", "record_screen", "port_forward_list",
}

# Destructive tools: delete data, kill processes, cut access, or reboot. Clients should
# gate these behind confirmation.
DESTRUCTIVE = {
    "remove_host", "file_delete", "reboot", "power_action", "reboot_and_wait",
    "kill_process", "user_delete", "service_delete", "reg_delete", "firewall_delete",
    "rdp_disable", "clear_event_log", "uninstall_software", "defender_realtime",
    "task_delete", "cleanup_staged", "rdp_logoff_session", "port_proxy_delete",
    "install_rdp_wrapper", "uac_set", "rdp_connect_to_console", "rdp_set_port",
    "unpersist_service",
}


def _annotations_for(name: str):
    if ToolAnnotations is None:
        return None
    if name in READONLY:
        return ToolAnnotations(title=name, readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    if name in DESTRUCTIVE:
        return ToolAnnotations(title=name, readOnlyHint=False, destructiveHint=True, openWorldHint=True)
    # mutating but not destructive (create/set/start ...)
    return ToolAnnotations(title=name, readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _csv_env(key: str) -> set[str]:
    raw = os.environ.get(key, "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _install_tool_wrapper(mcp: FastMCP) -> None:
    """Wrap ``mcp.tool`` so every tool gets safety annotations and honours the allowlist.

    WINRDP_ENABLED_TOOLS (csv) — if set, ONLY these tools are registered.
    WINRDP_DISABLED_TOOLS (csv) — these tools are skipped.
    """
    enabled = _csv_env("WINRDP_ENABLED_TOOLS")
    disabled = _csv_env("WINRDP_DISABLED_TOOLS")
    original = mcp.tool

    def wrapper(fn=None, **kwargs):
        def apply(f):
            name = kwargs.get("name") or getattr(f, "__name__", "")
            if (enabled and name not in enabled) or (name in disabled):
                _log.debug("tool %s skipped by allowlist", name)
                return f  # not registered, but callable stays usable
            if "annotations" not in kwargs:
                ann = _annotations_for(name)
                if ann is not None:
                    kwargs["annotations"] = ann
            return original(f, **kwargs)

        if fn is not None and callable(fn):
            return apply(fn)
        return apply  # used as @mcp.tool(...)

    mcp.tool = wrapper  # type: ignore[assignment]


def build_server(*, local: bool = False, seed_host: Host | None = None,
                 debug: bool = False) -> tuple[FastMCP, Context]:
    log.setup(debug=debug)
    ctx = Context(default_local=local)
    if seed_host is not None:
        ctx.vault.add(seed_host, make_active=True)

    mcp = FastMCP("winrdp-mcp", instructions=INSTRUCTIONS)
    _install_tool_wrapper(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):  # pragma: no cover
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok", "version": __version__, "hosts": len(ctx.vault.all())})

    register_all(mcp, ctx)
    _log.info("winrdp-mcp %s ready (%d hosts in inventory)", __version__, len(ctx.vault.all()))
    return mcp, ctx
