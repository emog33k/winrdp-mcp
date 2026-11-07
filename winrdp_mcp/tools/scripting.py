"""One-shot script execution: run Python / Node / any script on a box straight through MCP.

The point: hand Claude "here's some code, run it on the box" with zero ceremony — the
runtime is located, installed on demand if missing, dependencies are pip-installed, the
code is staged, executed, and stdout/stderr/rc come back.
"""

from __future__ import annotations

import binascii
import os
import shlex
from typing import Optional

from .. import ps
from ..config import REMOTE_TMP
from ..progress import Context, Progress


def _rid(ext: str) -> str:
    return f"{REMOTE_TMP}\\winrdp_{binascii.hexlify(os.urandom(5)).decode()}.{ext}"


def _rid_posix(ext: str) -> str:
    return f"/tmp/winrdp_{binascii.hexlify(os.urandom(5)).decode()}.{ext}"


def _is_posix(transport) -> bool:
    return bool(getattr(transport, "is_posix", lambda: False)())


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
        kill_on_timeout: bool = False,
    ) -> dict:
        """Run inline Python code ON A REMOTE BOX and return its output.

        This NEVER runs on the operator machine. `host` names a registered box; omitting it
        targets the *active* host (the one set with use_host — list_hosts shows which). It
        is not a local/remote switch: `host=None` still means remote. To execute on the
        operator machine itself, register it once with
        add_host("local", "localhost", transport="local") and pass host="local".

        The code is staged to a temp .py on the box, run by the box's own Python
        interpreter, then deleted. `python` in the result is the interpreter path that ran
        it and `host` is the box it ran on — check them if you are unsure where it executed.

        `pip`: optional comma/space-separated packages to install first (on the box).
        `ensure_python`: install Python automatically (winget) if it's missing.
        `elevated`/`as_user`: full-token / interactive-desktop execution. On a POSIX SSH
        host, `elevated=True` prefixes `sudo -n`; `as_user`/`kill_on_timeout` are Windows-only.
        `kill_on_timeout`: kill the remote python process when `timeout` expires — by
        default a timeout only abandons the call and the script keeps running on the box.
        Returns {host, python, stdout, stderr, rc}.
        """
        alias = ctx.resolve(host).alias
        t = ctx.transport_for(host)
        posix = _is_posix(t)
        py = _find_python(t)
        if not py and ensure_python and not posix:
            _install_python(ctx, host)
            py = _find_python(t)
        if not py:
            return {"host": alias,
                    "error": "Python not found on the box and could not be installed. "
                             "Call ensure_runtime or install manually."}

        pkgs = " ".join(p.strip() for p in pip.replace(",", " ").split() if p.strip())
        if posix:
            if pkgs:
                t.run_sh(f"{shlex.quote(py)} -m pip install --disable-pip-version-check "
                         f"-q {pkgs}", timeout=timeout).raise_for_status("pip install")
            remote = _rid_posix("py")
            t.upload(code.encode("utf-8"), remote)
            runner = f"{shlex.quote(py)} {shlex.quote(remote)} {args}".strip()
            if elevated:
                runner = "sudo -n " + runner
            try:
                r = t.run_sh(runner, timeout=timeout)
                return {"host": alias, "python": py, "stdout": r.stdout, "stderr": r.stderr,
                        "rc": r.rc}
            finally:
                t.run_sh(f"rm -f {shlex.quote(remote)}", timeout=30)

        if pkgs:
            t.run_ps(f"& {ps.ps_string(py)} -m pip install --disable-pip-version-check -q {pkgs}",
                     timeout=timeout).raise_for_status("pip install")

        remote = _rid("py")
        t.run_ps(ps.ensure_remote_dirs())
        t.upload(code.encode("utf-8"), remote)
        cmd = f'& {ps.ps_string(py)} {ps.ps_string(remote)} {args}'.strip()
        try:
            r = ctx.exec_ps(cmd, host=host, elevated=elevated, as_user=as_user,
                            timeout=timeout, kill_on_timeout=kill_on_timeout)
            return {"host": alias, "python": py, "stdout": r.stdout, "stderr": r.stderr,
                    "rc": r.rc}
        finally:
            t.run_ps(f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force -ErrorAction SilentlyContinue")

    @mcp.tool
    def run_python_file(local_path: str, host: Optional[str] = None, args: str = "",
                        pip: str = "", elevated: bool = False, timeout: int = 600,
                        kill_on_timeout: bool = False) -> dict:
        """Upload a .py file from the operator machine and run it ON A REMOTE BOX (the one
        named by `host`, or the active host). Only the file is read locally; execution is
        remote — see run_python for the host semantics."""
        with open(local_path, "r", encoding="utf-8") as f:
            code = f.read()
        return run_python(code, host=host, args=args, pip=pip, elevated=elevated,
                          timeout=timeout, kill_on_timeout=kill_on_timeout)

    @mcp.tool
    def pip_install(packages: str, host: Optional[str] = None, timeout: int = 600,
                    mcp_ctx: Context | None = None) -> dict:
        """pip-install one or more packages on a box (comma/space separated)."""
        prog = Progress(mcp_ctx, 100)
        t = ctx.transport_for(host)
        py = _find_python(t)
        if not py:
            return {"error": "Python not found; call ensure_runtime first"}
        pkgs = " ".join(p.strip() for p in packages.replace(",", " ").split() if p.strip())
        prog.report(10, f"pip installing {pkgs} (can take a while)")
        if _is_posix(t):
            r = t.run_sh(f"{shlex.quote(py)} -m pip install --disable-pip-version-check {pkgs}",
                         timeout=timeout)
        else:
            r = t.run_ps(f"& {ps.ps_string(py)} -m pip install --disable-pip-version-check {pkgs}",
                         timeout=timeout)
        prog.report(100, "pip install finished")
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
        kill_on_timeout: bool = False,
    ) -> dict:
        """Run a script in any interpreter ON A REMOTE BOX (`host`, or the active host —
        never locally; see run_python for the host semantics).

        interpreter: auto | python | node | powershell | cmd | bat | vbscript (plus sh/bash
        on a POSIX SSH host). 'auto' guesses from a shebang or obvious syntax, defaulting
        to PowerShell. PowerShell content runs with $ErrorActionPreference='Stop'.
        `kill_on_timeout`: kill the remote process when `timeout` expires instead of
        leaving it running.
        Returns {host, interpreter, stdout, stderr, rc}.
        """
        interp = interpreter
        if interp == "auto":
            interp = _guess_interpreter(content)

        alias = ctx.resolve(host).alias
        t = ctx.transport_for(host)
        posix = _is_posix(t)

        if posix:
            if interp not in ("python", "node", "sh", "bash"):
                return {"host": alias,
                        "error": f"interpreter '{interp}' is not available on a POSIX host; "
                                 "use sh/bash/python/node"}
            ext = {"python": "py", "node": "js", "sh": "sh", "bash": "sh"}[interp]
            remote = _rid_posix(ext)
            t.upload(content.encode("utf-8"), remote)
            runner = {"python": _find_python(t) or "python3", "node": "node",
                      "sh": "sh", "bash": "bash"}[interp]
            try:
                r = t.run_sh(f"{shlex.quote(runner)} {shlex.quote(remote)} {args}".strip(),
                             timeout=timeout)
                return {"host": alias, "interpreter": interp, "stdout": r.stdout,
                        "stderr": r.stderr, "rc": r.rc}
            finally:
                t.run_sh(f"rm -f {shlex.quote(remote)}", timeout=30)

        t.run_ps(ps.ensure_remote_dirs())

        if interp == "powershell":
            r = ctx.exec_ps(content, host=host, elevated=elevated, as_user=as_user,
                            timeout=timeout, kill_on_timeout=kill_on_timeout, wrap=True)
            return {"host": alias, "interpreter": interp, "stdout": r.stdout,
                    "stderr": r.stderr, "rc": r.rc}

        if interp in ("sh", "bash"):
            return {"host": alias, "error": "sh/bash require a POSIX SSH host (transport=ssh)"}

        ext = {"python": "py", "node": "js", "cmd": "bat", "bat": "bat", "vbscript": "vbs"}.get(interp)
        if not ext:
            return {"host": alias, "error": f"unknown interpreter '{interpreter}'"}
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
            r = ctx.exec_ps(runner.strip(), host=host, elevated=elevated, as_user=as_user,
                            timeout=timeout, kill_on_timeout=kill_on_timeout)
            return {"host": alias, "interpreter": interp, "stdout": r.stdout,
                    "stderr": r.stderr, "rc": r.rc}
        finally:
            t.run_ps(f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force -ErrorAction SilentlyContinue")

    @mcp.tool
    def run_sh(script: str, host: Optional[str] = None, timeout: int = 600) -> dict:
        """Run a POSIX shell command on a Linux/macOS SSH host (not Windows).

        Uses `sh -lc` over the SSH transport; the host must be registered with
        transport="ssh" and actually run a POSIX shell.
        """
        alias = ctx.resolve(host).alias
        t = ctx.transport_for(host)
        if not _is_posix(t):
            return {"host": alias,
                    "error": "host has no POSIX shell; use run_powershell/run_cmd for Windows"}
        r = t.run_sh(script, timeout=timeout)
        return {"host": alias, "interpreter": "sh", "stdout": r.stdout, "stderr": r.stderr,
                "rc": r.rc}

    @mcp.tool
    def run_node(code: str, host: Optional[str] = None, args: str = "", timeout: int = 300,
                 kill_on_timeout: bool = False) -> dict:
        """Run inline Node.js code ON A REMOTE BOX (Node must be installed there)."""
        return run_script(code, interpreter="node", host=host, args=args, timeout=timeout,
                          kill_on_timeout=kill_on_timeout)

    @mcp.tool
    def ensure_runtime(runtime: str = "python", host: Optional[str] = None, timeout: int = 900,
                       mcp_ctx: Context | None = None) -> dict:
        """Ensure a language runtime is installed on a box. runtime: python | node.
        Installs via winget (falls back to choco). Runs elevated."""
        prog = Progress(mcp_ctx, 100)
        if _is_posix(ctx.transport_for(host)):
            return {"error": "ensure_runtime installs Windows runtimes; on a POSIX host "
                             "use the distro package manager (apt/dnf/apk)"}
        if runtime == "python":
            if _find_python(ctx.transport_for(host)):
                return {"runtime": "python", "installed": True, "note": "already present"}
            prog.report(10, "installing Python runtime (download + install, a few minutes)")
            _install_python(ctx, host, timeout=timeout, progress=prog.heartbeat("installing Python"))
            res = {"runtime": "python", "installed": bool(_find_python(ctx.transport_for(host)))}
            prog.report(100, "Python runtime ready")
            return res
        if runtime == "node":
            prog.report(10, "installing Node.js runtime (download + install, a few minutes)")
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
            out = ctx.exec_json(body, host=host, elevated=True, timeout=timeout)
            prog.report(100, "Node runtime step finished")
            return out
        return {"error": "runtime must be python|node"}


# ---- helpers --------------------------------------------------------------
def _find_python(transport) -> Optional[str]:
    """Return a usable python executable path on the box, or None.

    Resolves the concrete executable path (not just PATH) so it still works right after a
    fresh install — a new WinRM shell can inherit a stale PATH that doesn't yet include the
    just-installed Python, so we also glob the standard install locations.
    """
    if _is_posix(transport):
        r = transport.run_sh("command -v python3 || command -v python", timeout=30)
        p = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
        return p[-1] if p else None
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


def _install_python(ctx, host, timeout: int = 1200, progress=None) -> None:
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
    ctx.run_long(body, host=host, timeout=timeout, progress=progress)


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
        if "bash" in line:
            return "bash"
        if line.rstrip().endswith("sh"):
            return "sh"
    if head.startswith("import ") or head.startswith("from ") or "print(" in head or "def " in head:
        return "python"
    if "console.log" in head or head.startswith("const ") or head.startswith("require("):
        return "node"
    if head.startswith("@echo") or head.startswith("echo off"):
        return "cmd"
    return "powershell"
