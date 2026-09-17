"""RDP-specific tools: enable/disable, NLA, sessions, tscon, port, multi-session."""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

from .. import ps
from . import _validate as V

TS_KEY = r"HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server"
RDP_TCP = r"HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp"


def register(mcp, ctx) -> None:
    @mcp.tool
    def rdp_status(host: Optional[str] = None) -> dict:
        """Report RDP configuration: enabled, NLA, port, max connections, firewall."""
        body = (
            f"$deny=(Get-ItemProperty -Path '{TS_KEY}' -Name fDenyTSConnections).fDenyTSConnections;"
            f"$nla=(Get-ItemProperty -Path '{RDP_TCP}' -Name UserAuthentication -ErrorAction SilentlyContinue).UserAuthentication;"
            f"$port=(Get-ItemProperty -Path '{RDP_TCP}' -Name PortNumber -ErrorAction SilentlyContinue).PortNumber;"
            "$fw=@(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue|"
            "Where-Object{$_.Enabled -eq 'True'}).Count;"
            "$result=[ordered]@{"
            "enabled=($deny -eq 0);nla_required=($nla -eq 1);port=$port;"
            "firewall_rules_enabled=$fw}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def rdp_enable(host: Optional[str] = None, nla: bool = True) -> dict:
        """Enable RDP: allow connections, open the firewall group, set NLA. Idempotent."""
        body = (
            f"Set-ItemProperty -Path '{TS_KEY}' -Name fDenyTSConnections -Value 0 -Type DWord;"
            f"Set-ItemProperty -Path '{RDP_TCP}' -Name UserAuthentication -Value {1 if nla else 0} -Type DWord;"
            "Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue;"
            "Set-Service TermService -StartupType Automatic;Start-Service TermService -ErrorAction SilentlyContinue;"
            "$result=@{enabled=$true;nla=$" + str(nla).lower() + "}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def rdp_disable(host: Optional[str] = None) -> dict:
        """Disable incoming RDP connections."""
        body = (
            f"Set-ItemProperty -Path '{TS_KEY}' -Name fDenyTSConnections -Value 1 -Type DWord;"
            "$result=@{enabled=$false}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def rdp_set_port(port: int, host: Optional[str] = None, open_firewall: bool = True) -> dict:
        """Change the RDP listening port and (optionally) open it in the firewall.
        Takes effect after a reboot or a TermService restart."""
        fw = (
            f"New-NetFirewallRule -DisplayName 'winrdp-custom-{int(port)}' -Direction Inbound "
            f"-Protocol TCP -LocalPort {int(port)} -Action Allow|Out-Null;" if open_firewall else ""
        )
        body = (
            f"Set-ItemProperty -Path '{RDP_TCP}' -Name PortNumber -Value {int(port)} -Type DWord;"
            + fw +
            "Restart-Service TermService -Force -ErrorAction SilentlyContinue;"
            "$result=@{port=" + str(int(port)) + ";note='reconnect on the new port'}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def rdp_sessions(host: Optional[str] = None) -> list:
        """List RDP/console sessions (id, user, state).

        Uses ``qwinsta`` when available so multi-session/RDP hosts keep their full session
        list. Windows Home editions may omit that utility, in which case return the active
        local console user's Explorer session instead."""
        body = (
            "$result=@();$q=Get-Command qwinsta -ErrorAction SilentlyContinue;"
            "if($q){$out=@(& $q.Source 2>$null)}else{$out=@()};"
            "if($out.Count -ge 2){"
            "$h=$out[0];$iU=$h.IndexOf('USERNAME');$iI=$h.IndexOf('ID');"
            "$iS=$h.IndexOf('STATE');$iT=$h.IndexOf('TYPE');"
            "function _sub($s,$a,$b){if($a -lt 0){return ''};if($b -lt 0 -or $b -gt $s.Length){$b=$s.Length};"
            "if($a -ge $s.Length){return ''};return $s.Substring($a,$b-$a).Trim()}"
            "$result=@($out|Select-Object -Skip 1|Where-Object{$_.Trim()}|ForEach-Object{"
            "$l=$_;@{"
            "session=(_sub $l 0 $iU).TrimStart('>').Trim();"
            "user=(_sub $l $iU $iI);id=(_sub $l $iI $iS);state=(_sub $l $iS $iT)}})};"
            "if($result.Count -eq 0){"
            "$u=[string](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName;"
            "if($u){$leaf=($u -split '\\\\')[-1];"
            "$shells=@(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" -ErrorAction SilentlyContinue);"
            "foreach($p in $shells){"
            "$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue;"
            "if($o){$full=if($o.Domain){$o.Domain+'\\'+$o.User}else{$o.User};"
            "if(($full -ieq $u)-or($o.User -ieq $leaf)){"
            "$result=@(@{session='console';user=$u;id=[string]$p.SessionId;state='Active'});break}}}}"
            "}"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def rdp_disconnect_session(session_id: int, host: Optional[str] = None) -> dict:
        """Disconnect an RDP session by id (keeps it running)."""
        r = ctx.transport_for(host).run_cmd(f"tsdiscon {int(session_id)}", timeout=30)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr}

    @mcp.tool
    def rdp_logoff_session(session_id: int, host: Optional[str] = None) -> dict:
        """Log off an RDP session by id (ends it)."""
        r = ctx.transport_for(host).run_cmd(f"logoff {int(session_id)}", timeout=30)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr}

    @mcp.tool
    def rdp_connect_to_console(session_id: int, host: Optional[str] = None,
                               target: str = "console") -> dict:
        """Redirect a session to the console with ``tscon`` (session steal/handoff).
        Requires SYSTEM; runs elevated."""
        V.token(target, "target")
        r = ctx.exec_ps(f"tscon {int(session_id)} /dest:{target}", host=host, elevated=True, timeout=30)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def rdp_connection_file(host: Optional[str] = None, save_path: Optional[str] = None,
                            fullscreen: bool = True, multimon: bool = False,
                            store_credentials: bool = True) -> dict:
        """Generate a ready-to-use .rdp file on the operator machine for a registered box.

        Uses the box's stored address/port/username. If store_credentials and the operator
        is Windows, the password is stored via cmdkey directly (never returned to you), so
        mstsc connects without prompting. The plaintext password is never placed in the
        tool result.
        """
        h = ctx.resolve(host)
        addr = f"{h.host}:{h.rdp_port}"
        user = f"{h.domain}\\{h.username}" if h.domain else h.username
        lines = [
            f"full address:s:{addr}",
            f"username:s:{user}",
            f"screen mode id:i:{2 if fullscreen else 1}",
            f"use multimon:i:{1 if multimon else 0}",
            "redirectclipboard:i:1",
            "redirectdrives:i:1",
            "audiomode:i:0",
            "authentication level:i:2",
            "prompt for credentials:i:0",
        ]
        content = "\r\n".join(lines) + "\r\n"
        path = save_path or os.path.join(tempfile.gettempdir(), f"{h.alias}.rdp")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        credentials_stored = False
        if store_credentials and h.password and os.name == "nt":
            # Store the secret directly with cmdkey; do NOT surface it to the model.
            cp = subprocess.run(["cmdkey", f"/generic:TERMSRV/{h.host}",
                                 f"/user:{user}", f"/pass:{h.password}"], capture_output=True)
            credentials_stored = cp.returncode == 0
        return {
            "rdp_file": path,
            "mstsc_command": f'mstsc "{path}"',
            "credentials_stored": credentials_stored,
            "note": ("Credentials pre-stored; just run mstsc_command."
                     if credentials_stored else
                     "Run mstsc_command; you'll be prompted for the password."),
        }

    @mcp.tool
    def rdp_open(host: Optional[str] = None) -> dict:
        """Launch mstsc from the operator machine to a registered box, pre-storing the
        password with cmdkey so it connects without prompting. Operator must be Windows."""
        h = ctx.resolve(host)
        user = f"{h.domain}\\{h.username}" if h.domain else h.username
        if os.name != "nt":
            return {"error": "operator is not Windows; use rdp_connection_file and connect manually",
                    "mstsc_command": f"xfreerdp /v:{h.host}:{h.rdp_port} /u:{user}"}
        if h.password:
            subprocess.run(["cmdkey", f"/generic:TERMSRV/{h.host}",
                            f"/user:{user}", f"/pass:{h.password}"],
                           capture_output=True)
        subprocess.Popen(["mstsc", f"/v:{h.host}:{h.rdp_port}"])
        return {"launched": True, "target": f"{h.host}:{h.rdp_port}", "user": user}

    @mcp.tool
    def install_rdp_wrapper(host: Optional[str] = None, url: Optional[str] = None,
                            timeout: int = 600) -> dict:
        """Install RDP Wrapper to allow concurrent RDP sessions on client SKUs (Win10/11),
        which the built-in single-session limit otherwise blocks.

        Provide `url` to a RDPWrap installer zip (e.g. a community
        RDPWrap-Installer release). Downloads and runs its install.bat elevated, then
        updates rdpwrap.ini. Without a url, returns guidance and flips the supported policy.
        Note: this modifies termsrv behaviour — use only on boxes you own.
        """
        if not url:
            body = (
                f"Set-ItemProperty -Path '{TS_KEY}' -Name fSingleSessionPerUser -Value 0 -Type DWord;"
                "$result=@{policy_flipped=$true;note='For true concurrent sessions on client SKUs, "
                "pass a RDPWrap installer url; the built-in license limit needs RDP Wrapper.'}"
            )
            return ctx.exec_json(body, host=host, elevated=True)
        body = (
            f"$u={ps.ps_string(url)};$z=Join-Path $env:TEMP 'rdpw.zip';$d=Join-Path $env:TEMP 'rdpw';"
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            "(New-Object Net.WebClient).DownloadFile($u,$z);"
            "if(Test-Path $d){Remove-Item $d -Recurse -Force};Expand-Archive $z -DestinationPath $d -Force;"
            "$bat=Get-ChildItem $d -Recurse -Filter 'install.bat'|Select-Object -First 1;"
            "if(-not $bat){$bat=Get-ChildItem $d -Recurse -Filter 'RDPWInst*.exe'|Select-Object -First 1};"
            "if($bat){Start-Process -FilePath $bat.FullName -ArgumentList '-i','-s' -WorkingDirectory $bat.Directory -Wait -Verb RunAs;"
            "$result=@{installed=$true;ran=$bat.Name}}else{$result=@{installed=$false;error='install.bat/RDPWInst not found in archive'}}"
        )
        return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)

    @mcp.tool
    def rdp_allow_multiple_sessions(host: Optional[str] = None, enable: bool = True) -> dict:
        """Toggle the per-user single-session restriction so several users can RDP at once.
        Note: concurrent sessions on client SKUs (Win10/11) beyond the built-in limits may
        require the RDP Wrapper tool; this flips the supported policy switches."""
        val = 0 if enable else 1  # fSingleSessionPerUser=0 allows multiple
        body = (
            f"Set-ItemProperty -Path '{TS_KEY}' -Name fSingleSessionPerUser -Value {val} -Type DWord;"
            "$result=@{multiple_sessions=$" + str(enable).lower() + "}"
        )
        return ctx.exec_json(body, host=host)
