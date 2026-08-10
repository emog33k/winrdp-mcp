"""Software / package management: winget, Chocolatey, MSI/EXE installers, uninstall."""

from __future__ import annotations

from typing import Optional

from .. import ps
from . import _validate as V


def register(mcp, ctx) -> None:
    @mcp.tool
    def install_software(
        name: Optional[str] = None,
        url: Optional[str] = None,
        host: Optional[str] = None,
        manager: str = "auto",
        silent_args: str = "/S",
        timeout: int = 1200,
    ) -> dict:
        """Install a program on a box.

        Provide `name` (a winget id or Chocolatey package, e.g. 'Google.Chrome' or
        'googlechrome') and it uses winget or choco (manager='auto' prefers winget then
        choco). Or provide `url` to a .msi/.exe installer; it downloads and runs it
        silently (MSI uses /qn; EXE uses silent_args). Installs run detached (scheduled
        task) so a mid-install WinRM disconnect on a small/busy box doesn't fail them.
        """
        if url:
            body = (
                f"$u={ps.ps_string(url)};$f=Join-Path $env:TEMP ([IO.Path]::GetFileName($u.Split('?')[0]));"
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                "(New-Object Net.WebClient).DownloadFile($u,$f);"
                "if($f -match '\\.msi$'){"
                "Start-Process msiexec -ArgumentList '/i',\"`\"$f`\"\",'/qn','/norestart' -Wait}"
                f"else{{Start-Process $f -ArgumentList {ps.ps_string(silent_args)} -Wait}}"
            )
            r = ctx.run_long(body, host=host, timeout=timeout)
            return {"installer_url": url, "stdout": r.stdout[-2000:], "stderr": r.stderr[-1000:], "rc": r.rc}

        if not name:
            return {"error": "provide name (winget/choco package) or url (installer)"}
        V.package(name, "name")  # goes onto the winget/choco command line

        mgr = manager
        if mgr == "auto":
            probe = ctx.transport_for(host).run_ps(
                "if(Get-Command winget -ErrorAction SilentlyContinue){'winget'}"
                "elseif(Get-Command choco -ErrorAction SilentlyContinue){'choco'}else{'none'}",
                timeout=30,
            )
            mgr = probe.stdout.strip() or "none"
        if mgr == "winget":
            cmd = (f"winget install --id {name} -e --silent --accept-package-agreements "
                   "--accept-source-agreements --disable-interactivity")
        elif mgr == "choco":
            cmd = f"choco install {name} -y --no-progress"
        else:
            return {"error": "no package manager found; call ensure_package_manager first or pass url"}
        r = ctx.run_long(cmd, host=host, timeout=timeout)
        return {"manager": mgr, "stdout": r.stdout[-4000:], "stderr": r.stderr[-1000:], "rc": r.rc}

    @mcp.tool
    def uninstall_software(name: str, host: Optional[str] = None, timeout: int = 600) -> dict:
        """Uninstall a program by display name or package id.

        Tries winget, then choco, then the registry uninstall string (silent)."""
        body = (
            f"$n={ps.ps_string(name)};$done=$false;$log='';"
            "if(Get-Command winget -ErrorAction SilentlyContinue){"
            "$o=winget uninstall --id $n -e --silent --disable-interactivity 2>&1|Out-String;"
            "if($LASTEXITCODE -eq 0){$done=$true};$log+=$o}"
            "if(-not $done -and (Get-Command choco -ErrorAction SilentlyContinue)){"
            "$o=choco uninstall $n -y 2>&1|Out-String;if($LASTEXITCODE -eq 0){$done=$true};$log+=$o}"
            "if(-not $done){"
            "$keys='HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
            "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*';"
            "$app=Get-ItemProperty $keys -ErrorAction SilentlyContinue|"
            "Where-Object{$_.DisplayName -like \"*$n*\"}|Select-Object -First 1;"
            "if($app -and $app.UninstallString){$u=$app.UninstallString;"
            "if($u -match 'msiexec'){$g=[regex]::Match($u,'\\{[0-9A-Fa-f\\-]+\\}').Value;"
            "Start-Process msiexec -ArgumentList '/x',$g,'/qn','/norestart' -Wait;$done=$true}"
            "else{Start-Process cmd -ArgumentList '/c',$u -Wait;$done=$true}}}"
            "$result=@{uninstalled=$done;log=$log.Substring(0,[Math]::Min(2000,$log.Length))}"
        )
        return ctx.exec_json(body, host=host, elevated=True, timeout=timeout)

    @mcp.tool
    def list_installed_software(host: Optional[str] = None, filter: Optional[str] = None) -> list:
        """List installed programs (from the uninstall registry, 32/64-bit + per-user)."""
        where = f"|Where-Object{{$_.DisplayName -like {ps.ps_string('*' + filter + '*')}}}" if filter else ""
        body = (
            "$keys='HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
            "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
            "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*';"
            f"$result=@(Get-ItemProperty $keys -ErrorAction SilentlyContinue|"
            f"Where-Object{{$_.DisplayName}}{where}|Sort-Object DisplayName -Unique|ForEach-Object{{@{{"
            "name=$_.DisplayName;version=$_.DisplayVersion;publisher=$_.Publisher}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def ensure_package_manager(host: Optional[str] = None, manager: str = "choco",
                               timeout: int = 600) -> dict:
        """Install a package manager on a box. manager: 'choco' (Chocolatey) or 'winget'
        (installs the App Installer if missing). Runs elevated."""
        if manager == "choco":
            script = (
                "Set-ExecutionPolicy Bypass -Scope Process -Force;"
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                "iex ((New-Object Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'));"
                "$result=@{installed=(Get-Command choco -ErrorAction SilentlyContinue)-ne $null}"
            )
        elif manager == "winget":
            script = (
                "if(Get-Command winget -ErrorAction SilentlyContinue){$result=@{installed=$true;note='already present'}}"
                "else{$u='https://aka.ms/getwinget';$f=Join-Path $env:TEMP 'winget.msixbundle';"
                "(New-Object Net.WebClient).DownloadFile($u,$f);Add-AppxPackage -Path $f;"
                "$result=@{installed=(Get-Command winget -ErrorAction SilentlyContinue)-ne $null}}"
            )
        else:
            return {"error": "manager must be 'choco' or 'winget'"}
        return ctx.exec_json(script, host=host, elevated=True, timeout=timeout)
