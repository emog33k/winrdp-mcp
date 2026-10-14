"""Zero-config provisioning ladder.

Given a host + admin creds, make the box remotely manageable regardless of Windows
version and regardless of what is currently enabled. The ladder, best rung first:

1. **WinRM** already reachable  -> use it, harden config.
2. **SSH** reachable           -> use it, optionally turn WinRM on for richer control.
3. **SMB(445)+DCOM(135)** only  -> push an enable-WinRM script over SMB and trigger it
   with a fire-and-forget WMI ``Win32_Process.Create`` (needs optional impacket), then
   fall through to WinRM.
4. **Nothing** but RDP(3389)    -> return a one-line bootstrap the operator pastes into
   their existing RDP session once; afterwards WinRM is on.

Everything above the ladder just asks :func:`provision` for a working :class:`Transport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config, ps
from .transports import (
    LocalTransport,
    SMBFiles,
    SSHTransport,
    Transport,
    TransportError,
    WinRMTransport,
    port_open,
    wmi_exec,
)
from .vault import Host

# --- The canonical "make me manageable" PowerShell -------------------------
# Enables WinRM over HTTP, opens the firewall, and flips LocalAccountTokenFilterPolicy so
# non-builtin local admins get a full token over the network. Idempotent; safe to re-run.
#
# Deliberately does NOT enable Basic auth, AllowUnencrypted, or TrustedHosts=* — the default
# NTLM transport encrypts the SOAP payload over HTTP without them, so enabling them would
# only weaken the box (allow a later client to send credentials near-cleartext). If you
# specifically need Basic-over-HTTP, enable it yourself, scoped and knowingly.
ENABLE_WINRM_PS = r"""
$ErrorActionPreference='Stop'
# Keep RDP allowed BEFORE we touch WinRM/firewall, so enabling remoting can never lock out
# the very RDP session the operator may have pasted this into. (We deliberately do NOT
# switch the network profile Public->Private here: that forces Windows to re-apply the
# per-profile firewall rule set, which on a fresh cloud box drops the live RDP connection
# and blocks reconnects until NLA/firewall settle. -SkipNetworkProfileCheck below lets WinRM
# come up on a Public network without that side effect.)
try { Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0 -Type DWord -ErrorAction SilentlyContinue } catch {}
try { Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue } catch {}
# Enable WinRM over HTTP — but ONLY if it isn't already up. Enable-PSRemoting -Force RESTARTS
# the WinRM service, and when this script runs OVER a live WinRM session (the provision harden
# re-run) that drops the connection (HTTP 502) and on a flaky/proxied link can leave the
# service stopped. If WinRM is already running with an HTTP listener, skip it — it's a no-op.
$svc = Get-Service WinRM -ErrorAction SilentlyContinue
$hasHttp = $false
try { $hasHttp = @(Get-ChildItem WSMan:\localhost\Listener -ErrorAction SilentlyContinue | Where-Object { ($_.Keys -join ';') -match 'Transport=HTTP' }).Count -gt 0 } catch {}
if (-not $svc -or $svc.Status -ne 'Running' -or -not $hasHttp) {
  Enable-PSRemoting -Force -SkipNetworkProfileCheck
}
Set-Service WinRM -StartupType Automatic -ErrorAction SilentlyContinue
if ((Get-Service WinRM -ErrorAction SilentlyContinue).Status -ne 'Running') { Start-Service WinRM }
# Full token for local admins over the network (fixes "Access is denied" for non-builtin admins)
$p='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null
# Firewall — open WinRM-HTTP inbound for all profiles (NTLM encrypts the payload even over
# HTTP). Scope this to your operator IP at the provider firewall / security group.
Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
netsh advfirewall firewall add rule name='WinRM-HTTP-In-5985' dir=in action=allow protocol=TCP localport=5985 2>$null
# Re-assert RDP once more after the firewall changes, belt-and-suspenders.
try { Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue } catch {}
Write-Output 'WINRDP_WINRM_ENABLED'
"""

# Opt-in add-on for operators whose network breaks connection-oriented NTLM (a rotating
# proxy / VPN: the multi-leg NTLM/Negotiate handshake needs one persistent TCP connection,
# which such proxies don't preserve, so WinRM auth fails even with the right password). Basic
# auth is stateless — each request carries the credential independently — so it survives.
#
# Security tradeoff: over HTTP the password travels base64-encoded (near-cleartext), visible
# to anything on the path. Only enable this when you must, ideally scoped to your operator IP
# at the provider firewall. AllowUnencrypted is set via the POLICY registry key so it applies
# even on a Public network profile (the `winrm set` cmdlet refuses that with a firewall error).
BASIC_WINRM_PS = r"""
# Only change (and restart WinRM) if something isn't already set — so a re-run (e.g. the
# provision harden step, which executes OVER WinRM) does NOT restart the service under itself
# (that returns a 502 mid-request and briefly drops remoting).
$changed=$false
if((winrm get winrm/config/service/auth) -notmatch 'Basic\s*=\s*true'){
  winrm set winrm/config/service/auth '@{Basic="true"}' 2>$null | Out-Null; $changed=$true }
