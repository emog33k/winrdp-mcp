"""Multi-host inventory tools: add / list / select / remove / test / provision."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from ..config import WINRM_HTTP_PORT
from ..provision import _scan
from ..transports import port_open
from ..vault import Host


def register(mcp, ctx) -> None:
    @mcp.tool
    def add_host(
        alias: str,
        host: str,
        username: str = "",
        password: str = "",
        domain: str = "",
        transport: str = "auto",
        use_ssl: bool = False,
        winrm_auth: str = "ntlm",
        winrm_port: int = 5985,
        ssh_port: int = 22,
        rdp_port: int = 3389,
        winrm_cert_validation: str = "ignore",
        ssh_host_key_policy: str = "auto",
        tags: str = "",
        make_active: bool = True,
    ) -> dict:
        """Register a Windows box in the encrypted inventory.

        transport: "auto" (WinRM then SSH), "winrm", "ssh", or "local".
        winrm_cert_validation: "ignore" (self-signed, default) or "validate" (require a
        trusted cert — recommended for production over HTTPS).
        ssh_host_key_policy: "auto" (trust-on-first-use) or "reject" (known_hosts only).
        tags: optional comma-separated labels for fan-out grouping (e.g. "prod,web").
        Credentials are stored encrypted at rest. Set make_active to target it by default.
        """
        h = Host(
            alias=alias, host=host, username=username, password=password, domain=domain,
            transport=transport, use_ssl=use_ssl, winrm_auth=winrm_auth,
            winrm_port=winrm_port, ssh_port=ssh_port, rdp_port=rdp_port,
            winrm_cert_validation=winrm_cert_validation, ssh_host_key_policy=ssh_host_key_policy,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
        )
        ctx.vault.add(h, make_active=make_active)
        ctx.invalidate(alias)
        return {"added": alias, "active": ctx.vault.active.alias if ctx.vault.active else None}

    @mcp.tool
    def list_hosts() -> dict:
        """List all registered boxes (passwords redacted) and which one is active."""
        return {
            "active": ctx.vault.active.alias if ctx.vault.active else None,
            "hosts": [h.redacted() for h in ctx.vault.all()],
        }

    @mcp.tool
    def use_host(alias: str) -> dict:
        """Make a registered box the active target for subsequent tool calls."""
        if not ctx.vault.set_active(alias):
            return {"error": f"unknown alias '{alias}'"}
        return {"active": alias}

    @mcp.tool
    def remove_host(alias: str) -> dict:
        """Remove a box from the inventory."""
        ctx.invalidate(alias)
        return {"removed": ctx.vault.remove(alias), "alias": alias}

    @mcp.tool
    def test_host(host: Optional[str] = None) -> dict:
        """Scan reachable management ports and verify the live transport for a box."""
        h = ctx.resolve(host)
        ports = _scan(h.host)
        result = {"alias": h.alias, "host": h.host, "ports": ports}
        try:
            t = ctx.transport_for(host)
            r = t.run_ps("$env:COMPUTERNAME", timeout=20)
            result["transport"] = t.name
            result["online"] = r.ok
            result["computername"] = r.stdout.strip()
        except Exception as e:  # noqa: BLE001
            result["online"] = False
            result["error"] = str(e)
        return result

    @mcp.tool
    def provision_host(
        host: Optional[str] = None,
        enable_ssh: bool = False,
        allow_wmi_bootstrap: bool = True,
        fast_transfer: bool = True,
    ) -> dict:
        """Zero-config provision: make a box remotely manageable no matter its state.

        Climbs a ladder — WinRM -> SSH -> SMB/WMI cold-start -> bootstrap one-liner —
        turning on WinRM, opening the firewall, and fixing local-admin token filtering.
        fast_transfer=True also ensures a fast file channel: SMB (ADMIN$, no install) when
        445 is open, otherwise it installs OpenSSH so uploads use SFTP instead of the slow
        chunked path. Run this once per box right after add_host. Returns a detailed report;
        if it cannot auto-enable remoting, `bootstrap_oneliner` is a command to paste into
        an existing RDP session on the box once.
        """
        return ctx.provision(host, enable_ssh=enable_ssh, allow_wmi_bootstrap=allow_wmi_bootstrap,
                             fast_transfer=fast_transfer)

    @mcp.tool
    def run_on_hosts(
        script: str,
        aliases: str = "",
        tag: str = "",
        elevated: bool = False,
        timeout: int = 120,
        max_parallel: int = 8,
    ) -> dict:
        """Run one PowerShell script across many boxes in parallel and collect results.

        Select targets by `aliases` (comma-separated) or `tag`; omit both to hit every
        registered box. Returns per-host {stdout, stderr, rc} keyed by alias — the payoff
        of the multi-host inventory.
        """
        targets = _select(ctx, aliases, tag)
        if not targets:
            return {"error": "no matching hosts"}

        def _run(alias: str) -> tuple[str, dict]:
            try:
                r = ctx.exec_ps(script, host=alias, elevated=elevated, timeout=timeout)
                return alias, {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
            except Exception as e:  # noqa: BLE001
                return alias, {"error": str(e)}

        out: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(targets)))) as ex:
            futs = [ex.submit(_run, a) for a in targets]
            for f in as_completed(futs):
                alias, res = f.result()
                out[alias] = res
        return {"hosts": targets, "results": out}

    @mcp.tool
    def reboot_and_wait(host: Optional[str] = None, timeout: int = 300, force: bool = True) -> dict:
        """Reboot a box and block until it is reachable again (WinRM/SSH back up).
        Essential for automation that must continue after a restart."""
        import time

        h = ctx.resolve(host)
        flag = "/f " if force else ""
        ctx.transport_for(host).run_cmd(f"shutdown /r {flag}/t 3", timeout=30)
        ctx.invalidate(h.alias)
        time.sleep(15)  # let it start going down
        deadline = time.time() + timeout
        port = h.winrm_port if h.transport in ("auto", "winrm") else h.ssh_port
        while time.time() < deadline:
            if port_open(h.host, port) or port_open(h.host, h.ssh_port) or port_open(h.host, WINRM_HTTP_PORT):
                try:
                    r = ctx.transport_for(host).run_ps("$env:COMPUTERNAME", timeout=20)
                    if r.ok:
                        return {"online": True, "computername": r.stdout.strip(),
                                "waited_s": round(timeout - (deadline - time.time()), 1)}
                except Exception:
                    ctx.invalidate(h.alias)
            time.sleep(5)
        return {"online": False, "message": f"box did not come back within {timeout}s"}


def _select(ctx, aliases: str, tag: str) -> list:
    names = [a.strip() for a in aliases.split(",") if a.strip()]
    if names:
        return [a for a in names if ctx.vault.get(a)]
    if tag:
        return [h.alias for h in ctx.vault.all() if tag in (h.tags or [])]
    return [h.alias for h in ctx.vault.all()]
