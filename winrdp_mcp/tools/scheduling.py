"""Scheduling & persistence — run commands on a schedule, at startup, or as a resilient
auto-restarting service.
"""

from __future__ import annotations

from typing import Optional

from .. import ps
from ..config import REMOTE_TMP, REMOTE_TOOLS
from ..progress import Context, Progress
from . import _validate as V

_NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"
_NSSM_EXE = REMOTE_TOOLS + r"\nssm.exe"

# Task Scheduler HRESULTs that are status, not an exit code (0x41300-0x41303).
_TASK_PSEUDO_RC = {267008: "ready", 267009: "running", 267010: "not_scheduled",
                   267011: "not_yet_run", 267014: "terminated"}


def _tail_ps(var: str, path_expr: str, lines: int, max_bytes: int) -> str:
    """PS fragment setting ``$var`` to {bytes, tail[]} for a log file, or $null if absent.

    Each line is cast to [string] first: Get-Content decorates its output with
    PSPath/Provider NoteProperties that ConvertTo-Json would expand into megabytes."""
    return (
        f"${var}=$null;$f={path_expr};"
        "if(Test-Path -LiteralPath $f){"
        "$sz=(Get-Item -LiteralPath $f).Length;"
        f"$tx=@(Get-Content -LiteralPath $f -Tail {int(lines)} -ErrorAction SilentlyContinue|"
        "ForEach-Object{[string]$_});"
        f"$j=($tx -join \"`n\");if($j.Length -gt {int(max_bytes)})"
        f"{{$j=$j.Substring($j.Length-{int(max_bytes)})}};"
        f"${var}=@{{bytes=$sz;tail=@($j -split \"`n\")}}}};"
    )


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
    def task_status(name: str, host: Optional[str] = None, lines: int = 20,
                    max_bytes: int = 4000) -> dict:
        """Check on a detached job — is it still running, did it succeed, what did it print.

        This is the counterpart to `run_powershell(detach=True)` / `start_process(detach=True)`
        / `schedule_command`: pass back the `task` name they returned and get the live state
        plus the tail of its stdout/stderr logs in one call, instead of having to guess log
        paths and tail_file them separately.

        Returns {task, status, running, pids, state, last_run, exit_code, stdout, stderr,
        stdout_log, stderr_log}. `status` is one of running | finished | not_yet_run |
        finished_unregistered | not_found. `exit_code` is the process exit code of the last
        completed run (null while it is still running); Task Scheduler's own 0x413xx
        pseudo-results are reported as `task_result` instead of being mistaken for an exit
        code. `stdout`/`stderr` are {bytes, tail[]} or null — the *_log keys hold the paths.

        Stop it with task_stop(name); read more of a log with tail_file(stdout_log).
        """
        V.obj_name(name, "name")
        marker = f"{REMOTE_TMP}\\{name}"
        body = (
            f"$n={ps.ps_string(name)};$marker={ps.ps_string(marker)};"
            "$t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
            "$i=if($t){Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue}else{$null};"
            "$pids=@();try{$pids=@(Get-CimInstance Win32_Process "
            "-Filter \"CommandLine LIKE '%$marker%'\" -ErrorAction SilentlyContinue|"
            "ForEach-Object{$_.ProcessId})}catch{};"
            + _tail_ps("o", "$marker+'.out'", lines, max_bytes)
            + _tail_ps("e", "$marker+'.err'", lines, max_bytes) +
            "$result=@{task=$n;exists=($t -ne $null);"
            "state=if($t){$t.State.ToString()}else{$null};"
            "last_run=if($i -and $i.LastRunTime){$i.LastRunTime.ToString('o')}else{$null};"
            "next_run=if($i -and $i.NextRunTime){$i.NextRunTime.ToString('o')}else{$null};"
            "last_result=if($i -ne $null){$i.LastTaskResult}else{$null};"
            "pids=$pids;stdout=$o;stderr=$e;"
            "stdout_log=($marker+'.out');stderr_log=($marker+'.err')}"
        )
        r = ctx.exec_json(body, host=host, timeout=90)
        if not isinstance(r, dict) or "task" not in r:
            return r if isinstance(r, dict) else {"task": name, "raw": r}

        pids = r.get("pids") or []
        if isinstance(pids, int):  # ConvertTo-Json unwraps a single-element array
            pids = [pids]
        r["pids"] = pids
        rc = r.pop("last_result", None)
        running = r.get("state") == "Running" or bool(pids)
        r["running"] = running
        if rc in _TASK_PSEUDO_RC:
            r["task_result"] = _TASK_PSEUDO_RC[rc]
            r["exit_code"] = None
        else:
            r["exit_code"] = rc
        if running:
            r["status"] = "running"
        elif rc == 267011:
            r["status"] = "not_yet_run"
        elif r.get("exists"):
            r["status"] = "finished"
        elif r.get("stdout") or r.get("stderr"):
            # run_elevated/run_detached unregister the task when done but leave the logs.
            r["status"] = "finished_unregistered"
        else:
            r["status"] = "not_found"
        r["hint"] = ("still running — poll task_status again, or task_stop to cancel"
                     if running else
                     f"not found: no task '{name}' and no logs under {REMOTE_TMP}"
                     if r["status"] == "not_found" else
                     "finished — exit_code 0 means success; read more with tail_file(stdout_log)")
        return r

    @mcp.tool
    def task_stop(name: str, host: Optional[str] = None, unregister: bool = True) -> dict:
        """Stop a running scheduled task by name and kill its process tree — the clean way to
        cancel a detached job (the `task` returned by run_powershell(detach=True) /
        start_process). Also kills any powershell/child processes the task spawned from its
        staged script, then (by default) unregisters the task. Returns what it stopped."""
        V.obj_name(name, "name")
        # The detached wrapper runs `powershell -File <REMOTE_TMP>\<task>.ps1`; find any live
        # process whose command line references that script (the task's own tree) and kill it.
        from ..config import REMOTE_TMP
        script_marker = f"{REMOTE_TMP}\\{name}"
        body = (
            f"$n={ps.ps_string(name)};$marker={ps.ps_string(script_marker)};"
            "$killed=@();"
            "try{Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue}catch{};"
            # kill processes spawned from the task's staged script (and their children, /T)
            "try{$procs=@(Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%$marker%'\" "
            "-ErrorAction SilentlyContinue);foreach($p in $procs){"
            "try{& taskkill /PID $($p.ProcessId) /T /F 2>$null|Out-Null;$killed+=$p.ProcessId}catch{}}}catch{};"
            + ("try{Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue}catch{};"
               if unregister else "")
            + "$result=@{task=$n;killed_pids=$killed;unregistered=" + ("$true" if unregister else "$false") + "}"
        )
        return ctx.exec_json(body, host=host, elevated=True, timeout=60)

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
                           start_now: bool = True, timeout: int = 300,
                           mcp_ctx: Context | None = None) -> dict:
        """Install a command as a resilient auto-restarting Windows service via NSSM.

        Unlike a scheduled task, this keeps the command running and restarts it if it exits.
        NSSM is downloaded to the tool cache on first use. `command` runs under cmd.exe so
        any command line works. Runs elevated.
        """
        V.obj_name(name, "name")
        prog = Progress(mcp_ctx, 100)
        prog.report(15, f"installing service '{name}' via NSSM (downloads NSSM on first use)")
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
        out = ctx.run_long_json(body, host=host, timeout=timeout,
                                progress=prog.heartbeat(f"installing service {name}"))
        prog.report(100, f"service '{name}' installed")
        return out

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
