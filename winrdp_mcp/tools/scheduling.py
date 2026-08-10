"""Scheduling & persistence — run commands on a schedule, at startup, or as a resilient
auto-restarting service.
"""

from __future__ import annotations

from typing import Optional

from .. import ps
from ..config import REMOTE_TOOLS
from . import _validate as V

_NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"
_NSSM_EXE = REMOTE_TOOLS + r"\nssm.exe"


def register(mcp, ctx) -> None:
    @mcp.tool
    def schedule_command(name: str, command: str, host: Optional[str] = None,
                         schedule: str = "DAILY", start_time: str = "09:00",
                         every: Optional[int] = None, run_as: str = "SYSTEM",
                         highest: bool = True) -> dict:
        """Create a recurring scheduled task on a box.

        schedule: MINUTE | HOURLY | DAILY | WEEKLY | ONLOGON | ONSTART. Use `every` as the
        interval modifier for MINUTE/HOURLY (e.g. schedule='MINUTE', every=15). run_as
        SYSTEM needs no password; highest=True runs with a full token.
        """
        V.obj_name(name, "name")
        V.obj_name(run_as, "run_as")
        V.enum(schedule, {"MINUTE", "HOURLY", "DAILY", "WEEKLY", "ONLOGON", "ONSTART"}, "schedule")
        rl = "/RL HIGHEST " if highest else ""
        mo = f"/MO {int(every)} " if every and schedule in ("MINUTE", "HOURLY", "DAILY", "WEEKLY") else ""
        st = f"/ST {start_time} " if schedule in ("DAILY", "WEEKLY", "HOURLY", "MINUTE") else ""
        V.token(start_time, "start_time")
        cmd = (f'schtasks /Create /F /TN "{name}" /TR "{command}" /SC {schedule} '
               f'{mo}{st}{rl}/RU "{run_as}"')
        r = ctx.transport_for(host).run_cmd(cmd, timeout=60)
        return {"ok": r.ok, "task": name, "schedule": schedule, "stdout": r.stdout,
                "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def run_at_startup(name: str, command: str, host: Optional[str] = None,
                       at: str = "boot", run_as: str = "SYSTEM", highest: bool = True) -> dict:
        """Register a command to run automatically at boot or logon (scheduled task).
        at: boot (ONSTART) | logon (ONLOGON)."""
        V.obj_name(name, "name")
        V.obj_name(run_as, "run_as")
        sc = "ONSTART" if at == "boot" else "ONLOGON"
        rl = "/RL HIGHEST " if highest else ""
        cmd = f'schtasks /Create /F /TN "{name}" /TR "{command}" /SC {sc} {rl}/RU "{run_as}"'
        r = ctx.transport_for(host).run_cmd(cmd, timeout=60)
        return {"ok": r.ok, "task": name, "trigger": sc, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def persist_as_service(name: str, command: str, host: Optional[str] = None,
                           display_name: Optional[str] = None, autostart: bool = True,
                           start_now: bool = True, timeout: int = 300) -> dict:
        """Install a command as a resilient auto-restarting Windows service via NSSM.

        Unlike a scheduled task, this keeps the command running and restarts it if it exits.
        NSSM is downloaded to the tool cache on first use. `command` runs under cmd.exe so
        any command line works. Runs elevated.
        """
        V.obj_name(name, "name")
        disp = display_name or name
        start_type = "SERVICE_AUTO_START" if autostart else "SERVICE_DEMAND_START"
        body = (
            ps.ensure_remote_dirs() + ";"
            # ensure nssm.exe present
            f"if(-not(Test-Path {ps.ps_string(_NSSM_EXE)})){{"
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            "$z=Join-Path $env:TEMP 'nssm.zip';$d=Join-Path $env:TEMP 'nssm_x';"
            f"(New-Object Net.WebClient).DownloadFile({ps.ps_string(_NSSM_URL)},$z);"
            "if(Test-Path $d){Remove-Item $d -Recurse -Force};Expand-Archive $z -DestinationPath $d -Force;"
            "$src=Get-ChildItem $d -Recurse -Filter nssm.exe|Where-Object{$_.FullName -match 'win64'}|Select-Object -First 1;"
            "if(-not $src){$src=Get-ChildItem $d -Recurse -Filter nssm.exe|Select-Object -First 1};"
            f"Copy-Item $src.FullName {ps.ps_string(_NSSM_EXE)} -Force;Remove-Item $z,$d -Recurse -Force -EA SilentlyContinue}};"
            # (re)install the service
            f"$n={ps.ps_string(_NSSM_EXE)};$svc={ps.ps_string(name)};"
            "& $n stop $svc 2>$null|Out-Null;& $n remove $svc confirm 2>$null|Out-Null;"
            "& $n install $svc $env:ComSpec 2>$null|Out-Null;"
            f"& $n set $svc AppParameters {ps.ps_string('/c ' + command)}|Out-Null;"
            f"& $n set $svc DisplayName {ps.ps_string(disp)}|Out-Null;"
            f"& $n set $svc Start {start_type}|Out-Null;"
            + ("& $n start $svc 2>$null|Out-Null;" if start_now else "") +
            "$s=Get-Service -Name $svc -ErrorAction SilentlyContinue;"
            "$result=@{ok=$true;service=$svc;status=if($s){$s.Status.ToString()}else{'unknown'};"
            "start=" + ("'auto'" if autostart else "'demand'") + "}"
        )
        return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)

    @mcp.tool
    def unpersist_service(name: str, host: Optional[str] = None) -> dict:
        """Stop and remove a service previously created with persist_as_service (NSSM)."""
        V.obj_name(name, "name")
        body = (
            f"$n={ps.ps_string(_NSSM_EXE)};$svc={ps.ps_string(name)};"
            "if(Test-Path $n){& $n stop $svc 2>$null|Out-Null;& $n remove $svc confirm 2>$null|Out-Null}"
            "else{& sc.exe stop $svc 2>$null|Out-Null;& sc.exe delete $svc 2>$null|Out-Null};"
            "$result=@{removed=$true;service=$svc}"
        )
        return ctx.exec_json(body, host=host, elevated=True, timeout=60)
