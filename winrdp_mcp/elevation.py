"""UAC / privilege elevation on managed boxes.

Two real mechanisms, not "please run as admin":

* :func:`run_elevated` — runs a script through a one-shot Scheduled Task with
  ``RunLevel = Highest``. A task principal gets a *full* (unfiltered) token, so this
  bypasses the UAC token-filtering that otherwise hands network/SSH logons of a
  non-builtin local admin a restricted token. Runs as ``SYSTEM`` by default.

* :func:`run_in_user_session` — runs a script inside the interactive console user's
  session (needed for anything that must touch the visible desktop / GUI).

Also exposes helpers to read and change the machine's UAC policy.
"""

from __future__ import annotations

import binascii
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import ps
from .config import REMOTE_TMP
from .transports import ExecResult, Transport, TransportError

# Parse qwinsta's fixed-width columns into the active (or disconnected) interactive user.
# Given $rows (qwinsta's output lines), emit ACTIVE:<user>, DISC:<user>, or NONE.
# The USERNAME slice must stop at the ID column, not at STATE: the ID column sits between
# them, so slicing to STATE lets the session-id digits bleed into the username (e.g. a
# short "admin" becomes "admin                     1"). That value fails the SID lookup in
# Register-ScheduledTask (ERROR_NONE_MAPPED). Cutting at ID also keeps a blank-username row
# (services/listeners) empty so it is skipped, and preserves usernames that contain spaces.
_SESSION_PARSE_PS = (
    "$active=$null;$disc=$null;"
    "if($rows.Count -ge 2){$h=$rows[0];$iU=$h.IndexOf('USERNAME');$iI=$h.IndexOf('ID');$iS=$h.IndexOf('STATE');"
    "if($iU -ge 0 -and $iI -gt $iU -and $iS -gt $iI){foreach($l in ($rows|Select-Object -Skip 1)){"
    "if($l.Length -le $iS){continue};"
    "$u=$l.Substring($iU,$iI-$iU).Trim();$st=($l.Substring($iS).Trim() -split '\\s+')[0];"
    "if($u){if($st -eq 'Active'){$active=$u}elseif($st -eq 'Disc'){$disc=$u}}}}}"
    "if($active){'ACTIVE:'+$active}elseif($disc){'DISC:'+$disc}else{'NONE'}"
)
# Windows Home editions may ship without qwinsta.exe. When it is missing, fall back to
# the console user reported by Win32_ComputerSystem, confirmed live by an explorer.exe
# owned by that user (so a logged-off box still reports NONE). $rows is emitted as
# ACTIVE:<user> or NONE; the disconnected case does not arise on this single-user path.
_HOME_FALLBACK_PS = (
    "$u=[string](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName;"
    r"if($u){$leaf=($u -split '\\')[-1];$found=$false;"
    "$shells=@(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" -ErrorAction SilentlyContinue);"
    "foreach($p in $shells){"
    "$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue;"
    r"if($o){$full=if($o.Domain){$o.Domain+'\'+$o.User}else{$o.User};"
    "if(($full -ieq $u)-or($o.User -ieq $leaf)){$found=$true;break}}}"
    "if($found){'ACTIVE:'+$u}else{'NONE'}"
    "}else{'NONE'}"
)
# Prefer qwinsta (keeps multi-session/RDP hosts complete); fall back on Home boxes.
ACTIVE_SESSION_PROBE_PS = (
    "$q=Get-Command qwinsta -ErrorAction SilentlyContinue;"
    "if($q){$rows=@(& $q.Source 2>$null);" + _SESSION_PARSE_PS + "}"
    "else{" + _HOME_FALLBACK_PS + "}"
)

# Windows Home boxes may lack qwinsta. Fall back to the console user confirmed by an
# explorer.exe owned by that user, and expose it as a synthetic console row.
_HOME_FALLBACK_ROWS_PS = (
    "$u=[string](Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName;"
    "if($u){$leaf=($u -split '\\')[-1];$found=$false;"
    "$shells=@(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" "
    "-ErrorAction SilentlyContinue);"
    "foreach($p in $shells){"
    "$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue;"
    "if($o){$full=if($o.Domain){$o.Domain+'\\'+$o.User}else{$o.User};"
    "if(($full -ieq $u)-or($o.User -ieq $leaf)){$found=$true;break}}};"
    "if($found){$rows=@(@{session='console';user=$u;id='';state='Active';active=$true});"
    "$active=$true;$console=$true}}"
)

