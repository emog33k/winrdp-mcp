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


def run_elevated(
    transport: Transport,
    script: str,
    *,
    timeout: int = 300,
    run_as: str = "SYSTEM",
    password: Optional[str] = None,
    poll: float = 1.5,
) -> ElevatedResult:
    """Execute ``script`` with a full elevated token via a transient Scheduled Task."""
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
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            chk = transport.run_ps(f"Test-Path -LiteralPath {ps.ps_string(done_f)}", timeout=30)
            if "True" in chk.stdout:
                break
            time.sleep(poll)
        else:
            raise TransportError(f"elevated task timed out after {timeout}s")

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
        transport.run_ps(
            f"Unregister-ScheduledTask -TaskName {ps.ps_string(task)} -Confirm:$false -ErrorAction SilentlyContinue;"
            "Remove-Item -LiteralPath " + ",".join(
                ps.ps_string(p) for p in (remote_ps, out_f, err_f, done_f)
            ) + " -Force -ErrorAction SilentlyContinue",
            timeout=30,
        )


def _safe_read(transport: Transport, path: str) -> str:
    r = transport.run_ps(
        f"if(Test-Path -LiteralPath {ps.ps_string(path)}){{Get-Content -LiteralPath {ps.ps_string(path)} -Raw}}",
        timeout=60,
    )
    return r.stdout


def run_in_user_session(transport: Transport, script: str, *, timeout: int = 120) -> ExecResult:
    """Run ``script`` inside the active interactive user's session (for GUI work).

    Uses a Scheduled Task bound to the console user with an interactive logon so the
    process attaches to the visible desktop. Output capture is best-effort.
    """
    rid = _rid()
    task = f"winrdp_ui_{rid}"
    remote_ps = f"{REMOTE_TMP}\\{task}.ps1"
    out_f = f"{REMOTE_TMP}\\{task}.out"
    done_f = f"{REMOTE_TMP}\\{task}.done"

    # Discover the console session's user.
    who = transport.run_ps(
        "$u=(Get-CimInstance Win32_ComputerSystem).UserName; if(-not $u){"
        "$s=quser 2>$null; if($s){($s|Select-Object -Skip 1|ForEach-Object{($_ -replace '^>','').Trim().Split(' ')[0]}|Select-Object -First 1)}}"
        " else {$u}",
        timeout=30,
    )
    user = (who.stdout or "").strip().splitlines()[-1].strip() if who.stdout.strip() else ""
    if not user:
        raise TransportError("no interactive user session found to run GUI script in")

    wrapper = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "try{"
        f"& {{{script}}} *> {ps.ps_string(out_f)};$rc=$LASTEXITCODE;if($null -eq $rc){{$rc=0}}"
        f"}}catch{{$_|Out-File -FilePath {ps.ps_string(out_f)} -Append -Encoding utf8;$rc=1"
        f"}}finally{{Set-Content -LiteralPath {ps.ps_string(done_f)} -Value \"rc=$rc\" -Encoding ascii}}"
    )
    transport.upload(wrapper.encode("utf-8-sig"), remote_ps)
    arg = f'-NoProfile -ExecutionPolicy Bypass -File "{remote_ps}"'
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
        transport.run_ps(
            f"Unregister-ScheduledTask -TaskName {ps.ps_string(task)} -Confirm:$false -ErrorAction SilentlyContinue;"
            f"Remove-Item -LiteralPath {ps.ps_string(remote_ps)},{ps.ps_string(out_f)},{ps.ps_string(done_f)} -Force -ErrorAction SilentlyContinue",
            timeout=30,
        )


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
