"""FastMCP server assembly, tool safety annotations, and tool allowlisting."""

from __future__ import annotations

import os

from fastmcp import FastMCP

from . import __version__, log, prompts, resources, toolsets
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

If you see list_toolsets/enable_toolset (the 'dynamic' profile), only core tools are loaded
up front: call enable_toolset('rdp'|'gui'|'network'|...) to pull in a group when a task
needs it. For a rare one-off, prefer run_powershell over enabling a whole group.
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
    "health_report", "whoami_priv", "failed_logons", "list_open_ports",
    "list_toolsets", "diagnose_host",
}

# Destructive tools: delete data, kill processes, cut access, or reboot. Clients should
# gate these behind confirmation.
DESTRUCTIVE = {
    "remove_host", "file_delete", "reboot", "power_action", "reboot_and_wait",
    "kill_process", "user_delete", "service_delete", "reg_delete", "firewall_delete",
    "rdp_disable", "clear_event_log", "uninstall_software", "defender_realtime",
    "task_delete", "cleanup_staged", "rdp_logoff_session", "port_proxy_delete",
    "install_rdp_wrapper", "uac_set", "rdp_connect_to_console", "rdp_set_port",
    "unpersist_service", "task_stop",
}

# Idempotent writers: converge the box to a desired state, so re-running with the same
# args has no further effect. Lets a client safely retry on a dropped connection.
# (idempotentHint is only meaningful for tools that write, i.e. not in READONLY.)
IDEMPOTENT = {
    "rdp_enable", "rdp_disable", "enable_ssh", "enable_winrm", "set_dns", "env_set",
    "reg_write", "reg_delete", "apply_baseline", "use_host", "uac_set", "rdp_set_port",
    "rdp_allow_multiple_sessions", "grant_acl", "take_own", "make_dir",
    "defender_exclusion_add", "defender_realtime", "file_delete", "remove_host",
    "enable_toolset", "disable_toolset",
}

# Tools whose reach stays inside the target box (no internet/third-party endpoints) are a
# closed world; everything else (package managers, URL downloads, web-staged tools, raw
# shell) can reach out, so it stays open-world.
CLOSED_WORLD = {
    "list_hosts", "test_host", "system_info", "performance", "event_log", "file_list",
    "file_read", "file_search", "file_hash", "get_acl", "list_services",
    "list_processes", "list_tasks", "list_users", "firewall_rules", "reg_read",
    "uac_get", "rdp_status", "rdp_sessions", "net_info", "net_connections",
    "port_proxy_list", "cim_query", "hotfixes", "defender_status", "env_get",
    "list_startup", "list_installed_software", "list_windows", "ui_find",
    "health_report", "whoami_priv", "failed_logons", "list_open_ports",
    "list_staged_tools", "list_windows",
}

# Acronyms that should stay upper-cased in the human-readable display title.
_ACRONYMS = {
    "rdp": "RDP", "acl": "ACL", "uac": "UAC", "dns": "DNS", "ssh": "SSH",
    "winrm": "WinRM", "os": "OS", "cpu": "CPU", "ui": "UI", "ocr": "OCR",
    "cim": "CIM", "id": "ID", "ip": "IP", "url": "URL", "smb": "SMB",
}


def _title_for(name: str) -> str:
    """A human-friendly display name, e.g. run_powershell -> 'Run Powershell'."""
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in name.split("_"))


def _annotations_for(name: str):
    if ToolAnnotations is None:
        return None
    title = _title_for(name)
    open_world = name not in CLOSED_WORLD
    if name in READONLY:
        return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,
                               openWorldHint=open_world)
    idempotent = name in IDEMPOTENT
    if name in DESTRUCTIVE:
        return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=True,
                               idempotentHint=idempotent, openWorldHint=open_world)
    # mutating but not destructive (create/set/start ...)
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=False,
                           idempotentHint=idempotent, openWorldHint=open_world)


def _csv_env(key: str) -> set[str]:
    raw = os.environ.get(key, "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _install_confirm_middleware(mcp: FastMCP, destructive: set[str]) -> None:
    """Opt-in server-side confirmation for destructive tools (MCP 2026 elicitation).

    Enabled with WINRDP_CONFIRM_DESTRUCTIVE=1. Before a destructive tool runs, the server
    asks the operator to confirm via elicitation. If the client doesn't support elicitation,
    the call proceeds (the destructiveHint annotation still lets the client gate it). This is
    belt-and-suspenders on top of client-side gating, for clients that don't gate themselves.
    """
    try:
        from fastmcp.exceptions import ToolError
        from fastmcp.server.elicitation import AcceptedElicitation
        from fastmcp.server.middleware import Middleware
    except Exception:  # pragma: no cover — older FastMCP without middleware
        _log.warning("WINRDP_CONFIRM_DESTRUCTIVE set but this FastMCP lacks elicitation middleware")
        return

    class ConfirmDestructive(Middleware):
        async def on_call_tool(self, context, call_next):
            name = getattr(context.message, "name", "")
            fctx = context.fastmcp_context
            if name in destructive and fctx is not None:
                args = getattr(context.message, "arguments", None) or {}
                target = args.get("host") or "the active host"
                try:
                    result = await fctx.elicit(
                        f"Confirm destructive operation '{name}' on {target}? "
                        "This can delete data, cut access, or reboot the box.",
                    )
                except Exception:  # noqa: BLE001 — client can't elicit; defer to client gating
                    result = None
                if result is not None and not isinstance(result, AcceptedElicitation):
                    raise ToolError(f"'{name}' cancelled by operator (confirmation declined).")
            return await call_next(context)

    mcp.add_middleware(ConfirmDestructive())
    _log.info("destructive-tool confirmation enabled (%d tools)", len(destructive))


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
    if os.environ.get("WINRDP_CONFIRM_DESTRUCTIVE", "").strip() in ("1", "true", "yes", "on"):
        _install_confirm_middleware(mcp, DESTRUCTIVE)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):  # pragma: no cover
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok", "version": __version__, "hosts": len(ctx.vault.all())})

    profile = os.environ.get("WINRDP_PROFILE", "full").strip() or "full"
    if profile == "dynamic":
        # Lean start: core tools + list/enable/disable_toolset meta-tools. The model grows
        # its own tool list on demand (2026 dynamic-discovery guidance for tool bloat).
        toolsets.register(mcp, ctx)
    else:
        register_all(mcp, ctx, profile=profile)
    prompts.register(mcp, ctx)
    resources.register(mcp, ctx)
    _log.info("winrdp-mcp %s ready (profile=%s, %d hosts in inventory)",
              __version__, profile, len(ctx.vault.all()))
    return mcp, ctx
