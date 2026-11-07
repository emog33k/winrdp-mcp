"""On-demand staging of helper scripts and tools onto managed boxes.

Claude can pull whatever it needs onto a box mid-task: a PowerShell module, a
Sysinternals binary, an arbitrary .exe, or a script it just wrote locally. Everything
lands under ``C:\\ProgramData\\winrdp-mcp\\tools`` and is tracked in a manifest so it can
be listed and cleaned up.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from . import config, ps
from .transports import Transport

# Curated shortcuts so the model can say ``stage_tool("psexec")`` and be done.
PRESETS: dict[str, str] = {
    "psexec": "https://live.sysinternals.com/PsExec64.exe",
    "handle": "https://live.sysinternals.com/handle64.exe",
    "procdump": "https://live.sysinternals.com/procdump64.exe",
    "autoruns": "https://live.sysinternals.com/autoruns64.exe",
    "tcpview": "https://live.sysinternals.com/tcpview64.exe",
    "pslist": "https://live.sysinternals.com/pslist64.exe",
    "accesschk": "https://live.sysinternals.com/accesschk64.exe",
    "sigcheck": "https://live.sysinternals.com/sigcheck64.exe",
}


def _remote_path(name: str) -> str:
    return config.REMOTE_TOOLS + "\\" + name


def stage_script(transport: Transport, content: str, name: str) -> dict:
    """Write a script/text file into the tools cache and return its remote path."""
    if not name.lower().endswith((".ps1", ".bat", ".cmd", ".py", ".txt")):
        name += ".ps1"
    transport.run_ps(ps.ensure_remote_dirs()).raise_for_status("ensure dirs")
    remote = _remote_path(name)
    transport.upload(content.encode("utf-8"), remote)
    _record(transport, name, remote, source="inline", sha256=_sha(content.encode()))
    return {"name": name, "remote_path": remote, "bytes": len(content.encode())}


def stage_local_file(transport: Transport, local_path: str, name: Optional[str] = None) -> dict:
    """Upload a file from the operator machine into the box's tools cache."""
    with open(local_path, "rb") as f:
        data = f.read()
    name = name or os.path.basename(local_path)
    transport.run_ps(ps.ensure_remote_dirs()).raise_for_status("ensure dirs")
    remote = _remote_path(name)
    transport.upload(data, remote)
    _record(transport, name, remote, source=f"local:{local_path}", sha256=_sha(data))
    return {"name": name, "remote_path": remote, "bytes": len(data)}


def stage_tool(transport: Transport, source: str, name: Optional[str] = None,
               timeout: int = 300) -> dict:
    """Fetch a tool onto the box.

    ``source`` may be a preset name (see :data:`PRESETS`), an http(s) URL (downloaded
    *on the box* via .NET WebClient), or a local file path on the operator machine.
    """
    if source in PRESETS:
        url = PRESETS[source]
        name = name or os.path.basename(url)
        return _download_on_box(transport, url, name, timeout)
    if source.lower().startswith(("http://", "https://")):
        name = name or os.path.basename(source.split("?", 1)[0]) or "download.bin"
        return _download_on_box(transport, source, name, timeout)
    # treat as a local file
    return stage_local_file(transport, source, name)


def _download_on_box(transport: Transport, url: str, name: str, timeout: int) -> dict:
    transport.run_ps(ps.ensure_remote_dirs()).raise_for_status("ensure dirs")
    remote = _remote_path(name)
    body = (
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
        f"(New-Object Net.WebClient).DownloadFile({ps.ps_string(url)},{ps.ps_string(remote)});"
        f"$fi=Get-Item {ps.ps_string(remote)};"
        "$result=@{name=$fi.Name;remote_path=$fi.FullName;bytes=$fi.Length;"
        "sha256=(Get-FileHash $fi.FullName -Algorithm SHA256).Hash}"
    )
    r = transport.run_ps(ps.wrap_json(body), timeout=timeout)
    data = ps.parse_json(r.stdout)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"download failed: {data['error']}")
    _record(transport, name, remote, source=f"url:{url}", sha256=data.get("sha256", ""))
    return data


def list_staged(transport: Transport) -> list:
    body = (
        f"if(Test-Path {ps.ps_string(config.REMOTE_TOOLS)}){{"
        f"$result=@(Get-ChildItem {ps.ps_string(config.REMOTE_TOOLS)} -File|ForEach-Object{{"
        "@{name=$_.Name;path=$_.FullName;bytes=$_.Length;"
        "modified=$_.LastWriteTimeUtc.ToString('o')}})}else{$result=@()}"
    )
    r = transport.run_ps(ps.wrap_json(body))
    return ps.as_list(ps.parse_json(r.stdout))


def cleanup(transport: Transport, name: Optional[str] = None,
            dry_run: bool = False) -> dict:
    """Remove a staged tool (or the whole cache) and report exactly what it covered.

    dry_run=True returns the same manifest without deleting — a saved profile of what a
    cleanup would touch.
    """
    target = _remote_path(name) if name else config.REMOTE_TOOLS
    remove = ("" if dry_run else
              "Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue;")
    body = (
        f"$t={ps.ps_string(target)};$files=@();"
        "if(Test-Path -LiteralPath $t){"
        "$files=@(Get-ChildItem -LiteralPath $t -Recurse -File -ErrorAction SilentlyContinue|"
        "ForEach-Object{@{path=$_.FullName;bytes=$_.Length}});"
        + remove +
        "};"
        "$sum=0;foreach($f in $files){$sum+=[int64]$f.bytes};"
        f"$result=@{{target=$t;dry_run=${'true' if dry_run else 'false'};"
        "count=@($files).Count;bytes=$sum;files=@($files)}"
    )
    r = transport.run_ps(ps.wrap_json(body))
    return ps.parse_json(r.stdout)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(transport: Transport, name: str, remote: str, source: str, sha256: str) -> None:
    """Append to an on-box manifest (best-effort)."""
    manifest = config.REMOTE_TOOLS + "\\_manifest.jsonl"
    line = (
        '{"name":' + ps_json(name) + ',"path":' + ps_json(remote)
        + ',"source":' + ps_json(source) + ',"sha256":' + ps_json(sha256) + "}"
    )
    try:
        transport.run_ps(
            f"Add-Content -LiteralPath {ps.ps_string(manifest)} -Value {ps.ps_string(line)}",
            timeout=30,
        )
    except Exception:
        pass


def ps_json(s: str) -> str:
    import json
    return json.dumps(s)