# Structured session list for session selection (same header slicing as above).
SESSIONS_PS = (
    "$active=$false;$console=$false;$rows=@();"
    "$q=Get-Command qwinsta -ErrorAction SilentlyContinue;"
    "if($q){$lines=@(& $q.Source 2>$null);"
    "if($lines.Count -ge 1){$h=[string]$lines[0];"
    "$iS=$h.IndexOf('SESSIONNAME');$iU=$h.IndexOf('USERNAME');"
    "$iI=$h.IndexOf('ID');$iT=$h.IndexOf('STATE');"
    "if($iS -ge 0 -and $iU -gt $iS -and $iI -gt $iU -and $iT -gt $iI){"
    "foreach($l in ($lines|Select-Object -Skip 1)){"
    "if($l.Length -le $iT){continue};"
    "$nm=$l.Substring($iS,$iU-$iS).Trim().TrimStart([char]'>');"
    "$u=$l.Substring($iU,$iI-$iU).Trim();"
    "$id=$l.Substring($iI,$iT-$iI).Trim();"
    "$st=($l.Substring($iT).Trim() -split '\\s+')[0];"
    "$a=($u -and $st -ieq 'Active');"
    "if($nm -ieq 'console'){$console=$true};"
    "if($a){$active=$true};"
    "$rows+=@{session=$nm;user=$u;id=$id;state=$st;active=$a}}}}"
    "}else{" + _HOME_FALLBACK_ROWS_PS + "};"
    "$result=@{active=$active;console=$console;rows=@($rows)}"
)


def list_sessions(transport: Transport) -> dict:
    """Return {active, console, rows:[{session,user,id,state,active}]} from qwinsta."""
    r = transport.run_ps(ps.wrap_json(SESSIONS_PS), timeout=30, idempotent=True)
    data = ps.parse_json(r.stdout)
    if not isinstance(data, dict) or "error" in data:
        raise TransportError((data or {}).get("error") or "could not read interactive sessions")
    return data


@dataclass
class ElevatedResult:
    stdout: str
    stderr: str
    rc: int

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _rid() -> str:
    return binascii.hexlify(os.urandom(6)).decode()


def kill_task(transport: Transport, task: str, marker: str, *,
              unregister: bool = True) -> list[int]:
    """Stop a scheduled task AND kill the process tree it spawned; return the killed pids.

    ``Stop-ScheduledTask`` alone is not enough: it signals the task, but a child process the
    task's powershell already started (an installer, a python script) keeps running. The
    task's staged script path is a unique string on those processes' command lines, so
    ``marker`` identifies exactly that tree and nothing else. Best-effort throughout — a
    cleanup path must not raise over the error that triggered it.
    """
    body = (
        f"$n={ps.ps_string(task)};$m={ps.ps_string(marker)};$killed=@();"
        "try{Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue}catch{};"
        "try{$procs=@(Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%$m%'\" "
        "-ErrorAction SilentlyContinue);foreach($p in $procs){"
        "try{& taskkill /PID $($p.ProcessId) /T /F 2>$null|Out-Null;$killed+=$p.ProcessId}catch{}}}catch{};"
        + ("try{Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue}catch{};"
           if unregister else "")
        + "$result=@{killed=@($killed)}"
    )
    try:
        data = ps.parse_json(transport.run_ps(ps.wrap_json(body), timeout=90).stdout)
        killed = data.get("killed") if isinstance(data, dict) else None
        if isinstance(killed, int):  # ConvertTo-Json unwraps a single element
            return [killed]
        return list(killed or [])
    except Exception:  # noqa: BLE001
        return []


