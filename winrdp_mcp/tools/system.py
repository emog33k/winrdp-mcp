"""System tools: shell execution, system info, performance, event log, screenshot, power."""

from __future__ import annotations

import base64
from typing import Optional

from .. import ps

try:  # rich image return when available
    from fastmcp.utilities.types import Image
except Exception:  # pragma: no cover
    Image = None


def register(mcp, ctx) -> None:
    @mcp.tool
    def run_powershell(
        script: str,
        host: Optional[str] = None,
        elevated: bool = False,
        as_user: bool = False,
        detach: bool = False,
        loopback: bool = False,
        timeout: int = 120,
    ) -> dict:
        """Run a PowerShell script on a box. The universal escape hatch — anything
        Windows can do, this can do.

        elevated=True runs it with a full (unfiltered) token via a one-shot Scheduled Task.
        as_user=True runs it inside the interactive RDP user's desktop session (for GUI).
        detach=True launches it fire-and-forget in a Scheduled Task so a background process
        (e.g. an HTTP server) SURVIVES the session close — a normal run/Start-Process dies
        with the WinRM Job Object; returns the task + log paths instead of output.
        loopback=True routes through a Scheduled Task so the script can reach 127.0.0.1
        (the WinRM network-logon token blocks outbound loopback; the task's logon does not).
        Returns {stdout, stderr, rc}.
        """
        if detach:
            return ctx.run_detached(script, host=host)
        r = ctx.exec_ps(script, host=host, elevated=elevated, as_user=as_user,
                        loopback=loopback, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def run_cmd(command: str, host: Optional[str] = None, timeout: int = 120) -> dict:
        """Run a cmd.exe command line on a box. Returns {stdout, stderr, rc}."""
        t = ctx.transport_for(host)
        r = t.run_cmd(command, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def system_info(host: Optional[str] = None) -> dict:
        """Comprehensive box facts: OS name/version/build/edition, hostname, domain,
        uptime, CPU, memory, disks, IP addresses, and whether it is a server SKU."""
        body = (
            "$os=Get-CimInstance Win32_OperatingSystem;"
            "$cs=Get-CimInstance Win32_ComputerSystem;"
            "$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1;"
            "$disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|ForEach-Object{"
            "@{drive=$_.DeviceID;size_gb=[math]::Round($_.Size/1GB,1);free_gb=[math]::Round($_.FreeSpace/1GB,1)}});"
            "$ips=@(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True'|"
            "ForEach-Object{$_.IPAddress}|Where-Object{$_ -and $_ -notmatch ':'});"
            "$result=[ordered]@{"
            "hostname=$env:COMPUTERNAME;domain=$cs.Domain;"
            "os_caption=$os.Caption;os_version=$os.Version;os_build=$os.BuildNumber;"
            "os_arch=$os.OSArchitecture;product_type=$os.ProductType;"  # 1=work,2=DC,3=server
            "is_server=($os.ProductType -ne 1);"
            "install_date=$os.InstallDate.ToString('o');"
            "last_boot=$os.LastBootUpTime.ToString('o');"
            "uptime_hours=[math]::Round(((Get-Date)-$os.LastBootUpTime).TotalHours,1);"
            "cpu=$cpu.Name;cpu_cores=$cs.NumberOfLogicalProcessors;"
            "mem_total_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1);"
            "mem_free_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);"
            "disks=$disks;ip_addresses=$ips;"
            "logged_on_user=$cs.UserName}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def performance(host: Optional[str] = None, top: int = 8) -> dict:
        """Live CPU %, memory, per-disk free space, and the top processes by memory."""
        body = (
            "$os=Get-CimInstance Win32_OperatingSystem;"
            "$cpu=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average;"
            f"$procs=@(Get-Process|Sort-Object WorkingSet64 -Descending|Select-Object -First {int(top)}|"
            "ForEach-Object{@{name=$_.ProcessName;pid=$_.Id;mem_mb=[math]::Round($_.WorkingSet64/1MB,1)}});"
            "$result=[ordered]@{"
            "cpu_load_pct=$cpu;"
            "mem_total_mb=[math]::Round($os.TotalVisibleMemorySize/1KB,0);"
            "mem_free_mb=[math]::Round($os.FreePhysicalMemory/1KB,0);"
            "mem_used_pct=[math]::Round(100-($os.FreePhysicalMemory/$os.TotalVisibleMemorySize*100),1);"
            "top_processes=$procs}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def event_log(
        log: str = "System",
        count: int = 50,
        level: Optional[str] = None,
        source: Optional[str] = None,
        host: Optional[str] = None,
    ) -> list:
        """Read recent Windows event-log entries. log: System|Application|Security|...
        level: Error|Warning|Information|Critical (optional filter)."""
        level_map = {"critical": 1, "error": 2, "warning": 3, "information": 4, "verbose": 5}
        filt = [f"LogName={ps.ps_string(log)}"]
        if level and level.lower() in level_map:
            filt.append(f"Level={level_map[level.lower()]}")
        if source:
            filt.append(f"ProviderName={ps.ps_string(source)}")
        hashtable = "@{" + ";".join(filt) + "}"
        body = (
            f"$result=@(Get-WinEvent -FilterHashtable {hashtable} -MaxEvents {int(count)} "
            "-ErrorAction SilentlyContinue|ForEach-Object{@{"
            "time=$_.TimeCreated.ToString('o');id=$_.Id;level=$_.LevelDisplayName;"
            "provider=$_.ProviderName;message=($_.Message -replace '\\s+',' ')}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def screenshot(host: Optional[str] = None, monitor: int = 0):
        """Capture the interactive RDP desktop of a box and return it as an image.

        Runs inside the logged-on user's session so it sees the real desktop. Requires an
        active/connected RDP session on the box.
        """
        cap = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
            "$ms=New-Object IO.MemoryStream;"
            "$bmp.Save($ms,[Drawing.Imaging.ImageFormat]::Png);"
            "[Convert]::ToBase64String($ms.ToArray())"
        )
        r = ctx.exec_ps(cap, host=host, as_user=True, timeout=60)
        b64 = "".join(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
        if not b64:
            return {"error": "no image captured (is an RDP session connected?)", "stderr": r.stderr}
        raw = base64.b64decode(b64)
        if Image is not None:
            return Image(data=raw, format="png")
        return {"image_base64": b64, "format": "png", "bytes": len(raw)}

    @mcp.tool
    def reboot(host: Optional[str] = None, delay_seconds: int = 5, force: bool = True) -> dict:
        """Reboot a box after a short delay."""
        flag = "/f " if force else ""
        r = ctx.transport_for(host).run_cmd(f"shutdown /r {flag}/t {int(delay_seconds)}", timeout=30)
        return {"scheduled": r.ok, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def power_action(action: str = "shutdown", host: Optional[str] = None,
                     delay_seconds: int = 5) -> dict:
        """Power control. action: shutdown | reboot | logoff | cancel."""
        cmds = {
            "shutdown": f"shutdown /s /f /t {int(delay_seconds)}",
            "reboot": f"shutdown /r /f /t {int(delay_seconds)}",
            "logoff": "shutdown /l",
            "cancel": "shutdown /a",
        }
        if action not in cmds:
            return {"error": f"unknown action '{action}'"}
        r = ctx.transport_for(host).run_cmd(cmds[action], timeout=30)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
