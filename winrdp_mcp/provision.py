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
# Enables WinRM over HTTP, opens the firewall, allows unencrypted+basic for local
# accounts if needed, and flips LocalAccountTokenFilterPolicy so non-builtin local
# admins get a full token over the network. Idempotent; safe to re-run.
ENABLE_WINRM_PS = r"""
$ErrorActionPreference='Stop'
# Network profile can block WinRM quickconfig; make connections Private first.
try { Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'} |
      Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue } catch {}
Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
# Listener + auth
winrm quickconfig -quiet -force 2>$null
Set-Item -Path WSMan:\localhost\Service\Auth\Basic        -Value $true -ErrorAction SilentlyContinue
Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted  -Value $true -ErrorAction SilentlyContinue
Set-Item -Path WSMan:\localhost\Client\TrustedHosts       -Value '*' -Force -ErrorAction SilentlyContinue
# Full token for local admins over the network (fixes "Access is denied" for non-builtin admins)
$p='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null
# Firewall
Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
netsh advfirewall firewall add rule name='WinRM-HTTP-In-5985' dir=in action=allow protocol=TCP localport=5985 2>$null
Write-Output 'WINRDP_WINRM_ENABLED'
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


def bootstrap_oneliner(h: Host) -> str:
    """A single line the operator can paste into an existing RDP/console session."""
    b64 = ps.encode_command(ENABLE_WINRM_PS)
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {b64}"


def provision(h: Host, *, enable_ssh: bool = False, allow_wmi_bootstrap: bool = True) -> ProvisionReport:
    """Make ``h`` manageable, climbing the ladder. Mutates ``h.resolved_transport``."""
    rep = ProvisionReport(alias=h.alias, host=h.host)
    rep.reachable_ports = _scan(h.host)
    rep.bootstrap_oneliner = bootstrap_oneliner(h)

    # local shortcut
    if h.transport == "local" or h.host in ("localhost", "127.0.0.1", "::1"):
        rep.transport = "local"
        rep.success = True
        rep.message = "Local transport; managing this machine directly."
        h.resolved_transport = "local"
        return rep

    # Rung 1: WinRM already up (prefer HTTP unless the host opts into SSL)
    t = _try_winrm(h, h.use_ssl) or _try_winrm(h, not h.use_ssl)
    if t:
        rep.transport = "winrm"
        rep.winrm_enabled = True
        rep.actions.append("winrm-already-reachable")
        _harden_winrm(t, rep)
        rep.success = True
        rep.message = "WinRM reachable; hardened and ready."
        h.resolved_transport = "winrm"
        return rep

    # Rung 2: SSH
    s = _try_ssh(h)
    if s:
        rep.transport = "ssh"
        rep.ssh_enabled = True
        rep.actions.append("ssh-reachable")
        # try to turn WinRM on too for the richer path
        r = s.run_ps(ENABLE_WINRM_PS, timeout=180)
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
    if allow_wmi_bootstrap and rep.reachable_ports["smb"] and rep.reachable_ports["dcom"]:
        try:
            _cold_start_wmi(h, rep)
            if rep.winrm_enabled:
                t3 = _try_winrm(h, False)
                if t3:
                    rep.transport = "winrm"
                    h.resolved_transport = "winrm"
                    _harden_winrm(t3, rep)
                    rep.success = True
                    rep.message = "Cold-started WinRM via SMB+WMI bootstrap."
                    return rep
        except ImportError:
            rep.actions.append("wmi-bootstrap-unavailable (pip install 'winrdp-mcp[bootstrap]')")
        except Exception as e:  # noqa: BLE001
            rep.actions.append(f"wmi-bootstrap-failed: {e}")

    # Rung 4: give the operator the one-liner
    rep.success = False
    rep.message = (
        "Could not auto-enable remoting. Paste bootstrap_oneliner into an RDP/console "
        "session on the box once, then re-run provision()."
    )
    return rep


def _harden_winrm(t: Transport, rep: ProvisionReport) -> None:
    """Re-run the enable script through an existing WinRM session (idempotent)."""
    try:
        r = t.run_ps(ENABLE_WINRM_PS, timeout=120)
        if "WINRDP_WINRM_ENABLED" in r.stdout:
            rep.actions.append("winrm-hardened")
    except Exception as e:  # noqa: BLE001
        rep.actions.append(f"winrm-harden-skipped: {e}")


def _cold_start_wmi(h: Host, rep: ProvisionReport) -> None:
    """Stage the enable script over SMB and launch it fire-and-forget over WMI."""
    smb = SMBFiles(h.host, h.username, h.password, h.domain)
    if not smb.probe():
        raise TransportError("SMB admin share not accessible")
    remote_ps = config.REMOTE_TMP + r"\bootstrap.ps1"
    # UTF-8 BOM so `powershell -File` decodes it correctly (BOM-less UTF-16 is misread).
    smb.write(ENABLE_WINRM_PS.encode("utf-8-sig"), remote_ps)
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
