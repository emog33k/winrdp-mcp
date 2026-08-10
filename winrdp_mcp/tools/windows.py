"""Deeper Windows management: CIM query, features/roles, updates, Defender, env, startup."""

from __future__ import annotations

from typing import Optional

from .. import ps
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
                         host: Optional[str] = None, timeout: int = 900) -> dict:
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
        return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)

    # ------------------------------------------------------------------ Windows Update
    @mcp.tool
    def windows_update(action: str = "check", host: Optional[str] = None,
                       timeout: int = 1800) -> dict:
        """Windows Update via the built-in COM API (no extra module needed).
        action: check (list pending) | install (download+install all)."""
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
            return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)
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
    def defender_scan(host: Optional[str] = None, scan_type: str = "QuickScan",
                      timeout: int = 1800) -> dict:
        """Run a Defender scan. scan_type: QuickScan | FullScan. Runs elevated."""
        V.enum(scan_type, {"QuickScan", "FullScan"}, "scan_type")
        body = f"Start-MpScan -ScanType {scan_type};$result=@{{started=$true;scan_type='{scan_type}'}}"
        return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)

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