def run_elevated(
    transport: Transport,
    script: str,
    *,
    timeout: int = 300,
    run_as: str = "SYSTEM",
    password: Optional[str] = None,
    poll: float = 1.5,
    progress=None,
    kill_on_timeout: bool = False,
) -> ElevatedResult:
    """Execute ``script`` with a full elevated token via a transient Scheduled Task.

    On timeout the remote work does NOT stop by itself — the deadline here is only the
    poll loop's. ``kill_on_timeout=True`` stops the task and kills its process tree before
    raising; otherwise the task and its logs are deliberately LEFT IN PLACE (not cleaned
    up) so the caller can poll it with ``task_status`` or cancel it with ``task_stop``.
    """
    rid = _rid()
    task = f"winrdp_elev_{rid}"
    base = f"{REMOTE_TMP}\\{task}"
    remote_ps = base + ".ps1"
    out_f = base + ".out"
    err_f = base + ".err"
    done_f = base + ".done"

    wrapper = (
        "$ErrorActionPreference='Continue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "try{"
        f"& {{{script}}} 1> {ps.ps_string(out_f)} 2> {ps.ps_string(err_f)};"
        "$rc=$LASTEXITCODE; if($null -eq $rc){$rc=0}"
        "}catch{"
        f"$_ | Out-File -FilePath {ps.ps_string(err_f)} -Encoding utf8 -Append; $rc=1"
        "}finally{"
        f"Set-Content -LiteralPath {ps.ps_string(done_f)} -Value \"rc=$rc\" -Encoding ascii"
        "}"
    )
    # UTF-8 BOM so both Windows PowerShell 5.1 and PowerShell 7 read `-File` correctly.
    transport.upload(wrapper.encode("utf-8-sig"), remote_ps)

    # Register + run the task via the ScheduledTasks cmdlets — all inside PowerShell, so
    # there is NO cmd.exe/schtasks quoting to mangle (the schtasks CLI + list2cmdline
    # combination silently corrupts nested quotes across transports).
    arg = f'-NoProfile -ExecutionPolicy Bypass -File "{remote_ps}"'
    reg_lines = ["$ErrorActionPreference='Stop'",
                 f"$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {ps.ps_string(arg)}"]
    if run_as.upper() == "SYSTEM":
        reg_lines.append("$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest")
        reg_lines.append(f"Register-ScheduledTask -TaskName {ps.ps_string(task)} -Action $a -Principal $p -Force|Out-Null")
    else:
        reg_lines.append(
            f"Register-ScheduledTask -TaskName {ps.ps_string(task)} -Action $a "
            f"-User {ps.ps_string(run_as)} -Password {ps.ps_string(password or '')} -RunLevel Highest -Force|Out-Null"
        )
    reg_lines.append(f"Start-ScheduledTask -TaskName {ps.ps_string(task)}")
    transport.run_ps(";".join(reg_lines), timeout=60).raise_for_status("register elevated task")
    timed_out = False
    try:
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            chk = transport.run_ps(f"Test-Path -LiteralPath {ps.ps_string(done_f)}",
                                   timeout=30, idempotent=True)
            if "True" in chk.stdout:
                break
            if progress is not None:
                try:
                    progress(time.time() - start)
                except Exception:  # noqa: BLE001 — a heartbeat must never break the run
                    pass
            time.sleep(poll)
        else:
            timed_out = True
            if kill_on_timeout:
                killed = kill_task(transport, task, remote_ps)
                raise TransportError(
                    f"elevated task timed out after {timeout}s — task stopped and "
                    f"{len(killed)} process(es) killed on the box (kill_on_timeout=True)")
            raise TransportError(
                f"elevated task timed out after {timeout}s, but it is STILL RUNNING on the "
                f"box (the deadline is the poll loop's, not the remote process's). Poll it "
                f"with task_status('{task}'), read its output with tail_file('{out_f}'), or "
                f"cancel it with task_stop('{task}'). Pass kill_on_timeout=True to have a "
                f"timeout kill it instead of leaving it running.")

        out = _safe_read(transport, out_f)
        err = _safe_read(transport, err_f)
        done = _safe_read(transport, done_f)
        rc = 0
        if "rc=" in done:
            try:
                rc = int(done.strip().split("rc=", 1)[1].split()[0])
            except (ValueError, IndexError):
                rc = 0
        return ElevatedResult(out, err, rc)
    finally:
        # Best-effort cleanup: if it raises (wedged transport) it must NOT replace the
        # result / mask the real exception being propagated. Skipped entirely on a timeout
        # the caller didn't ask us to kill — unregistering a task that is still running
        # would orphan it, leaving no handle to poll or stop it with.
        if not timed_out or kill_on_timeout:
            try:
                transport.run_ps(
                    f"Unregister-ScheduledTask -TaskName {ps.ps_string(task)} -Confirm:$false -ErrorAction SilentlyContinue;"
                    "Remove-Item -LiteralPath " + ",".join(
                        ps.ps_string(p) for p in (remote_ps, out_f, err_f, done_f)
                    ) + " -Force -ErrorAction SilentlyContinue",
                    timeout=30,
                )
            except Exception:  # noqa: BLE001
                pass


def run_detached(transport: Transport, script: str, *, run_as: str = "SYSTEM") -> dict:
    """Launch ``script`` fire-and-forget in a Scheduled Task and return immediately.

    A process started over WinRM (Start-Process, powershell -File, ...) lives inside the
    WinRM shell's Job Object and is killed when the session/command ends — so a background
    server dies after ~1 minute. A Scheduled Task runs the process under the Task Scheduler
    service, OUTSIDE that Job Object, so it survives the session. Also runs under a
    service/batch logon (not the WinRM network logon), so it can reach 127.0.0.1.
    """
    rid = _rid()
    task = f"winrdp_bg_{rid}"
    base = f"{REMOTE_TMP}\\{task}"
    remote_ps, out_f, err_f = base + ".ps1", base + ".out", base + ".err"
    wrapper = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"& {{{script}}} 1> {ps.ps_string(out_f)} 2> {ps.ps_string(err_f)}"
    )
    transport.upload(wrapper.encode("utf-8-sig"), remote_ps)
    arg = f'-NoProfile -ExecutionPolicy Bypass -File "{remote_ps}"'
    reg = (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {ps.ps_string(arg)};"
        f"$p=New-ScheduledTaskPrincipal -UserId {ps.ps_string(run_as)} -LogonType ServiceAccount -RunLevel Highest;"
        "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero);"
        f"Register-ScheduledTask -TaskName {ps.ps_string(task)} -Action $a -Principal $p -Settings $s -Force|Out-Null;"
        f"Start-ScheduledTask -TaskName {ps.ps_string(task)}"
    )
    transport.run_ps(reg, timeout=60).raise_for_status("register detached task")
    return {
        "detached": True, "task": task, "stdout_log": out_f, "stderr_log": err_f,
        "note": ("running outside the WinRM Job Object (survives session close) and with "
                 f"loopback access. Check whether it is still alive and what it exited with "
                 f"via task_status('{task}'); read more output with tail_file(stdout_log); "
                 f"stop it cleanly with task_stop('{task}') (stops the task and kills its "
                 "process tree)."),
    }


