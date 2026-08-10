"""MCP Resources — bounded, read-only, application-controlled context.

Resources let a client hand the model small, cacheable facts without spending a tool call:
the host inventory, and a compact per-host summary. Kept deliberately small (no full logs
or configs) so they inform rather than overwhelm.
"""

from __future__ import annotations

import json


def register(mcp, ctx) -> None:
    @mcp.resource("winrdp://hosts")
    def hosts_resource() -> str:
        """The registered host inventory (passwords redacted) and which host is active."""
        data = {
            "active": ctx.vault.active.alias if ctx.vault.active else None,
            "hosts": [
                {"alias": h.alias, "host": h.host, "username": h.username,
                 "transport": h.transport, "tags": h.tags,
                 "resolved_transport": h.resolved_transport}
                for h in ctx.vault.all()
            ],
        }
        return json.dumps(data, indent=2)

    @mcp.resource("winrdp://host/{alias}/info")
    def host_info(alias: str) -> str:
        """A compact live summary of one box (OS, build, CPU/RAM, disks, uptime)."""
        h = ctx.vault.get(alias)
        if not h:
            return json.dumps({"error": f"unknown host alias '{alias}'"})
        try:
            body = (
                "$os=Get-CimInstance Win32_OperatingSystem;$cs=Get-CimInstance Win32_ComputerSystem;"
                "$result=[ordered]@{hostname=$env:COMPUTERNAME;os=$os.Caption;build=$os.BuildNumber;"
                "arch=$os.OSArchitecture;"
                "uptime_hours=[math]::Round(((Get-Date)-$os.LastBootUpTime).TotalHours,1);"
                "cpu_cores=$cs.NumberOfLogicalProcessors;"
                "mem_total_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1);"
                "mem_free_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);"
                "disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|ForEach-Object{@{"
                "drive=$_.DeviceID;free_gb=[math]::Round($_.FreeSpace/1GB,1)}})}"
            )
            info = ctx.exec_json(body, host=alias, timeout=60)
            return json.dumps(info, indent=2, default=str)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"alias": alias, "error": str(e)})
