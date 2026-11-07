"""Deeper Windows management: CIM query, features/roles, updates, Defender, env, startup."""

from __future__ import annotations

from typing import Optional

from .. import ps
from ..progress import Context, Progress
from . import _validate as V


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ CIM/WMI
    @mcp.tool
    def cim_query(query: Optional[str] = None, class_name: Optional[str] = None,
                  namespace: str = "root/cimv2", host: Optional[str] = None,
                  top: int = 100) -> list:
        """Run an arbitrary CIM/WMI query — the escape hatch for any WMI data.
        Provide a WQL `query` (e.g. 'SELECT * FROM Win32_BIOS') or a `class_name`.
        Note: values are flattened to strings, so array-valued properties (e.g. IPAddress)
        collapse to a space-joined string and nested objects render as type names."""
        if query:
            src = f"-Query {ps.ps_string(query)}"
        elif class_name:
            src = f"-ClassName {ps.ps_string(class_name)}"
        else:
            return [{"error": "provide query or class_name"}]
        body = (
            f"$result=@(Get-CimInstance {src} -Namespace {ps.ps_string(namespace)} "
            f"-ErrorAction Stop|Select-Object -First {int(top)}|ForEach-Object{{"
            "$h=[ordered]@{};$_.CimInstanceProperties|Where-Object{$null -ne $_.Value}|"
            "ForEach-Object{$h[$_.Name]=[string]$_.Value};$h})"
        )
        return ps.as_list(ctx.exec_json(body, host=host, timeout=120))

    # ------------------------------------------------------------------ features / roles
    @mcp.tool
    def windows_features(action: str = "list", name: Optional[str] = None,
                         host: Optional[str] = None, timeout: int = 900,
                         mcp_ctx: Context | None = None) -> dict:
        """Manage Windows features/roles. action: list | install | remove.

        Auto-detects Server (Install-WindowsFeature) vs client (Enable/Disable optional
        feature). Examples: 'Web-Server' (IIS on Server), 'Microsoft-Hyper-V',
        'Containers'. Install/remove run elevated and may require a reboot.
        """
        detect = "$srv=(Get-CimInstance Win32_OperatingSystem).ProductType -ne 1;"
        if action == "list":
            body = (
                detect +
                "if($srv){$result=@(Get-WindowsFeature|Where-Object{$_.InstallState -ne $null}|"
                "ForEach-Object{@{name=$_.Name;display=$_.DisplayName;state=$_.InstallState.ToString()}})}"
                "else{$result=@(Get-WindowsOptionalFeature -Online|ForEach-Object{@{"
                "name=$_.FeatureName;state=$_.State.ToString()}})}"
            )
            return {"features": ps.as_list(ctx.exec_json(body, host=host, timeout=timeout))}
        if not name:
            return {"error": "provide feature name"}
        if action == "install":
            body = (
                detect +
                f"if($srv){{$r=Install-WindowsFeature -Name {ps.ps_string(name)} -IncludeManagementTools;"
                "$result=@{success=$r.Success;restart_needed=$r.RestartNeeded.ToString()}}"
                f"else{{$r=Enable-WindowsOptionalFeature -Online -FeatureName {ps.ps_string(name)} -All -NoRestart;"
                "$result=@{online=$true;restart_needed=$r.RestartNeeded}}"
            )
        elif action == "remove":
            body = (
                detect +
                f"if($srv){{$r=Uninstall-WindowsFeature -Name {ps.ps_string(name)};"
                "$result=@{success=$r.Success;restart_needed=$r.RestartNeeded.ToString()}}"
                f"else{{$r=Disable-WindowsOptionalFeature -Online -FeatureName {ps.ps_string(name)} -NoRestart;"
                "$result=@{online=$true;restart_needed=$r.RestartNeeded}}"
            )
        else:
            return {"error": "action must be list|install|remove"}
        prog = Progress(mcp_ctx, 100)
        prog.report(15, f"{action} Windows feature '{name}' (can take minutes)")
        out = ctx.run_long_json(body, host=host, timeout=timeout,
                                progress=prog.heartbeat(f"{action} feature {name}"))
        prog.report(100, f"feature {action} finished")
        return out

    # ------------------------------------------------------------------ Windows Update
    @mcp.tool
    def windows_update(action: str = "check", host: Optional[str] = None,
                       timeout: int = 1800, mcp_ctx: Context | None = None) -> dict:
        """Windows Update via the built-in COM API (no extra module needed).
        action: check (list pending) | install (download+install all)."""
        p = Progress(mcp_ctx, 100)
        search = (
            "$s=New-Object -ComObject Microsoft.Update.Session;"
            "$u=$s.CreateUpdateSearcher();"
            "$r=$u.Search('IsInstalled=0 and IsHidden=0');"
        )
        if action == "check":
            body = (
                search +
                "$result=@(@($r.Updates)|ForEach-Object{@{title=$_.Title;"
                "kb=($_.KBArticleIDs -join ',');severity=[string]$_.MsrcSeverity}})"
            )
            return {"pending": ps.as_list(ctx.exec_json(body, host=host, elevated=True, timeout=timeout))}
        if action == "install":
            p.report(10, "searching, downloading and installing Windows updates (can take many minutes)")
            body = (
                search +
                "$col=New-Object -ComObject Microsoft.Update.UpdateColl;"
                "@($r.Updates)|ForEach-Object{[void]$col.Add($_)};"
                "if($col.Count -eq 0){$result=@{installed=0;note='nothing pending'}}else{"
                "$d=$s.CreateUpdateDownloader();$d.Updates=$col;[void]$d.Download();"
                "$i=$s.CreateUpdateInstaller();$i.Updates=$col;$res=$i.Install();"
                "$result=@{installed=$col.Count;result_code=$res.ResultCode;"
                "reboot_required=$res.RebootRequired}}"
            )
            out = ctx.run_long_json(body, host=host, timeout=timeout,
                                    progress=p.heartbeat("installing Windows updates"))
            p.report(100, "Windows Update finished")
            return out
        return {"error": "action must be check|install"}

    @mcp.tool
    def hotfixes(host: Optional[str] = None, top: int = 40) -> list:
        """List installed hotfixes/updates (KB, description, install date)."""
        body = (
            f"$result=@(Get-HotFix|Sort-Object InstalledOn -Descending|Select-Object -First {int(top)}|"
            "ForEach-Object{@{kb=$_.HotFixID;type=$_.Description;"
            "installed=if($_.InstalledOn){$_.InstalledOn.ToString('o')}else{$null}}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    # ------------------------------------------------------------------ Defender
    @mcp.tool
    def defender_status(host: Optional[str] = None) -> dict:
        """Microsoft Defender status: realtime protection, signature age, last scan."""
        body = (
            "$m=Get-MpComputerStatus -ErrorAction SilentlyContinue;"
            "$result=if($m){@{"
            "realtime=$m.RealTimeProtectionEnabled;antivirus_enabled=$m.AntivirusEnabled;"
            "signature_version=$m.AntivirusSignatureVersion;"
            "signature_age_days=$m.AntivirusSignatureAge;"
            "last_quick_scan=[string]$m.QuickScanEndTime;tamper_protection=$m.IsTamperProtected}}"
            "else{@{available=$false}}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def defender_realtime(enable: bool, host: Optional[str] = None) -> dict:
        """Enable or disable Defender realtime protection. Runs elevated. (Tamper
        Protection may block disabling; disable it in the UI first if needed.)"""
        val = "$false" if enable else "$true"  # DisableRealtimeMonitoring is inverted
        body = f"Set-MpPreference -DisableRealtimeMonitoring {val};$result=@{{realtime_enabled=${str(enable).lower()}}}"
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_exclusion_add(path: str, host: Optional[str] = None) -> dict:
        """Add a path to Defender's exclusion list. Runs elevated."""
        body = f"Add-MpPreference -ExclusionPath {ps.ps_string(path)};$result=@{{ok=$true;path={ps.ps_string(path)}}}"
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_exclusions(host: Optional[str] = None) -> dict:
        """List Defender exclusions: paths, extensions, processes and IP addresses."""
        body = (
            "$p=Get-MpPreference -ErrorAction Stop;"
            "$result=@{paths=@($p.ExclusionPath);extensions=@($p.ExclusionExtension);"
            "processes=@($p.ExclusionProcess);ips=@($p.ExclusionIpAddress)}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_exclusion_remove(path: Optional[str] = None,
                                  extension: Optional[str] = None,
                                  process: Optional[str] = None,
                                  host: Optional[str] = None) -> dict:
        """Remove a Defender exclusion by path, extension or process. Provide at least one."""
        if not (path or extension or process):
            return {"error": "provide path, extension or process"}
        lines = []
        removed = []
        if path:
            lines.append(f"Remove-MpPreference -ExclusionPath {ps.ps_string(path)}")
            removed.append(f"path:{path}")
        if extension:
            lines.append(f"Remove-MpPreference -ExclusionExtension {ps.ps_string(extension)}")
            removed.append(f"extension:{extension}")
        if process:
            lines.append(f"Remove-MpPreference -ExclusionProcess {ps.ps_string(process)}")
            removed.append(f"process:{process}")
        lines.append("$result=@{ok=$true;removed=@(" +
                     ",".join(ps.ps_string(r) for r in removed) + ")}")
        return ctx.exec_json(";".join(lines), host=host, elevated=True)

    @mcp.tool
    def defender_quarantine(host: Optional[str] = None) -> dict:
        """List Defender detections and threats (quarantine history): name, resources,
        detection time and status."""
        body = (
            "$names=@{};Get-MpThreat -ErrorAction SilentlyContinue|"
            "ForEach-Object{$names[[int64]$_.ThreatID]=$_.ThreatName};"
            "$dets=@(Get-MpThreatDetection -ErrorAction SilentlyContinue|ForEach-Object{@{"
            "threat_id=[int64]$_.ThreatID;"
            "threat_name=if($names.ContainsKey([int64]$_.ThreatID)){"
            "$names[[int64]$_.ThreatID]}else{$null};"
            "detection_id=[string]$_.DetectionID;"
            "initial_time=if($_.InitialDetectionTime){$_.InitialDetectionTime.ToString('o')}"
            "else{$null};resources=@($_.Resources);"
            "action=if($_.ActionSuccess){'success'}else{'failed'}}});"
            "$threats=@(Get-MpThreat -ErrorAction SilentlyContinue|ForEach-Object{@{"
            "threat_id=[int64]$_.ThreatID;threat_name=$_.ThreatName;"
            "resources=@($_.Resources);severity=[int]$_.SeverityID;"
            "detected=if($_.DetectionTime){$_.DetectionTime.ToString('o')}else{$null}}});"
            "$result=@{detections=$dets;threats=$threats}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_quarantine_restore(threat_name: str = "", host: Optional[str] = None,
                                    all_items: bool = False) -> dict:
        """Restore quarantined items via MpCmdRun -Restore. Provide threat_name, or
        all_items=True to restore everything Defender quarantined."""
        if not threat_name and not all_items:
            return {"error": "provide threat_name or all_items=True"}
        if threat_name:
            V.obj_name(threat_name, "threat_name")
        args = "-Restore -All" if all_items else f"-Restore -Name {ps.ps_string(threat_name)}"
        body = (
            "$mp=@((Join-Path $env:ProgramFiles 'Windows Defender\\MpCmdRun.exe'),"
            "(Get-ChildItem \"$env:ProgramData\\Microsoft\\Windows Defender\\Platform\\*\\"
            "MpCmdRun.exe\" -ErrorAction SilentlyContinue|Sort-Object FullName -Descending|"
            "Select-Object -First 1 -ExpandProperty FullName))|"
            "Where-Object{$_ -and (Test-Path $_)}|Select-Object -First 1;"
            "if(-not $mp){throw 'MpCmdRun.exe not found'};"
            f"$out=& $mp {args} 2>&1|Out-String;$rc=$LASTEXITCODE;"
            "$result=@{ok=($rc -eq 0);rc=$rc;output=$out}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_history_clear(host: Optional[str] = None) -> dict:
        """Clear Defender threat history / quarantine (`Remove-MpThreat`).

        Tamper Protection blocks this. The tool checks `IsTamperProtected` first and
        returns a clear error instead of a cmdlet failure.
        """
        body = (
            "$s=Get-MpComputerStatus -ErrorAction SilentlyContinue;"
            "if($s -and $s.IsTamperProtected){"
            "$result=@{ok=$false;tamper_protected=$true;error='Tamper Protection is enabled; "
            "history/quarantine cannot be cleared. Disable it in Windows Security > Virus & "
            "threat protection > Manage settings first.'}}"
            "else{try{$before=@(Get-MpThreatDetection -ErrorAction SilentlyContinue).Count;"
            "Remove-MpThreat -ErrorAction Stop;"
            "$after=@(Get-MpThreatDetection -ErrorAction SilentlyContinue).Count;"
            "$result=@{ok=$true;tamper_protected=$false;before=$before;after=$after;"
            "removed=[math]::Max(0,$before-$after)}}"
            "catch{$result=@{ok=$false;tamper_protected=($s -and $s.IsTamperProtected);"
            "error=$_.Exception.Message}}}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def defender_scan(host: Optional[str] = None, scan_type: str = "QuickScan",
                      timeout: int = 1800, mcp_ctx: Context | None = None) -> dict:
        """Run a Defender scan. scan_type: QuickScan | FullScan. Runs elevated."""
        V.enum(scan_type, {"QuickScan", "FullScan"}, "scan_type")
        prog = Progress(mcp_ctx, 100)
        prog.report(10, f"running Defender {scan_type} (FullScan can take a long time)")
        body = f"Start-MpScan -ScanType {scan_type};$result=@{{started=$true;scan_type='{scan_type}'}}"
        out = ctx.run_long_json(body, host=host, timeout=timeout,
                                progress=prog.heartbeat(f"Defender {scan_type}"))
        prog.report(100, f"Defender {scan_type} finished")
        return out

    # ------------------------------------------------------------------ environment
    @mcp.tool
    def env_get(host: Optional[str] = None, scope: str = "Machine") -> dict:
        """Read environment variables. scope: Machine | User | Process."""
        V.enum(scope, {"Machine", "User", "Process"}, "scope")
        body = (
            f"$d=[Environment]::GetEnvironmentVariables('{scope}');"
            "$h=[ordered]@{};$d.Keys|Sort-Object|ForEach-Object{$h[$_]=$d[$_]};$result=$h"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def env_set(name: str, value: str, host: Optional[str] = None, scope: str = "Machine") -> dict:
        """Set an environment variable. scope: Machine | User. Machine scope runs elevated."""
        V.enum(scope, {"Machine", "User", "Process"}, "scope")
        body = (
            f"[Environment]::SetEnvironmentVariable({ps.ps_string(name)},{ps.ps_string(value)},'{scope}');"
            "$result=@{ok=$true;name=" + ps.ps_string(name) + f";scope='{scope}'}}"
        )
        return ctx.exec_json(body, host=host, elevated=(scope == "Machine"))

    # ------------------------------------------------------------------ startup / autoruns
    @mcp.tool
    def list_startup(host: Optional[str] = None) -> list:
        """List autostart entries (Run keys, Startup folders, WMI startup commands)."""
        body = (
            "$result=@(Get-CimInstance Win32_StartupCommand -ErrorAction SilentlyContinue|ForEach-Object{@{"
            "name=$_.Name;command=$_.Command;location=$_.Location;user=$_.User}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))
