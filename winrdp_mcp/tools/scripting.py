"""One-shot script execution: run Python / Node / any script on a box straight through MCP.

The point: hand Claude "here's some code, run it on the box" with zero ceremony — the
runtime is located, installed on demand if missing, dependencies are pip-installed, the
code is staged, executed, and stdout/stderr/rc come back.
"""

from __future__ import annotations

import binascii
import os
from typing import Optional

from .. import ps
from ..config import REMOTE_TMP


def _rid(ext: str) -> str:
    return f"{REMOTE_TMP}\\winrdp_{binascii.hexlify(os.urandom(5)).decode()}.{ext}"


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ python
    @mcp.tool
    def run_python(
        code: str,
        host: Optional[str] = None,
        args: str = "",
        pip: str = "",
        ensure_python: bool = True,
        elevated: bool = False,
        as_user: bool = False,
        timeout: int = 600,
    ) -> dict:
        """Run inline Python code on a box and return its output.

        `pip`: optional comma/space-separated packages to install first.
        `ensure_python`: install Python automatically (winget) if it's missing.
        `elevated`/`as_user`: full-token / interactive-desktop execution.
        Returns {python, stdout, stderr, rc}.
        """
        t = ctx.transport_for(host)
        py = _find_python(t)
        if not py and ensure_python:
            _install_python(ctx, host)
            py = _find_python(t)
        if not py:
            return {"error": "Python not found and could not be installed. "
                             "Call ensure_runtime or install manually."}

        if pip:
            pkgs = " ".join(p.strip() for p in pip.replace(",", " ").split() if p.strip())
            t.run_ps(f"& {ps.ps_string(py)} -m pip install --disable-pip-version-check -q {pkgs}",
                     timeout=timeout).raise_for_status("pip install")

        remote = _rid("py")
        t.run_ps(ps.ensure_remote_dirs())
        t.upload(code.encode("utf-8"), remote)
        cmd = f'& {ps.ps_string(py)} {ps.ps_string(remote)} {args}'.strip()
        try:
            r = ctx.exec_ps(cmd, host=host, elevated=elevated, as_user=as_user, timeout=timeout)
            return {"python": py, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
        finally:
            t.run_ps(f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force -ErrorAction SilentlyContinue")

    @mcp.tool
    def run_python_file(local_path: str, host: Optional[str] = None, args: str = "",
                        pip: str = "", elevated: bool = False, timeout: int = 600) -> dict:
        """Upload a .py file from the operator machine and run it on a box."""
        with open(local_path, "r", encoding="utf-8") as f:
            code = f.read()
        return run_python(code, host=host, args=args, pip=pip, elevated=elevated, timeout=timeout)

    @mcp.tool
    def pip_install(packages: str, host: Optional[str] = None, timeout: int = 600) -> dict:
        """pip-install one or more packages on a box (comma/space separated)."""
        t = ctx.transport_for(host)
        py = _find_python(t)
        if not py:
            return {"error": "Python not found; call ensure_runtime first"}
        pkgs = " ".join(p.strip() for p in packages.replace(",", " ").split() if p.strip())
        r = t.run_ps(f"& {ps.ps_string(py)} -m pip install --disable-pip-version-check {pkgs}", timeout=timeout)
        return {"python": py, "stdout": r.stdout[-4000:], "stderr": r.stderr[-1000:], "rc": r.rc}

    # ------------------------------------------------------------------ generic
    @mcp.tool
    def run_script(
        content: str,
        interpreter: str = "auto",
        host: Optional[str] = None,
        args: str = "",
        elevated: bool = False,
        as_user: bool = False,
        timeout: int = 600,
    ) -> dict:
        """Run a script in any interpreter on a box.

        interpreter: auto | python | node | powershell | cmd | bat | vbscript.
        'auto' guesses from a shebang or obvious syntax, defaulting to PowerShell.
        Returns {interpreter, stdout, stderr, rc}.
        """
        interp = interpreter
        if interp == "auto":
            interp = _guess_interpreter(content)

        t = ctx.transport_for(host)
        t.run_ps(ps.ensure_remote_dirs())

        if interp == "powershell":
            r = ctx.exec_ps(content, host=host, elevated=elevated, as_user=as_user, timeout=timeout)
            return {"interpreter": interp, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

        ext = {"python": "py", "node": "js", "cmd": "bat", "bat": "bat", "vbscript": "vbs"}.get(interp)
        if not ext:
            return {"error": f"unknown interpreter '{interpreter}'"}
        remote = _rid(ext)
        t.upload(content.encode("utf-8"), remote)
        runner = {
            "python": lambda p: f"& {ps.ps_string(_find_python(t) or 'python')} {ps.ps_string(p)} {args}",
            "node": lambda p: f"& node {ps.ps_string(p)} {args}",
            "cmd": lambda p: f"& cmd /c {ps.ps_string(p)} {args}",
            "bat": lambda p: f"& cmd /c {ps.ps_string(p)} {args}",
            "vbscript": lambda p: f"& cscript //nologo {ps.ps_string(p)} {args}",
        }[interp](remote)
        try:
            r = ctx.exec_ps(runner.strip(), host=host, elevated=elevated, as_user=as_user, timeout=timeout)
            return {"interpreter": interp, "stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
        finally:
            t.run_ps(f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force -ErrorAction SilentlyContinue")

    @mcp.tool
    def run_node(code: str, host: Optional[str] = None, args: str = "", timeout: int = 300) -> dict:
        """Run inline Node.js code on a box (Node must be installed)."""
        return run_script(code, interpreter="node", host=host, args=args, timeout=timeout)

    @mcp.tool
    def ensure_runtime(runtime: str = "python", host: Optional[str] = None, timeout: int = 900) -> dict:
        """Ensure a language runtime is installed on a box. runtime: python | node.
        Installs via winget (falls back to choco). Runs elevated."""
        if runtime == "python":
            if _find_python(ctx.transport_for(host)):
                return {"runtime": "python", "installed": True, "note": "already present"}
            _install_python(ctx, host, timeout=timeout)
            return {"runtime": "python", "installed": bool(_find_python(ctx.transport_for(host)))}
        if runtime == "node":
            body = (
                "if(Get-Command node -ErrorAction SilentlyContinue){$result=@{installed=$true;note='present'}}"
                "elseif(Get-Command winget -ErrorAction SilentlyContinue){"
                "winget install --id OpenJS.NodeJS -e --silent --accept-package-agreements "
                "--accept-source-agreements --disable-interactivity|Out-Null;"
                "$result=@{installed=((Get-Command node -ErrorAction SilentlyContinue)-ne $null)}}"
                "elseif(Get-Command choco -ErrorAction SilentlyContinue){choco install nodejs -y --no-progress|Out-Null;"
                "$result=@{installed=((Get-Command node -ErrorAction SilentlyContinue)-ne $null)}}"
                "else{$result=@{installed=$false;error='no package manager; call ensure_package_manager'}}"
            )
            return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)
        return {"error": "runtime must be python|node"}


# ---- helpers --------------------------------------------------------------
def _find_python(transport) -> Optional[str]:
    """Return a usable python.exe path on the box, or None.

    Resolves the concrete executable path (not just PATH) so it still works right after a
    fresh install — a new WinRM shell can inherit a stale PATH that doesn't yet include the
    just-installed Python, so we also glob the standard install locations.
    """
    r = transport.run_ps(
        "$cands=New-Object Collections.Generic.List[string];"
        "$c=Get-Command python.exe -ErrorAction SilentlyContinue|"
        "Where-Object{$_.Source -notlike '*WindowsApps*'};"
        "if($c){$cands.Add($c.Source)}"
        "$g=@('C:\\Program Files\\Python3*\\python.exe','C:\\Program Files (x86)\\Python3*\\python.exe',"
        "'C:\\Python3*\\python.exe',(Join-Path $env:LOCALAPPDATA 'Programs\\Python\\Python3*\\python.exe'));"
        "Get-ChildItem $g -ErrorAction SilentlyContinue|Sort-Object FullName -Descending|"
        "ForEach-Object{$cands.Add($_.FullName)};"
        "$py=Get-Command py.exe -ErrorAction SilentlyContinue;if($py){$cands.Add($py.Source)}"
        "$cands|Where-Object{$_ -and (Test-Path $_)}|Select-Object -First 1",
        timeout=30,
    )
    p = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
    return p[-1] if p else None


def _install_python(ctx, host, timeout: int = 1200) -> None:
    # winget/choco/the .exe installer take minutes; run detached (scheduled task) + poll so
    # a mid-install WinRM disconnect on a small/busy box doesn't fail the install.
    body = (
        "if(Get-Command winget -ErrorAction SilentlyContinue){"
        "winget install --id Python.Python.3.12 -e --silent --scope machine "
        "--accept-package-agreements --accept-source-agreements --disable-interactivity}"
        "elseif(Get-Command choco -ErrorAction SilentlyContinue){choco install python -y --no-progress}"
        "else{"  # last resort: direct installer
        "$u='https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe';"
        "$f=Join-Path $env:TEMP 'py.exe';(New-Object Net.WebClient).DownloadFile($u,$f);"
        "Start-Process $f -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1' -Wait}"
    )
    ctx.run_long(body, host=host, timeout=timeout)


def _guess_interpreter(content: str) -> str:
    head = content.lstrip()[:200].lower()
    if head.startswith("#!"):
        line = head.splitlines()[0]
        if "python" in line:
            return "python"
        if "node" in line:
            return "node"
        if "cscript" in line or "wscript" in line:
            return "vbscript"
    if head.startswith("import ") or head.startswith("from ") or "print(" in head or "def " in head:
        return "python"
    if "console.log" in head or head.startswith("const ") or head.startswith("require("):
        return "node"
    if head.startswith("@echo") or head.startswith("echo off"):
        return "cmd"
    return "powershell"
