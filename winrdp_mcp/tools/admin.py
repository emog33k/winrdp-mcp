"""Administration tools: registry, services, processes, scheduled tasks, users, firewall."""

from __future__ import annotations

import binascii
import os
from typing import Optional

from .. import ps
from ..config import REMOTE_TMP
from . import _validate as V


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ registry
    @mcp.tool
    def reg_read(path: str, name: Optional[str] = None, host: Optional[str] = None) -> dict:
        """Read a registry key (all values) or a single value.
        path like 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'."""
        if name is not None:
            V.obj_name(name, "name")
        if name:
            body = (
                f"$v=Get-ItemProperty -Path {ps.ps_string(path)} -Name {ps.ps_string(name)};"
                f"$result=@{{path={ps.ps_string(path)};name={ps.ps_string(name)};value=$v.{_safe(name)}}}"
            )
        else:
            body = (
                f"$i=Get-ItemProperty -Path {ps.ps_string(path)};"
                "$h=[ordered]@{};$i.PSObject.Properties|Where-Object{$_.Name -notlike 'PS*'}|"
                "ForEach-Object{$h[$_.Name]=$_.Value};"
                f"$result=@{{path={ps.ps_string(path)};values=$h}}"
            )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def reg_write(path: str, name: str, value: str, host: Optional[str] = None,
                  type: str = "String") -> dict:
        """Write a registry value. type: String|DWord|QWord|Binary|ExpandString|MultiString.
        Creates the key if missing."""
        V.enum(type, {"String", "DWord", "QWord", "Binary", "ExpandString", "MultiString"}, "type")
        if type in ("DWord", "QWord"):
            try:
                int(str(value), 0)
            except ValueError:
                raise V.ValidationError(f"{type} value must be an integer, got {value!r}")
            val = str(int(str(value), 0))
        else:
            val = ps.ps_string(value)
        body = (
            f"if(-not(Test-Path {ps.ps_string(path)})){{New-Item -Path {ps.ps_string(path)} -Force|Out-Null}};"
            f"New-ItemProperty -Path {ps.ps_string(path)} -Name {ps.ps_string(name)} -Value {val} "
            f"-PropertyType {type} -Force|Out-Null;"
            "$result=@{ok=$true}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def reg_delete(path: str, name: Optional[str] = None, host: Optional[str] = None) -> dict:
        """Delete a registry value (if name given) or an entire key."""
        if name:
            body = f"Remove-ItemProperty -Path {ps.ps_string(path)} -Name {ps.ps_string(name)} -Force;$result=@{{ok=$true}}"
        else:
            body = f"Remove-Item -Path {ps.ps_string(path)} -Recurse -Force;$result=@{{ok=$true}}"
        return ctx.exec_json(body, host=host)

    # ------------------------------------------------------------------ services
    @mcp.tool
    def list_services(host: Optional[str] = None, filter: Optional[str] = None) -> list:
        """List Windows services (name, display name, status, start mode)."""
        if filter:
            pat = ps.ps_string(f"*{filter}*")
            where = f"|Where-Object{{$_.Name -like {pat} -or $_.DisplayName -like {pat}}}"
        else:
            where = ""
        body = (
            f"$result=@(Get-Service{where}|ForEach-Object{{@{{"
            "name=$_.Name;display=$_.DisplayName;status=$_.Status.ToString();"
            "start=(Get-Service $_.Name).StartType.ToString()}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def service_control(name: str, action: str, host: Optional[str] = None) -> dict:
        """Control a service. action: start | stop | restart | enable | disable | manual | auto."""
        actions = {
            "start": f"Start-Service -Name {ps.ps_string(name)}",
            "stop": f"Stop-Service -Name {ps.ps_string(name)} -Force",
            "restart": f"Restart-Service -Name {ps.ps_string(name)} -Force",
            "enable": f"Set-Service -Name {ps.ps_string(name)} -StartupType Automatic",
            "auto": f"Set-Service -Name {ps.ps_string(name)} -StartupType Automatic",
            "disable": f"Set-Service -Name {ps.ps_string(name)} -StartupType Disabled",
            "manual": f"Set-Service -Name {ps.ps_string(name)} -StartupType Manual",
        }
        if action not in actions:
            return {"error": f"unknown action '{action}'"}
        body = (
            actions[action] + ";$s=Get-Service -Name " + ps.ps_string(name) + ";"
            "$result=@{name=$s.Name;status=$s.Status.ToString();start=$s.StartType.ToString()}"
        )
        return ctx.exec_json(body, host=host, elevated=False)

    # ------------------------------------------------------------------ processes
    @mcp.tool
    def list_processes(host: Optional[str] = None, name: Optional[str] = None, top: int = 100) -> list:
        """List running processes with pid, memory, and CPU seconds."""
        flt = f" -Name {ps.ps_string(name)}" if name else ""
        body = (
            f"$result=@(Get-Process{flt} -ErrorAction SilentlyContinue|"
            f"Sort-Object WorkingSet64 -Descending|Select-Object -First {int(top)}|ForEach-Object{{@{{"
            "name=$_.ProcessName;pid=$_.Id;mem_mb=[math]::Round($_.WorkingSet64/1MB,1);"
            "cpu_s=[math]::Round($_.CPU,1);path=$_.Path}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def kill_process(pid: Optional[int] = None, name: Optional[str] = None,
                     host: Optional[str] = None) -> dict:
        """Kill a process by pid or by name (all matching)."""
        if pid is not None:
            body = f"Stop-Process -Id {int(pid)} -Force;$result=@{{killed=$true;pid={int(pid)}}}"
        elif name:
            body = f"Stop-Process -Name {ps.ps_string(name)} -Force;$result=@{{killed=$true;name={ps.ps_string(name)}}}"
        else:
            return {"error": "provide pid or name"}
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def start_process(command: str, host: Optional[str] = None, elevated: bool = False,
                      as_user: bool = False, wait: bool = False, detach: bool = False) -> dict:
        """Launch a program/command on a box. elevated=True for full token, as_user=True
        to launch in the interactive RDP desktop session.

        detach=True launches it via a Scheduled Task so it SURVIVES the session close (a
        normal launch dies with the WinRM Job Object) and gets loopback access — use this
        for a background server. When wait=False the process's stdout/stderr are redirected
        to log files (stdout_log/stderr_log) for diagnosis via tail_file. wait=True returns
        the output directly.
        """
        if detach:
            return ctx.run_detached(f"& $env:ComSpec /c {ps.ps_string(command)}", host=host)
        if wait:
            r = ctx.exec_ps(command, host=host, elevated=elevated, as_user=as_user, timeout=300)
            return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
        rid = binascii.hexlify(os.urandom(5)).decode()
        out = f"{REMOTE_TMP}\\proc_{rid}.out"
        err = f"{REMOTE_TMP}\\proc_{rid}.err"
        launch = (
            ps.ensure_remote_dirs() + ";"
            f"$p=Start-Process -FilePath $env:ComSpec -ArgumentList '/c',{ps.ps_string(command)} "
            f"-WindowStyle Hidden -PassThru -RedirectStandardOutput {ps.ps_string(out)} "
            f"-RedirectStandardError {ps.ps_string(err)};Write-Output $p.Id"
        )
        r = ctx.exec_ps(launch, host=host, elevated=elevated, as_user=as_user, timeout=60)
        pid = (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout.strip() else None
        return {"pid": pid, "stdout_log": out, "stderr_log": err,
                "note": "background process launched; read stdout_log/stderr_log with tail_file",
                "stderr": r.stderr, "rc": r.rc}

    # ------------------------------------------------------------------ scheduled tasks
    @mcp.tool
    def list_tasks(host: Optional[str] = None, folder: str = "\\", detailed: bool = False,
                   top: int = 500) -> list:
        """List scheduled tasks (name, path, state).

        Fast by default. detailed=True also fetches last-run time/result per task via
        Get-ScheduledTaskInfo — much slower on boxes with many tasks (N+1 CIM calls), so
        keep it off unless you need it and scope with `folder`.
        """
        if detailed:
            info = (
                "$i=$_|Get-ScheduledTaskInfo -ErrorAction SilentlyContinue;"
                "last_run=if($i){$i.LastRunTime.ToString('o')}else{$null};"
                "last_result=if($i){$i.LastTaskResult}else{$null};"
            )
        else:
            info = ""
        body = (
            f"$result=@(Get-ScheduledTask -TaskPath {ps.ps_string(folder)} -ErrorAction SilentlyContinue|"
            f"Select-Object -First {int(top)}|ForEach-Object{{@{{"
            f"name=$_.TaskName;path=$_.TaskPath;state=$_.State.ToString();{info}}}}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def task_create(name: str, command: str, host: Optional[str] = None,
                    schedule: str = "ONCE", start_time: str = "23:59",
                    run_as: str = "SYSTEM", highest: bool = True) -> dict:
        """Create a scheduled task. schedule: ONCE|DAILY|HOURLY|ONLOGON|ONSTART.
        run_as SYSTEM needs no password; highest=True runs with a full token."""
        V.obj_name(name, "name")
        V.obj_name(run_as, "run_as")
        V.enum(schedule, {"ONCE", "DAILY", "HOURLY", "ONLOGON", "ONSTART", "MINUTE"}, "schedule")
        V.token(start_time, "start_time")
        rl = "/RL HIGHEST " if highest else ""
        st = f"/ST {start_time} " if schedule in ("ONCE", "DAILY", "HOURLY") else ""
        cmd = (
            f'schtasks /Create /F /TN "{name}" /TR "{command}" /SC {schedule} {st}{rl}/RU "{run_as}"'
        )
        r = ctx.transport_for(host).run_cmd(cmd, timeout=60)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def task_delete(name: str, host: Optional[str] = None) -> dict:
        """Delete a scheduled task by name."""
        V.obj_name(name, "name")
        r = ctx.transport_for(host).run_cmd(f'schtasks /Delete /F /TN "{name}"', timeout=60)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def task_run(name: str, host: Optional[str] = None) -> dict:
        """Run a scheduled task now."""
        V.obj_name(name, "name")
        r = ctx.transport_for(host).run_cmd(f'schtasks /Run /TN "{name}"', timeout=60)
        return {"ok": r.ok, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    # ------------------------------------------------------------------ users & groups
    @mcp.tool
    def list_users(host: Optional[str] = None) -> list:
        """List local user accounts (enabled state, last logon, admin membership)."""
        body = (
            "$admins=@((Get-LocalGroupMember -Group 'Administrators' -ErrorAction SilentlyContinue).Name);"
            "$result=@(Get-LocalUser|ForEach-Object{@{"
            "name=$_.Name;enabled=$_.Enabled;"
            "last_logon=if($_.LastLogon){$_.LastLogon.ToString('o')}else{$null};"
            "is_admin=($admins -contains \"$env:COMPUTERNAME\\$($_.Name)\" -or $admins -contains $_.Name)}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def user_create(username: str, password: str, host: Optional[str] = None,
                    administrator: bool = False, rdp: bool = True,
                    never_expires: bool = True) -> dict:
        """Create a local user; optionally add to Administrators and Remote Desktop Users."""
        never = " -PasswordNeverExpires:$true" if never_expires else ""
        # The password is passed via `secrets` (staged to an admin-only file and read into
        # $pw on the box) rather than inlined, so it never lands on the target command line /
        # Event 4688.
        lines = [
            "$sec=ConvertTo-SecureString $pw -AsPlainText -Force",
            f"if(Get-LocalUser -Name {ps.ps_string(username)} -ErrorAction SilentlyContinue){{"
            f"Set-LocalUser -Name {ps.ps_string(username)} -Password $sec}}else{{"
            f"New-LocalUser -Name {ps.ps_string(username)} -Password $sec{never} -AccountNeverExpires|Out-Null}}",
        ]
        if administrator:
            lines.append(
                f"Add-LocalGroupMember -Group 'Administrators' -Member {ps.ps_string(username)} -ErrorAction SilentlyContinue")
        if rdp:
            lines.append(
                f"Add-LocalGroupMember -Group 'Remote Desktop Users' -Member {ps.ps_string(username)} -ErrorAction SilentlyContinue")
        lines.append("$result=@{created=$true;user=" + ps.ps_string(username) +
                     ";administrator=$" + str(administrator).lower() + ";rdp=$" + str(rdp).lower() + "}")
        return ctx.exec_json(";".join(lines), host=host, elevated=False, secrets={"pw": password})

    @mcp.tool
    def user_delete(username: str, host: Optional[str] = None) -> dict:
        """Delete a local user account."""
        body = f"Remove-LocalUser -Name {ps.ps_string(username)};$result=@{{deleted=$true;user={ps.ps_string(username)}}}"
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def group_add_member(group: str, member: str, host: Optional[str] = None) -> dict:
        """Add a user to a local group (e.g. 'Administrators', 'Remote Desktop Users')."""
        body = (
            f"Add-LocalGroupMember -Group {ps.ps_string(group)} -Member {ps.ps_string(member)} "
            "-ErrorAction SilentlyContinue;$result=@{ok=$true}"
        )
        return ctx.exec_json(body, host=host)

    # ------------------------------------------------------------------ firewall
    @mcp.tool
    def firewall_rules(host: Optional[str] = None, direction: str = "Inbound",
                       enabled_only: bool = True, top: int = 200) -> list:
        """List firewall rules for a direction."""
        V.enum(direction, {"Inbound", "Outbound"}, "direction")
        flt = "|Where-Object{$_.Enabled -eq 'True'}" if enabled_only else ""
        body = (
            f"$result=@(Get-NetFirewallRule -Direction {direction}{flt}|Select-Object -First {int(top)}|ForEach-Object{{@{{"
            "name=$_.DisplayName;enabled=$_.Enabled.ToString();action=$_.Action.ToString();"
            "profile=$_.Profile.ToString()}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def firewall_add(name: str, port: int, host: Optional[str] = None,
                     protocol: str = "TCP", direction: str = "Inbound",
                     action: str = "Allow") -> dict:
        """Open (or block) a port in Windows Firewall."""
        V.enum(direction, {"Inbound", "Outbound"}, "direction")
        V.enum(protocol, {"TCP", "UDP"}, "protocol")
        V.enum(action, {"Allow", "Block"}, "action")
        body = (
            f"New-NetFirewallRule -DisplayName {ps.ps_string(name)} -Direction {direction} "
            f"-Protocol {protocol} -LocalPort {int(port)} -Action {action}|Out-Null;"
            "$result=@{ok=$true;name=" + ps.ps_string(name) + f";port={int(port)}}}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def firewall_delete(name: str, host: Optional[str] = None) -> dict:
        """Delete firewall rule(s) by display name."""
        body = f"Remove-NetFirewallRule -DisplayName {ps.ps_string(name)};$result=@{{ok=$true}}"
        return ctx.exec_json(body, host=host)

    # ------------------------------------------------------------------ service create/delete
    @mcp.tool
    def service_create(name: str, bin_path: str, host: Optional[str] = None,
                       display_name: Optional[str] = None, start: str = "auto",
                       run_as: Optional[str] = None, password: Optional[str] = None) -> dict:
        """Create a Windows service. start: auto|demand|disabled. Optionally run under a
        specific account. Runs elevated."""
        startmap = {"auto": "Automatic", "demand": "Manual", "manual": "Manual", "disabled": "Disabled"}
        V.enum(start, set(startmap), "start")
        # Use New-Service with properly quoted args (no cmd/sc quoting to break; a `"`/`$`
        # in a value can't inject).
        lines = [
            f"$sp=@{{Name={ps.ps_string(name)};BinaryPathName={ps.ps_string(bin_path)};"
            f"StartupType='{startmap[start]}'}}",
        ]
        if display_name:
            lines.append(f"$sp['DisplayName']={ps.ps_string(display_name)}")
        secrets = None
        if run_as:
            if password:
                # $pw is read on the box from an admin-only file (see secrets=), never inlined.
                secrets = {"pw": password}
                lines.append(
                    f"$sp['Credential']=New-Object PSCredential({ps.ps_string(run_as)},"
                    "(ConvertTo-SecureString $pw -AsPlainText -Force))"
                )
            else:
                lines.append(f"$sp['Credential']=New-Object PSCredential({ps.ps_string(run_as)},"
                             "(New-Object Security.SecureString))")
        lines.append("New-Service @sp|Out-Null;$result=@{ok=$true;name=" + ps.ps_string(name) + "}")
        return ctx.exec_json(";".join(lines), host=host, elevated=True, timeout=60, secrets=secrets)

    @mcp.tool
    def service_delete(name: str, host: Optional[str] = None) -> dict:
        """Delete a Windows service. Runs elevated."""
        r = ctx.exec_ps(f"& sc.exe delete {ps.ps_string(name)}", host=host, elevated=True, timeout=60)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    # ------------------------------------------------------------------ clipboard
    @mcp.tool
    def clipboard_get(host: Optional[str] = None) -> dict:
        """Read the clipboard text of the interactive session on a box."""
        r = ctx.exec_ps("Get-Clipboard -Raw", host=host, as_user=True, timeout=30)
        return {"text": r.stdout}

    @mcp.tool
    def clipboard_set(text: str, host: Optional[str] = None) -> dict:
        """Set the clipboard text of the interactive session on a box."""
        r = ctx.exec_ps(f"Set-Clipboard -Value {ps.ps_string(text)}", host=host, as_user=True, timeout=30)
        return {"ok": r.rc == 0, "stderr": r.stderr}

    # ------------------------------------------------------------------ event log write/clear
    @mcp.tool
    def write_event(message: str, host: Optional[str] = None, log: str = "Application",
                    source: str = "winrdp-mcp", event_id: int = 1000, level: str = "Information") -> dict:
        """Write an entry to a Windows event log (creates the source if needed). Elevated."""
        # `level` is spliced raw into an elevated (SYSTEM) command — validate it against the
        # allowed EntryType set so it can't inject PowerShell.
        V.enum(level, {"Information", "Warning", "Error", "SuccessAudit", "FailureAudit"}, "level")
        body = (
            f"if(-not [Diagnostics.EventLog]::SourceExists({ps.ps_string(source)})){{"
            f"New-EventLog -LogName {ps.ps_string(log)} -Source {ps.ps_string(source)}}};"
            f"Write-EventLog -LogName {ps.ps_string(log)} -Source {ps.ps_string(source)} "
            f"-EventId {int(event_id)} -EntryType {level} -Message {ps.ps_string(message)};"
            "$result=@{ok=$true}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def clear_event_log(log: str, host: Optional[str] = None) -> dict:
        """Clear a Windows event log. Runs elevated."""
        body = f"Clear-EventLog -LogName {ps.ps_string(log)};$result=@{{cleared=$true;log={ps.ps_string(log)}}}"
        return ctx.exec_json(body, host=host, elevated=True)


def _safe(name: str) -> str:
    # property access $v.<name> — guard odd chars by wrapping in braces
    return "{" + name.replace("}", "") + "}"