def _safe_read(transport: Transport, path: str) -> str:
    r = transport.run_ps(
        f"if(Test-Path -LiteralPath {ps.ps_string(path)}){{Get-Content -LiteralPath {ps.ps_string(path)} -Raw}}",
        timeout=60,
    )
    return r.stdout


def run_in_user_session(transport: Transport, script: str, *, timeout: int = 120,
                        session_id: Optional[int] = None) -> ExecResult:
    """Run ``script`` inside an interactive user's session (for GUI work).

    Uses a Scheduled Task bound to that user with an interactive logon so the process
    attaches to the visible desktop. ``session_id`` selects a row from
    :func:`list_sessions`; without it the first Active session is used. A disconnected
    session has no composed desktop and fails with an actionable message.

    Limitation: Task Scheduler binds to the USER, not to a session id — if the same user
    has several interactive sessions the OS picks one. True per-session pinning needs
    CreateProcessAsUser.
    """
    rid = _rid()
    task = f"winrdp_ui_{rid}"
    remote_ps = f"{REMOTE_TMP}\\{task}.ps1"
    out_f = f"{REMOTE_TMP}\\{task}.out"
    done_f = f"{REMOTE_TMP}\\{task}.done"

    rows = [r for r in (list_sessions(transport).get("rows") or []) if r.get("user")]
    if session_id is not None:
        match = [r for r in rows if str(r.get("id")) == str(session_id)]
        if not match:
            available = ", ".join(
                f"{r.get('id')}:{r.get('user')}:{r.get('state')}" for r in rows) or "none"
            raise TransportError(
                f"session {session_id} not found; available sessions: {available}")
        chosen = match[0]
        if not chosen.get("active"):
            raise TransportError(
                f"session {session_id} ({chosen.get('user')}) is {chosen.get('state')} — "
                "GUI / as_user ops need an Active session. Reconnect RDP, or call "
                "rdp_connect_to_console(session_id) to move it to the console.")
    else:
        active = [r for r in rows if r.get("active")]
        if active:
            chosen = active[0]
        else:
            disc = [r for r in rows
                    if str(r.get("state", "")).lower().startswith(("disc", "откл"))]
            if disc:
                raise TransportError(
                    f"the interactive session for '{disc[0].get('user')}' is Disconnected — "
                    "GUI / as_user ops need a connected desktop. Reconnect RDP, or call "
                    "rdp_connect_to_console(session_id) to move the session to the console "
                    "(which composes the desktop).")
            raise TransportError("no interactive user session on the box for GUI / as_user ops")
    user = chosen["user"]

    wrapper = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "try{"
        f"& {{{script}}} *> {ps.ps_string(out_f)};$rc=$LASTEXITCODE;if($null -eq $rc){{$rc=0}}"
        f"}}catch{{$_|Out-File -FilePath {ps.ps_string(out_f)} -Append -Encoding utf8;$rc=1"
        f"}}finally{{Set-Content -LiteralPath {ps.ps_string(done_f)} -Value \"rc=$rc\" -Encoding ascii}}"
    )
    transport.upload(wrapper.encode("utf-8-sig"), remote_ps)
    arg = f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{remote_ps}"'
    register = (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {ps.ps_string(arg)};"
        f"$p=New-ScheduledTaskPrincipal -UserId {ps.ps_string(user)} -LogonType Interactive -RunLevel Limited;"
        f"Register-ScheduledTask -TaskName {ps.ps_string(task)} -Action $a -Principal $p -Force|Out-Null;"
        f"Start-ScheduledTask -TaskName {ps.ps_string(task)}"
    )
    transport.run_ps(register, timeout=60).raise_for_status("register UI task")
    try:
        # Poll for completion (fast for screenshot/clipboard; a persistent GUI launcher
        # returns rc unknown once we stop waiting — reported as still_running).
        deadline = time.time() + timeout
        rc = 0
        still_running = True
        while time.time() < deadline:
            if "True" in transport.run_ps(f"Test-Path -LiteralPath {ps.ps_string(done_f)}", timeout=30).stdout:
                done = _safe_read(transport, done_f)
                if "rc=" in done:
                    try:
                        rc = int(done.strip().split("rc=", 1)[1].split()[0])
                    except (ValueError, IndexError):
                        rc = 0
                still_running = False
                break
            time.sleep(1.5)
        out = _safe_read(transport, out_f)
        return ExecResult(out, "" if not still_running else "still running (no exit captured)", rc)
    finally:
        try:
            transport.run_ps(
                f"Unregister-ScheduledTask -TaskName {ps.ps_string(task)} -Confirm:$false -ErrorAction SilentlyContinue;"
                f"Remove-Item -LiteralPath {ps.ps_string(remote_ps)},{ps.ps_string(out_f)},{ps.ps_string(done_f)} -Force -ErrorAction SilentlyContinue",
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            pass


UAC_KEY = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def get_uac_policy_script() -> str:
    return (
        f"$k=Get-ItemProperty -Path '{UAC_KEY}';"
        "$result=[ordered]@{"
        "EnableLUA=$k.EnableLUA;"
        "ConsentPromptBehaviorAdmin=$k.ConsentPromptBehaviorAdmin;"
        "PromptOnSecureDesktop=$k.PromptOnSecureDesktop;"
        "LocalAccountTokenFilterPolicy=(Get-ItemProperty -Path '"
        r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
        "' -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy"
        "}"
    )


def set_uac_script(enable_lua: Optional[bool] = None, admin_approval_off: bool = False,
                   token_filter_off: bool = False) -> str:
    lines = [f"$p='{UAC_KEY}'"]
    if enable_lua is not None:
        lines.append(f"Set-ItemProperty -Path $p -Name EnableLUA -Value {1 if enable_lua else 0} -Type DWord")
    if admin_approval_off:
        lines.append("Set-ItemProperty -Path $p -Name ConsentPromptBehaviorAdmin -Value 0 -Type DWord")
    if token_filter_off:
        lines.append("Set-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -Type DWord")
    lines.append("$result=@{changed=$true;note='reboot may be required for EnableLUA changes'}")
    return ";".join(lines)
