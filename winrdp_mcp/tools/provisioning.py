"""Provisioning-adjacent tools: on-demand tooling, UAC policy, elevation, agent deploy."""

from __future__ import annotations

from typing import Optional

from .. import elevation, tooling
from ..provision import ENABLE_SSH_PS, ENABLE_WINRM_PS


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ tooling
    @mcp.tool
    def stage_tool(source: str, name: Optional[str] = None, host: Optional[str] = None) -> dict:
        """Pull a helper tool onto a box on demand.

        source: a preset ('psexec','handle','procdump','autoruns','tcpview','pslist',
        'accesschk','sigcheck'), an http(s) URL (downloaded on the box), or a local file
        path on the operator machine (uploaded). Lands in C:\\ProgramData\\winrdp-mcp\\tools.
        """
        return tooling.stage_tool(ctx.transport_for(host), source, name)

    @mcp.tool
    def stage_script(content: str, name: str, host: Optional[str] = None) -> dict:
        """Write a script (PowerShell/batch/py/text) into the box's tool cache and return
        its remote path, ready to run with run_powershell/run_cmd."""
        return tooling.stage_script(ctx.transport_for(host), content, name)

    @mcp.tool
    def list_staged_tools(host: Optional[str] = None) -> list:
        """List tools/scripts staged on a box."""
        return tooling.list_staged(ctx.transport_for(host))

    @mcp.tool
    def cleanup_staged(name: Optional[str] = None, host: Optional[str] = None) -> dict:
        """Remove a staged tool by name, or wipe the whole tool cache if name is omitted."""
        return tooling.cleanup(ctx.transport_for(host), name)

    # ------------------------------------------------------------------ elevation / UAC
    @mcp.tool
    def run_elevated(script: str, host: Optional[str] = None, run_as: str = "SYSTEM",
                     timeout: int = 300) -> dict:
        """Run PowerShell with a full elevated token via a one-shot Scheduled Task.
        run_as 'SYSTEM' (default) needs no password. This is the real UAC bypass path used
        when a normal WinRM/SSH logon would otherwise get a filtered token."""
        t = ctx.transport_for(host)
        r = elevation.run_elevated(t, script, run_as=run_as, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def uac_get(host: Optional[str] = None) -> dict:
        """Read the box's UAC policy (EnableLUA, admin prompt behaviour, token filter)."""
        return ctx.exec_json(elevation.get_uac_policy_script(), host=host)

    @mcp.tool
    def uac_set(host: Optional[str] = None, enable_lua: Optional[bool] = None,
                disable_admin_prompt: bool = False, disable_token_filter: bool = False) -> dict:
        """Adjust UAC policy. disable_token_filter=True lets non-builtin local admins get a
        full token over the network (LocalAccountTokenFilterPolicy=1). EnableLUA changes
        need a reboot."""
        script = elevation.set_uac_script(
            enable_lua=enable_lua,
            admin_approval_off=disable_admin_prompt,
            token_filter_off=disable_token_filter,
        )
        return ctx.exec_json(script, host=host, elevated=True)

    @mcp.tool
    def enable_winrm(host: Optional[str] = None) -> dict:
        """(Re)run the WinRM enablement script on a box through the current transport."""
        r = ctx.exec_ps(ENABLE_WINRM_PS, host=host, timeout=180)
        return {"ok": "WINRDP_WINRM_ENABLED" in r.stdout, "stdout": r.stdout, "stderr": r.stderr}

    @mcp.tool
    def enable_ssh(host: Optional[str] = None) -> dict:
        """Install and start the Windows OpenSSH server on a box, open the firewall, and
        set PowerShell as the default SSH shell."""
        r = ctx.exec_ps(ENABLE_SSH_PS, host=host, elevated=True, timeout=300)
        return {"ok": "WINRDP_SSH_ENABLED" in r.stdout, "stdout": r.stdout, "stderr": r.stderr}

    # ------------------------------------------------------------------ interactive UI agent
    @mcp.tool
    def deploy_ui_agent(host: Optional[str] = None, port: int = 8765,
                        auth_key: Optional[str] = None) -> dict:
        """Deploy the interactive-desktop UI agent (winremote-mcp) onto a box for
        click/type/OCR-level control, and start it as an HTTP MCP endpoint.

        Requires Python + pip on the box (installs winremote-mcp via pip). For headless
        boxes without Python, prefer the PowerShell-based tools which need no agent.
        Returns the endpoint URL to add as a second MCP server in Claude.
        """
        t = ctx.transport_for(host)
        # Verify python
        chk = t.run_ps("(Get-Command python -ErrorAction SilentlyContinue).Source", timeout=30)
        if not chk.stdout.strip():
            return {
                "ok": False,
                "error": "python not found on box; install Python first or use PowerShell tools",
            }
        install = t.run_ps("python -m pip install --upgrade winremote-mcp", timeout=600)
        key = auth_key or ""
        keyarg = f" --auth-key {key}" if key else ""
        # launch in the interactive session so it can see the desktop
        launch = (
            f"Start-Process -WindowStyle Hidden powershell -ArgumentList "
            f"'-NoProfile','-Command','winremote-mcp serve --http --host 0.0.0.0 --port {int(port)}{keyarg}'"
        )
        r = elevation.run_in_user_session(t, launch, timeout=60)
        h = ctx.resolve(host)
        return {
            "ok": True,
            "install_log_tail": install.stdout[-500:],
            "endpoint": f"http://{h.host}:{int(port)}/mcp",
            "note": "Add this URL as an HTTP MCP server in Claude for desktop UI control.",
            "launch_stderr": r.stderr,
        }
