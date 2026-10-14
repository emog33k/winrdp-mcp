"""Provisioning-adjacent tools: on-demand tooling, UAC policy, elevation, agent deploy."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from .. import elevation, tooling
from ..models import ExecResult
from ..progress import Context, Progress
from ..provision import ENABLE_SSH_PS, bootstrap_oneliner, enable_winrm_script
from . import _validate as V


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
                     timeout: int = 300) -> ExecResult:
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
    def enable_winrm(host: Optional[str] = None, basic: bool = False) -> dict:
        """(Re)run the WinRM enablement script on a box through the current transport.

        basic=True also turns on Basic auth + AllowUnencrypted — needed when the OPERATOR
        side is behind a proxy/VPN that breaks connection-oriented NTLM (rotating proxy etc.).
        Then register the host with winrm_auth='basic'. Security tradeoff: over HTTP the
        password travels near-cleartext — scope WinRM to your operator IP at the firewall."""
        r = ctx.exec_ps(enable_winrm_script(basic), host=host, timeout=180)
        return {"ok": "WINRDP_WINRM_ENABLED" in r.stdout,
                "basic_enabled": (basic and "WINRDP_BASIC_ENABLED" in r.stdout),
                "stdout": r.stdout, "stderr": r.stderr}

    @mcp.tool
    def enable_ssh(host: Optional[str] = None, mcp_ctx: Context | None = None) -> dict:
        """Install and start the Windows OpenSSH server on a box, open the firewall, and
        set PowerShell as the default SSH shell."""
        prog = Progress(mcp_ctx, 100)
        prog.report(10, "installing & starting OpenSSH server (download + feature install)")
        r = ctx.exec_ps(ENABLE_SSH_PS, host=host, elevated=True, timeout=300)
        prog.report(100, "OpenSSH step finished")
        return {"ok": "WINRDP_SSH_ENABLED" in r.stdout, "stdout": r.stdout, "stderr": r.stderr}

    @mcp.tool
    def rdp_bootstrap(host: Optional[str] = None, open_rdp: bool = True,
                      enable_basic: bool = False) -> dict:
        """Last-resort bootstrap for a box where ONLY RDP (3389) is reachable — no WinRM, SSH,
        or SMB — so there is no remote channel to run the enable script for you.

        This makes the one manual step nearly one-click: it copies the enable-WinRM command to
        THIS (operator) machine's clipboard and, if open_rdp, launches mstsc with the saved
        credentials (clipboard redirection is on). You then just paste it once on the box:

          1. In the RDP session: right-click Start -> 'Terminal (Admin)' / 'Windows PowerShell (Admin)'.
          2. Paste (Ctrl+V) and press Enter. It prints WINRDP_WINRM_ENABLED when done.
          3. Back here: re-run provision_host() — WinRM is now up and everything auto-detects.

        Why a manual step at all: RDP is an interactive display protocol, not a remote-exec API,
        so with every management port closed the controller cannot execute anything itself. Open
        5985 (WinRM) or 22 (SSH) inbound at your provider firewall to make provisioning fully
        automatic next time.

        enable_basic=True makes the pasted command also turn on Basic auth — use it when your
        OPERATOR machine is behind a proxy/VPN that breaks NTLM (rotating proxy etc.); then
        register the host with winrm_auth='basic'."""
        h = ctx.resolve(host)
        line = bootstrap_oneliner(h, basic=enable_basic)
        copied = False
        if os.name == "nt":
            try:
                subprocess.run("clip", input=line.encode("utf-16-le"), check=True,
                               capture_output=True)
                copied = True
            except Exception:  # noqa: BLE001 — clipboard is a convenience, not required
                copied = False
        launched = False
        if open_rdp and os.name == "nt":
            user = f"{h.domain}\\{h.username}" if h.domain else h.username
            if h.password:
                subprocess.run(["cmdkey", f"/generic:TERMSRV/{h.host}", f"/user:{user}",
                                f"/pass:{h.password}"], capture_output=True)
            try:
                subprocess.Popen(["mstsc", f"/v:{h.host}:{h.rdp_port}"])
                launched = True
            except Exception:  # noqa: BLE001
                launched = False
        return {
            "clipboard_copied": copied,
            "rdp_launched": launched,
            "oneliner": line,
            "steps": [
                "In the RDP session: Start (right-click) -> Terminal/PowerShell (Admin).",
                "Paste (Ctrl+V) and press Enter; wait for 'WINRDP_WINRM_ENABLED'.",
                "Back here: run provision_host() again.",
            ],
            "note": ("Command is on your clipboard; mstsc is connecting." if copied and launched
                     else "Copy the 'oneliner' and paste it into an elevated PowerShell on the box."),
        }

    # ------------------------------------------------------------------ interactive UI agent
    @mcp.tool
    def deploy_ui_agent(host: Optional[str] = None, port: int = 8765,
                        auth_key: Optional[str] = None, bind: str = "127.0.0.1",
                        mcp_ctx: Context | None = None) -> dict:
        """Deploy the interactive-desktop UI agent (winremote-mcp) onto a box for
        click/type/OCR-level control, and start it as an HTTP MCP endpoint.

        By default the agent binds to 127.0.0.1 (loopback) — reach it from your machine with
        port_forward(remote_port=port). To bind it to a public interface (bind='0.0.0.0') you
        MUST pass an auth_key, otherwise it would be an unauthenticated remote desktop-control
        endpoint. Requires Python + pip on the box.
        """
        # auth_key is spliced into the launch command; a token has no quotes/shell meta.
        if auth_key:
            V.token(auth_key, "auth_key")
        loopback = bind in ("127.0.0.1", "localhost", "::1")
        if not loopback:
            V.host(bind, "bind")
            if not auth_key:
                return {
                    "ok": False,
                    "error": (f"refusing to bind the UI agent to {bind} without an auth_key — that "
                              "would expose an UNAUTHENTICATED remote desktop-control endpoint on "
                              "the box. Pass auth_key=..., or keep bind='127.0.0.1' and reach it via "
                              "port_forward(remote_port=%d)." % int(port)),
                }
        t = ctx.transport_for(host)
        # Verify python
        chk = t.run_ps("(Get-Command python -ErrorAction SilentlyContinue).Source", timeout=30)
        if not chk.stdout.strip():
            return {
                "ok": False,
                "error": "python not found on box; install Python first or use PowerShell tools",
            }
        prog = Progress(mcp_ctx, 100)
        prog.report(15, "installing the UI agent (pip install winremote-mcp)")
        install = t.run_ps("python -m pip install --upgrade winremote-mcp", timeout=600)
        prog.report(80, "starting the UI agent in the desktop session")
        keyarg = f" --auth-key {auth_key}" if auth_key else ""
        # launch in the interactive session so it can see the desktop
        launch = (
            f"Start-Process -WindowStyle Hidden powershell -ArgumentList "
            f"'-NoProfile','-Command','winremote-mcp serve --http --host {bind} --port {int(port)}{keyarg}'"
        )
        r = elevation.run_in_user_session(t, launch, timeout=60)
        prog.report(100, "UI agent deployed")
        h = ctx.resolve(host)
        if loopback:
            endpoint = f"http://127.0.0.1:{int(port)}/mcp"
            note = ("Agent bound to loopback on the box. Reach it with "
                    f"port_forward(remote_port={int(port)}), then add the local URL as an HTTP MCP "
                    "server in Claude.")
        else:
            endpoint = f"http://{h.host}:{int(port)}/mcp"
            note = "Add this URL as an HTTP MCP server in Claude (auth_key required to connect)."
        return {
            "ok": True,
            "install_log_tail": install.stdout[-500:],
            "endpoint": endpoint,
            "bind": bind,
            "authenticated": bool(auth_key),
            "note": note,
            "launch_stderr": r.stderr,
        }
