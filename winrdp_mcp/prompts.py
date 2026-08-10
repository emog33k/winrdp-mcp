"""MCP Prompts — reusable, user-invoked SOP workflows.

Prompts are user-controlled templates (unlike tools, which the model calls). They give a
person a one-click "do this whole operation" that steers the model through the right
sequence of winrdp-mcp tools, instead of hand-chaining a dozen calls. In Claude Code they
show up as slash-command-style entries.
"""

from __future__ import annotations


def register(mcp, ctx) -> None:
    @mcp.prompt
    def provision_and_harden(host: str, username: str = "Administrator", password: str = "",
                             alias: str = "") -> str:
        """Bring a brand-new Windows box under management and lock it down sensibly."""
        alias = alias or "box1"
        return (
            f"Provision and harden the Windows box at {host}. Do it step by step, reporting each result:\n"
            f"1. add_host(alias='{alias}', host='{host}', username='{username}', password='<given>').\n"
            f"2. provision_host() — confirm it reports success and a fast-transfer channel. If it can't reach "
            f"the box, surface the bootstrap_oneliner and stop.\n"
            f"3. system_info() and health_report() — sanity-check OS/build/resources.\n"
            f"4. rdp_enable(nla=True); confirm with rdp_status().\n"
            f"5. apply_baseline() — high-performance power plan, no sleep, long paths.\n"
            f"6. Review the exposed surface: list_open_ports() and firewall_rules(). Point out anything "
            f"listening on 0.0.0.0 that shouldn't be, and recommend scoping RDP/WinRM to the operator IP.\n"
            f"7. Summarize what changed and what the operator should still do by hand (e.g. provider "
            f"firewall, HTTPS WinRM, rotate the password)."
        )

    @mcp.prompt
    def diagnose_box(host: str = "") -> str:
        """Investigate why a box is slow, unhealthy, or misbehaving, and summarize findings."""
        h = f" on host '{host}'" if host else " on the active host"
        return (
            f"Diagnose the Windows box{h}. Gather evidence, then give a prioritized summary with a likely "
            f"root cause:\n"
            f"1. health_report() — read CPU/RAM/disk pressure, uptime, top processes, stopped auto-start "
            f"services, recent errors, pending updates, Defender.\n"
            f"2. If CPU/RAM is high, list_processes(top=15) to find the culprit; if a service is down, note it.\n"
            f"3. event_log(log='System', level='Error', count=15) and event_log(log='Application', "
            f"level='Error', count=15) for recurring failures.\n"
            f"4. If disk is low, run a quick 'Get-ChildItem C:\\ -Directory | ... ' via run_powershell to find "
            f"the biggest space users.\n"
            f"5. Report: what's wrong, the evidence, the most likely cause, and a concrete fix (with the exact "
            f"tool/command to apply it). Don't apply changes without asking."
        )

    @mcp.prompt
    def security_audit(host: str = "") -> str:
        """Run a read-only security posture review of a box and report risks."""
        h = f" on host '{host}'" if host else " on the active host"
        return (
            f"Perform a READ-ONLY security audit{h} and produce a risk-ranked report. Change nothing.\n"
            f"1. whoami_priv() — current session privileges/integrity.\n"
            f"2. list_open_ports() — everything listening; flag ports exposed on 0.0.0.0 (esp. 3389/5985/445).\n"
            f"3. list_users() — enumerate local users; flag every account in Administrators and any enabled "
            f"account with a stale/never last-logon.\n"
            f"4. failed_logons(count=50) — look for brute-force patterns (repeated source IPs / accounts).\n"
            f"5. defender_status() and uac_get() — AV realtime + UAC posture.\n"
            f"6. list_startup() — autostart entries; flag anything unusual.\n"
            f"7. rdp_status() — NLA required? custom port?\n"
            f"Summarize as: Critical / High / Medium findings, each with the evidence and the exact "
            f"remediation tool to fix it."
        )

    @mcp.prompt
    def setup_dev_box(host: str = "", runtimes: str = "python,node,git") -> str:
        """Turn a box into a working dev environment (runtimes + common tools)."""
        h = f" on host '{host}'" if host else " on the active host"
        return (
            f"Set up a development environment{h}. Requested runtimes: {runtimes}.\n"
            f"1. ensure_package_manager('winget') if winget is missing (check with a quick run_powershell "
            f"'Get-Command winget').\n"
            f"2. For each runtime: python -> ensure_runtime('python'); node -> ensure_runtime('node'); "
            f"git and others -> install_software(<winget id>, e.g. 'Git.Git', 'Microsoft.VisualStudioCode').\n"
            f"3. Verify each: run_python(\"import sys;print(sys.version)\") for Python, run_node(...) for Node, "
            f"run_powershell('git --version') for git.\n"
            f"4. Optionally stage_tool('psexec') and other Sysinternals if useful.\n"
            f"5. Report exactly what got installed (with versions) and anything that failed."
        )

    @mcp.prompt
    def open_service_locally(host: str, remote_port: int, note: str = "") -> str:
        """Reach a service bound to a box's localhost from your machine (SSH tunnel)."""
        return (
            f"I need to reach the service on {host}'s 127.0.0.1:{remote_port} from this machine"
            f"{(' (' + note + ')') if note else ''}.\n"
            f"1. Confirm the service is actually listening on the box: run_powershell "
            f"(\"Test-NetConnection 127.0.0.1 -Port {remote_port}\", loopback=True) — note loopback=True, "
            f"because a normal WinRM logon can't reach loopback.\n"
            f"2. Open a tunnel: port_forward(remote_port={remote_port}, host='{host}'). It needs OpenSSH on "
            f"the box; if it fails, run enable_ssh() (or re-run provision_host) first.\n"
            f"3. Give me the local_url it returns, and remind me to port_forward_stop() when done."
        )
