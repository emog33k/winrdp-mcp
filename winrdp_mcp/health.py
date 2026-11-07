"""Real health matrix — prove the things a single ``$env:COMPUTERNAME`` cannot.

``test_host`` used to call ``online = r.ok`` for one command; ``provision`` marked a host
ready after a bare ``probe()``. Both are false positives: the transport can answer while
the token is filtered, the file channel is unusable, or the scheduled-task path is broken.

Every step below is an observation, not an assumption:

1. identity   — the box's name matches what the caller expects.
2. principal  — full admin token (High/System integrity) and LATFP is really 1.
3. transfer   — 64 KiB random round-trip with a SHA-256 match; chunked is a warning.
4. elevation  — the same one-shot Scheduled Task path run_powershell(elevated=True) uses.
5. session    — qwinsta shows Active (GUI) or at least a console session.
6. platform   — PowerShell version and Windows build.
"""

from __future__ import annotations

import binascii
import hashlib
import os
from typing import Optional

from . import elevation, ps
from .config import REMOTE_TMP
from .transports import Transport

# One round-trip for identity + principal + platform.
_INFO_PS = (
    "$id=[Security.Principal.WindowsIdentity]::GetCurrent();"
    "$pr=New-Object Security.Principal.WindowsPrincipal($id);"
    "$w=Join-Path $env:SystemRoot 'System32\\whoami.exe';$lvl='';"
    "if(Test-Path $w){$ml=@(& $w /groups 2>$null)|Where-Object{$_ -match 'S-1-16-'}|"
    "Select-Object -First 1;"
    "if($ml){$sid=[regex]::Match([string]$ml,'S-1-16-\\d+').Value;"
    "$lvl=switch($sid){'S-1-16-4096'{'Low'}'S-1-16-8192'{'Medium'}'S-1-16-12288'{'High'}"
    "'S-1-16-16384'{'System'}default{$sid}}}};"
    "$p='HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System';"
    "$latfp=(Get-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy "
    "-ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy;"
    "$os=Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue;"
    "$result=[ordered]@{hostname=$env:COMPUTERNAME;user=$id.Name;"
    "is_admin=$pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
    "is_system=($id.User.Value -eq 'S-1-5-18');integrity=$lvl;latfp=$latfp;"
    "ps_version=$PSVersionTable.PSVersion.ToString();"
    "build=if($os){[string]$os.BuildNumber}else{$null}}"
)

# Active vs console, parsed with the same fixed-width header slicing as
# elevation.ACTIVE_SESSION_PROBE_PS. A console row counts even without a logged-on user:
# the box is healthy, only GUI (as_user/screenshot) is unavailable.
_SESSION_PS = (
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
    "$st=($l.Substring($iT).Trim() -split '\\s+')[0];"
    "if($nm -ieq 'console'){$console=$true};"
    "if($u -and $st -ieq 'Active'){$active=$true};"
    "$rows+=@{session=$nm;user=$u;state=$st}}}}};"
    "$result=@{active=$active;console=$console;rows=@($rows)}"
)

_ELEV_PS = (
    "$id=[Security.Principal.WindowsIdentity]::GetCurrent();"
    "$pr=New-Object Security.Principal.WindowsPrincipal($id);"
    "if($pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
    "{Write-Output 'WINRDP_ELEV_OK'};"
    "Write-Output ('WINRDP_ELEV:' + $id.Name)"
)

_TRANSFER_BYTES = 64 * 1024


def _step(name: str, ok: bool, detail: str, **extra) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, **extra}


def _parse(transport: Transport, body: str, timeout: int) -> dict:
    r = transport.run_ps(ps.wrap_json(body), timeout=timeout)
    data = ps.parse_json(r.stdout)  # raises if the payload is absent
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError((data or {}).get("error") or f"no JSON payload (rc={r.rc})")
    return data


