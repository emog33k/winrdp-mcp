"""High-level ops convenience: one-call health report, baseline hardening, and quick
security-audit reads. These compose what would otherwise be several manual calls."""

from __future__ import annotations

from typing import Optional

from .. import ps


def register(mcp, ctx) -> None:
    @mcp.tool
    def health_report(host: Optional[str] = None) -> dict:
        """One-call box health: OS/uptime, CPU/RAM, per-disk free, top processes, stopped
        auto-start services, recent System/Application errors, pending Windows Updates, and
        Defender status. The "how is this box doing" summary."""
        body = (
            "$os=Get-CimInstance Win32_OperatingSystem;$cs=Get-CimInstance Win32_ComputerSystem;"
            "$cpu=(Get-CimInstance Win32_Processor|Measure-Object LoadPercentage -Average).Average;"
            "$disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|ForEach-Object{@{"
            "drive=$_.DeviceID;free_gb=[math]::Round($_.FreeSpace/1GB,1);size_gb=[math]::Round($_.Size/1GB,1)}});"
            "$top=@(Get-Process|Sort-Object WorkingSet64 -Descending|Select-Object -First 5|ForEach-Object{@{"
            "name=$_.ProcessName;mem_mb=[math]::Round($_.WorkingSet64/1MB,1)}});"
            "$badsvc=@(Get-CimInstance Win32_Service -Filter \"StartMode='Auto' AND State!='Running'\"|"
            "Select-Object -First 15|ForEach-Object{$_.Name});"
            "$errs=@(Get-WinEvent -FilterHashtable @{LogName='System','Application';Level=1,2} -MaxEvents 8 "
            "-ErrorAction SilentlyContinue|ForEach-Object{$m=([string]$_.Message -replace '\\s+',' ');@{"
            "time=$_.TimeCreated.ToString('o');log=$_.LogName;id=$_.Id;src=$_.ProviderName;"
            "msg=$m.Substring(0,[Math]::Min(140,$m.Length))}});"
            "$upd=try{$s=New-Object -ComObject Microsoft.Update.Session;"
            "@($s.CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0').Updates).Count}catch{-1};"
            "$def=Get-MpComputerStatus -ErrorAction SilentlyContinue;"
            "$result=[ordered]@{"
            "hostname=$env:COMPUTERNAME;os=$os.Caption;build=$os.BuildNumber;"
            "uptime_hours=[math]::Round(((Get-Date)-$os.LastBootUpTime).TotalHours,1);"
            "cpu_load_pct=$cpu;mem_total_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1);"
            "mem_free_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);"
            "disks=$disks;top_processes=$top;stopped_auto_services=$badsvc;recent_errors=$errs;"
            "pending_updates=$upd;"
            "defender=if($def){@{realtime=$def.RealTimeProtectionEnabled;"
            "signature_age_days=$def.AntivirusSignatureAge}}else{$null}}"
        )
        return ctx.exec_json(body, host=host, timeout=180)

    @mcp.tool
    def apply_baseline(host: Optional[str] = None, timezone: Optional[str] = None,
                       high_performance: bool = True, disable_sleep: bool = True,
                       long_paths: bool = True, hide_server_manager: bool = True) -> dict:
        """Apply a pleasant server baseline: high-performance power plan, no sleep/hibernate,
        long-path support, hide Server Manager at logon, and optionally set the timezone.
        Makes a fresh box comfortable to work on. Runs elevated."""
        lines = []
        if high_performance:
            lines.append("powercfg /setactive SCHEME_MIN 2>$null")
        if disable_sleep:
            lines.append("powercfg /change standby-timeout-ac 0;powercfg /change standby-timeout-dc 0;"
                         "powercfg /hibernate off 2>$null")
        if long_paths:
            lines.append("Set-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
                         "-Name LongPathsEnabled -Value 1 -Type DWord")
        if hide_server_manager:
            lines.append("Set-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\ServerManager' "
                         "-Name DoNotOpenServerManagerAtLogon -Value 1 -Type DWord -EA SilentlyContinue")
        if timezone:
            if any(c in timezone for c in '"\r\n;`'):
                return {"error": "invalid timezone"}
            lines.append(f"Set-TimeZone -Id {ps.ps_string(timezone)} -EA SilentlyContinue")
        lines.append("$result=@{applied=$true;high_performance=$" + str(high_performance).lower() +
                     ";disable_sleep=$" + str(disable_sleep).lower() + ";long_paths=$" +
                     str(long_paths).lower() + "}")
        return ctx.exec_json(";".join(lines), host=host, elevated=True, timeout=120)

    @mcp.tool
    def whoami_priv(host: Optional[str] = None) -> dict:
        """Current session's user, groups, integrity level, and token privileges — the quick
        'what can this session actually do' check."""
        body = (
            "$id=[Security.Principal.WindowsIdentity]::GetCurrent();"
            "$pr=New-Object Security.Principal.WindowsPrincipal($id);"
            # call the real Windows whoami by full path (a Unix whoami may shadow it on PATH)
            "$w=Join-Path $env:SystemRoot 'System32\\whoami.exe';$lvl='';"
            # integrity via the S-1-16-* SID (locale-independent), mapped to a level name
            "$ml=@(& $w /groups)|Where-Object{$_ -match 'S-1-16-'}|Select-Object -First 1;"
            "if($ml){$sid=[regex]::Match([string]$ml,'S-1-16-\\d+').Value;"
            "$lvl=switch($sid){'S-1-16-4096'{'Low'}'S-1-16-8192'{'Medium'}'S-1-16-12288'{'High'}"
            "'S-1-16-16384'{'System'}default{$sid}}};"
            "$privs=@(& $w /priv|Where-Object{$_ -match '^Se'}|ForEach-Object{(([string]$_) -split '\\s{2,}')[0].Trim()}|Where-Object{$_});"
            "$result=[ordered]@{"
            "user=$id.Name;is_admin=$pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
            "is_system=($id.User.Value -eq 'S-1-5-18');integrity=$lvl;privileges=$privs}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def failed_logons(host: Optional[str] = None, count: int = 25) -> list:
        """Recent failed logon attempts (Security event 4625) — account, source IP, type."""
        body = (
            f"$result=@(Get-WinEvent -FilterHashtable @{{LogName='Security';Id=4625}} -MaxEvents {int(count)} "
            "-ErrorAction SilentlyContinue|ForEach-Object{$x=[xml]$_.ToXml();"
            "$d=@{};$x.Event.EventData.Data|ForEach-Object{$d[$_.Name]=$_.'#text'};@{"
            "time=$_.TimeCreated.ToString('o');account=$d['TargetUserName'];"
            "source_ip=$d['IpAddress'];logon_type=$d['LogonType'];workstation=$d['WorkstationName']}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host, timeout=120))

    @mcp.tool
    def list_open_ports(host: Optional[str] = None, top: int = 100) -> list:
        """Listening TCP ports with the owning process — the box's exposed attack surface."""
        body = (
            f"$result=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|"
            f"Sort-Object LocalPort -Unique|Select-Object -First {int(top)}|ForEach-Object{{"
            "$p=(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue);@{"
            "port=$_.LocalPort;address=$_.LocalAddress.ToString();pid=$_.OwningProcess;"
            "process=if($p){$p.ProcessName}else{'?'};path=if($p){$p.Path}else{$null}}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))