$wp='HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinRM\Service'
if(-not(Test-Path $wp)){New-Item -Path $wp -Force | Out-Null}
if((Get-ItemProperty -Path $wp -Name AllowUnencryptedTraffic -ErrorAction SilentlyContinue).AllowUnencryptedTraffic -ne 1){
  Set-ItemProperty -Path $wp -Name AllowUnencryptedTraffic -Value 1 -Type DWord -Force; $changed=$true }
if($changed){ Restart-Service WinRM }
Write-Output 'WINRDP_BASIC_ENABLED'
"""


def enable_winrm_script(basic: bool = False) -> str:
    """The make-manageable script, optionally with the Basic-auth add-on appended."""
    return ENABLE_WINRM_PS + (BASIC_WINRM_PS if basic else "")


# Harden-only touch, safe to run OVER a live WinRM session: it configures the token policy,
# firewall and RDP but NEVER restarts the WinRM service (no Enable-PSRemoting / Restart-Service
# — those belong only to the initial enable, run over SSH/WMI/the pasted bootstrap, not over
# the WinRM session we'd be cutting). Used by the provision harden step so hardening an
# already-reachable box can't drop its own connection.
# NB: deliberately does NOT re-touch the WinRM firewall/listener. If we reached the harden
# step, WinRM is already reachable (its firewall rule is already open), and re-adding the rule
# over the live WinRM connection can trigger a firewall re-eval that drops it. It also does
# NOT (re)enable Basic — that belongs to the initial enable paths (bootstrap/SSH/WMI), run off
# the WinRM session; doing it here would restart WinRM under its own connection.
HARDEN_PS = r"""
$ErrorActionPreference='Continue'
try { Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0 -Type DWord -ErrorAction SilentlyContinue } catch {}
try { Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue } catch {}
$p='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null
Write-Output 'WINRDP_WINRM_HARDENED'
"""


ENABLE_SSH_PS = r"""
$ErrorActionPreference='Stop'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
if ($cap -and $cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service sshd -StartupType Automatic
Start-Service sshd
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
# Make PowerShell the default SSH shell for a native experience.
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
  -Value (Get-Command powershell.exe).Source -PropertyType String -Force | Out-Null
Write-Output 'WINRDP_SSH_ENABLED'
"""


@dataclass
class ProvisionReport:
    alias: str
    host: str
    reachable_ports: dict = field(default_factory=dict)
    transport: str = ""
    winrm_enabled: bool = False
    ssh_enabled: bool = False
    actions: list = field(default_factory=list)
    bootstrap_oneliner: str = ""
    success: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "host": self.host,
            "reachable_ports": self.reachable_ports,
            "transport": self.transport,
            "winrm_enabled": self.winrm_enabled,
            "ssh_enabled": self.ssh_enabled,
            "actions": self.actions,
            "bootstrap_oneliner": self.bootstrap_oneliner,
            "success": self.success,
            "message": self.message,
        }


def _scan(host: str) -> dict:
    return {
        "winrm_http": port_open(host, config.WINRM_HTTP_PORT),
        "winrm_https": port_open(host, config.WINRM_HTTPS_PORT),
        "ssh": port_open(host, config.SSH_PORT),
        "rdp": port_open(host, config.RDP_PORT),
        "smb": port_open(host, config.SMB_PORT),
        "dcom": port_open(host, 135),
    }


def _try_winrm(h: Host, use_ssl: bool) -> Optional[WinRMTransport]:
    try:
        t = WinRMTransport(
            h.host, h.username, h.password,
            use_ssl=use_ssl,
            port=h.winrm_https_port if use_ssl else h.winrm_port,
            auth=h.winrm_auth, domain=h.domain,
            cert_validation=getattr(h, "winrm_cert_validation", "ignore"),
        )
        if t.probe():
            return t
    except Exception:
        pass
    return None


def _try_ssh(h: Host) -> Optional[SSHTransport]:
    try:
        t = SSHTransport(h.host, h.username, h.password, port=h.ssh_port,
                         host_key_policy=getattr(h, "ssh_host_key_policy", "auto"))
        if t.probe():
            return t
    except Exception:
        pass
    return None


def open_transport(h: Host) -> Transport:
    """Return a working transport for an already-provisioned host (no cold-start)."""
    if h.transport == "local" or h.host in ("localhost", "127.0.0.1", "::1"):
        t = LocalTransport(h.host)
        if t.probe():
            return t

    order = []
    if h.transport in ("auto", "winrm"):
        # Prefer HTTP 5985 (NTLM already encrypts the payload, and it's the fast, standard
        # path); only try HTTPS 5986 first when the host explicitly opts into SSL.
        if h.use_ssl:
            order += [("winrm_https", True), ("winrm_http", False)]
        else:
            order += [("winrm_http", False), ("winrm_https", True)]
    if h.transport in ("auto", "ssh"):
        order += [("ssh", None)]

    for kind, ssl in order:
        if kind.startswith("winrm"):
            t = _try_winrm(h, bool(ssl))
            if t:
                h.resolved_transport = "winrm"
                return t
        elif kind == "ssh":
            t = _try_ssh(h)
            if t:
                h.resolved_transport = "ssh"
                return t
    raise TransportError(
        f"No working transport for {h.alias} ({h.host}). Run provision() first."
    )


def bootstrap_oneliner(h: Host, basic: bool = False) -> str:
    """A single line the operator can paste into an existing RDP/console session.
    basic=True also turns on Basic auth (for networks that break NTLM — see BASIC_WINRM_PS)."""
    b64 = ps.encode_command(enable_winrm_script(basic))
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {b64}"


def _ensure_fast_transfer(h: Host, transport: Transport, rep: ProvisionReport) -> None:
    """Guarantee a fast file channel exists. SMB (ADMIN$) needs no install and is preferred;
    only when 445 is blocked do we install OpenSSH so uploads can use SFTP instead of the
    slow chunked-base64-over-WinRM path."""
    if rep.reachable_ports.get("smb"):
        rep.actions.append("fast-transfer=SMB (admin share, no install)")
        return
    if port_open(h.host, config.SSH_PORT):
        rep.actions.append("fast-transfer=SFTP (SSH already up)")
        return
    try:
        r = transport.run_ps(ENABLE_SSH_PS, timeout=300)
        if "WINRDP_SSH_ENABLED" in r.stdout:
            rep.ssh_enabled = True
            rep.actions.append("fast-transfer=SFTP (installed OpenSSH for fast uploads)")
        else:
            rep.actions.append("fast-transfer=chunked (OpenSSH install unconfirmed)")
    except Exception as e:  # noqa: BLE001
        rep.actions.append(f"fast-transfer=chunked (OpenSSH install failed: {e})")


def provision(h: Host, *, enable_ssh: bool = False, allow_wmi_bootstrap: bool = True,
              fast_transfer: bool = True, enable_basic: bool = False,
              progress_cb=None) -> ProvisionReport:
    """Make ``h`` manageable, climbing the ladder. Mutates ``h.resolved_transport``.

    fast_transfer: also ensure a fast file channel (SMB, else install OpenSSH for SFTP) so
    uploads are instant instead of chunked base64 over WinRM.
    progress_cb: optional ``callable(percent: float, message: str)`` called at each rung so a
    caller (the MCP tool) can emit progress notifications."""
    def _p(pct, msg):
        if progress_cb:
            try:
                progress_cb(float(pct), msg)
            except Exception:  # noqa: BLE001 — progress must never break provisioning
                pass

    # enable_basic: turn WinRM Basic auth on wherever we run the enable script, and connect
    # via Basic (for operators behind a proxy/VPN that breaks NTLM — see BASIC_WINRM_PS).
    if enable_basic:
        h.winrm_auth = "basic"
    rep = ProvisionReport(alias=h.alias, host=h.host)
    _p(2, f"scanning {h.host} for reachable management ports")
    rep.reachable_ports = _scan(h.host)
    rep.bootstrap_oneliner = bootstrap_oneliner(h, basic=enable_basic)

    # local shortcut
    if h.transport == "local" or h.host in ("localhost", "127.0.0.1", "::1"):
        rep.transport = "local"
        rep.success = True
        rep.message = "Local transport; managing this machine directly."
        h.resolved_transport = "local"
        return rep

    # Rung 1: WinRM already up (prefer HTTP unless the host opts into SSL)
    _p(15, "trying WinRM")
    t = _try_winrm(h, h.use_ssl) or _try_winrm(h, not h.use_ssl)
    if t:
        rep.transport = "winrm"
        rep.winrm_enabled = True
        rep.actions.append("winrm-already-reachable")
        _p(80, "WinRM reachable — hardening config")
        _harden_winrm(t, rep, enable_basic)
        if fast_transfer:
            _p(95, "ensuring a fast file-transfer channel")
            _ensure_fast_transfer(h, t, rep)
        rep.success = True
        rep.message = "WinRM reachable; hardened and ready."
        h.resolved_transport = "winrm"
        _p(100, "ready (WinRM)")
        return rep

    # Rung 2: SSH
    _p(40, "trying SSH")
    s = _try_ssh(h)
    if s:
        rep.transport = "ssh"
        rep.ssh_enabled = True
        rep.actions.append("ssh-reachable")
        # try to turn WinRM on too for the richer path
        r = s.run_ps(enable_winrm_script(enable_basic), timeout=180)
        if "WINRDP_WINRM_ENABLED" in r.stdout:
            rep.winrm_enabled = True
            rep.actions.append("winrm-enabled-via-ssh")
        s.close()
        # prefer WinRM now if it came up
        if rep.winrm_enabled and (_scan(h.host)["winrm_http"] or port_open(h.host, config.WINRM_HTTP_PORT)):
            t2 = _try_winrm(h, False)
            if t2:
                rep.transport = "winrm"
                h.resolved_transport = "winrm"
                rep.success = True
                rep.message = "Provisioned WinRM over the SSH channel."
                return rep
        h.resolved_transport = "ssh"
        rep.success = True
        rep.message = "SSH working; WinRM " + ("enabled." if rep.winrm_enabled else "not enabled.")
        return rep

    # Rung 3: cold-start over SMB + WMI
    _p(60, "attempting SMB+WMI cold-start")
    if allow_wmi_bootstrap and rep.reachable_ports["smb"] and rep.reachable_ports["dcom"]:
        try:
            _cold_start_wmi(h, rep, enable_basic)
            if rep.winrm_enabled:
                t3 = _try_winrm(h, False)
                if t3:
                    rep.transport = "winrm"
                    h.resolved_transport = "winrm"
                    _harden_winrm(t3, rep, enable_basic)
                    if fast_transfer:
                        _ensure_fast_transfer(h, t3, rep)
                    rep.success = True
                    rep.message = "Cold-started WinRM via SMB+WMI bootstrap."
                    _p(100, "ready (WinRM via cold-start)")
                    return rep
        except ImportError:
            rep.actions.append("wmi-bootstrap-unavailable (pip install 'winrdp-mcp[bootstrap]')")
        except Exception as e:  # noqa: BLE001
            rep.actions.append(f"wmi-bootstrap-failed: {e}")

    # Rung 4: no remote exec channel — guide the operator through the one manual step.
    # Base the message on what ACTUALLY happened (rep.actions markers), not on the port scan:
    # a transparent proxy / VPN on the operator side can make socket connects succeed for every
    # port, so rep.reachable_ports can be all-true and must not drive the recommendation.
    rep.success = False
    _p(100, "no remote channel — manual bootstrap required")
    acted = " ".join(rep.actions)
    if "wmi-bootstrap-unavailable" in acted:
        # SMB genuinely connected (smbprotocol), only the WMI launch needs impacket.
        rep.message = (
            "SMB reached the box but the WMI cold-start needs impacket, which isn't installed. "
            "Run `pip install 'winrdp-mcp[bootstrap]'` and re-run provision() for a fully "
            "automatic enable — no RDP needed. Otherwise use rdp_bootstrap()."
        )
    else:
        # No channel we could actually use. rdp_bootstrap is the universal fallback (it no-ops
        # gracefully if RDP isn't really reachable or the operator isn't on Windows).
        rep.message = (
            "No usable management channel (WinRM/SSH/SMB all failed to connect or authenticate). "
            "If you can RDP in, call rdp_bootstrap(): it copies the enable command to your "
            "clipboard and opens mstsc, so you paste it once in an elevated PowerShell on the box, "
            "then re-run provision(). To make provisioning fully automatic, open 5985 (WinRM) or "
            "22 (SSH) inbound at your provider firewall / security group."
        )
    return rep


def _harden_winrm(t: Transport, rep: ProvisionReport, enable_basic: bool = False) -> None:
    """Re-run the enable script through an existing WinRM session (idempotent)."""
    try:
        r = t.run_ps(HARDEN_PS, timeout=120)
        if "WINRDP_WINRM_HARDENED" in r.stdout:
            rep.actions.append("winrm-hardened")
    except Exception as e:  # noqa: BLE001
        rep.actions.append(f"winrm-harden-skipped: {e}")


def _cold_start_wmi(h: Host, rep: ProvisionReport, enable_basic: bool = False) -> None:
    """Stage the enable script over SMB and launch it fire-and-forget over WMI."""
    smb = SMBFiles(h.host, h.username, h.password, h.domain)
    if not smb.probe():
        raise TransportError("SMB admin share not accessible")
    remote_ps = config.REMOTE_TMP + r"\bootstrap.ps1"
    # UTF-8 BOM so `powershell -File` decodes it correctly (BOM-less UTF-16 is misread).
    smb.write(enable_winrm_script(enable_basic).encode("utf-8-sig"), remote_ps)
    rep.actions.append("bootstrap-staged-over-smb")
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_ps}"'
    wmi_exec(h.host, h.username, h.password, cmd, h.domain)
    rep.actions.append("bootstrap-launched-over-wmi")
    # give WinRM a moment to come up
    import time
    for _ in range(15):
        if port_open(h.host, config.WINRM_HTTP_PORT):
            rep.winrm_enabled = True
            break
        time.sleep(2)