def check_health(transport: Transport, *, expected_name: Optional[str] = None,
                 timeout: int = 90) -> dict:
    """Run the matrix. ``ok`` is true only when every step passes.

    The caller gets ``{ok, failed_step, failed_steps, steps, capabilities, hostname}``.
    ``capabilities`` says which interactive tools are usable on this box.
    """
    caps = {"as_user": False, "screenshot": False, "gui": False}
    try:
        info = _parse(transport, _INFO_PS, timeout)
    except Exception as e:  # noqa: BLE001 — a dead transport means every step fails
        detail = f"{type(e).__name__}: {e}"
        steps = [
            _step("identity", False, detail),
            _step("principal", False, detail),
            _step("transfer", False, "not run — identity/principal call failed"),
            _step("elevation", False, "not run — identity/principal call failed"),
            _step("session", False, "not run — identity/principal call failed"),
            _step("platform", False, detail),
        ]
        return _result(steps, caps, None)

    steps = []
    hostname = str(info.get("hostname") or "")

    if expected_name:
        ident_ok = hostname.casefold() == str(expected_name).casefold()
        ident_detail = f"hostname={hostname or '?'}, expected={expected_name}"
    else:
        ident_ok = bool(hostname)
        ident_detail = (f"hostname={hostname or '?'} "
                        "(no expected name configured — set expected_name to enforce it)")
    steps.append(_step("identity", ident_ok, ident_detail))

    is_admin = info.get("is_admin") is True
    integrity = str(info.get("integrity") or "")
    is_system = info.get("is_system") is True
    latfp = info.get("latfp")
    principal_ok = (is_admin and (is_system or integrity in ("High", "System"))
                    and latfp == 1)
    steps.append(_step(
        "principal", principal_ok,
        f"user={info.get('user')}, is_admin={is_admin}, integrity={integrity or '?'}, "
        f"LocalAccountTokenFilterPolicy={latfp}"))

    steps.append(_transfer_step(transport, timeout))

    try:
        er = elevation.run_elevated(transport, _ELEV_PS, timeout=max(60, min(timeout, 180)))
        marker = "WINRDP_ELEV_OK" in (er.stdout or "")
        who = next((ln[len("WINRDP_ELEV:"):] for ln in (er.stdout or "").splitlines()
                    if ln.startswith("WINRDP_ELEV:")), "?")
        # rc alone is not evidence: the elevated wrapper runs with EAP=Continue and
        # $LASTEXITCODE is null for a cmdlet error, so a broken task can exit 0.
        steps.append(_step(
            "elevation", er.rc == 0 and marker,
            f"rc={er.rc}, full-token-marker={'yes' if marker else 'NO'}, identity={who}"))
    except Exception as e:  # noqa: BLE001
        steps.append(_step("elevation", False, f"{type(e).__name__}: {e}"))

    try:
        sess = _parse(transport, _SESSION_PS, timeout)
        active = sess.get("active") is True
        console = sess.get("console") is True
        caps = {"as_user": active, "screenshot": active, "gui": active}
        detail = f"active={active}, console={console}"
        if not active:
            detail += "; as_user/screenshot/GUI unavailable (no Active session)"
        steps.append(_step("session", active or console, detail))
    except Exception as e:  # noqa: BLE001
        steps.append(_step("session", False, f"{type(e).__name__}: {e}"))

    psv = info.get("ps_version")
    build = info.get("build")
    steps.append(_step("platform", bool(psv and build),
                       f"PowerShell {psv or '?'}, build {build or '?'}"))

    return _result(steps, caps, hostname)


def _transfer_step(transport: Transport, timeout: int) -> dict:
    """64 KiB random round-trip with a SHA-256 compare. chunked works but is a warning."""
    rid = binascii.hexlify(os.urandom(5)).decode()
    remote = f"{REMOTE_TMP}\\winrdp_health_{rid}.bin"
    data = os.urandom(_TRANSFER_BYTES)
    expected = hashlib.sha256(data).hexdigest()
    try:
        transport.upload(data, remote, timeout=max(timeout, 120))
        back = transport.download(remote, timeout=max(timeout, 120))
        channel = transport.file_channel() or "chunked"
        match = hashlib.sha256(back).hexdigest() == expected
        warning = channel == "chunked"
        detail = (f"channel={channel}, {len(back)} bytes, "
                  f"sha256={'match' if match else 'MISMATCH'}")
        if warning:
            detail += " (chunked base64 over WinRM is ~50x slower — warning, not success)"
        return _step("transfer", match and not warning, detail,
                     channel=channel, warning=warning)
    except Exception as e:  # noqa: BLE001
        return _step("transfer", False, f"{type(e).__name__}: {e}", channel=None)
    finally:
        try:
            transport.run_ps(
                f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force "
                "-ErrorAction SilentlyContinue", timeout=30)
        except Exception:  # noqa: BLE001
            pass


def _result(steps: list, caps: dict, hostname: Optional[str]) -> dict:
    failed = [s["name"] for s in steps if not s["ok"]]
    return {
        "ok": not failed,
        "failed_step": failed[0] if failed else None,
        "failed_steps": failed,
        "steps": steps,
        "capabilities": caps,
        "hostname": hostname,
    }
