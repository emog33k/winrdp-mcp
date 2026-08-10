#requires -Version 5.1
<#
.SYNOPSIS
    Build a self-contained .dxt (MCP Bundle) for one-click install into Claude Desktop.
.DESCRIPTION
    Vendors winrdp-mcp and all its dependencies into dxt/server/lib, then zips the dxt/
    folder into dist/winrdp-mcp.dxt. The result runs with the desktop's bundled Python —
    no pip, no network at install time.
.EXAMPLE
    pwsh dxt/build.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutDir = "dist"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # repo root
$dxt  = Join-Path $root "dxt"
$lib  = Join-Path $dxt "server\lib"
$dist = Join-Path $root $OutDir

Write-Host "==> Cleaning $lib"
if (Test-Path $lib) { Remove-Item $lib -Recurse -Force }
New-Item -ItemType Directory -Force -Path $lib | Out-Null

Write-Host "==> Vendoring winrdp-mcp + deps into server/lib"
& $Python -m pip install --upgrade --target $lib "$root" 2>&1 | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# Drop test/dist metadata that bloats the bundle
Get-ChildItem $lib -Directory -Filter "*.dist-info" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $lib -Directory -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==> Zipping bundle"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$out = Join-Path $dist "winrdp-mcp.dxt"
if (Test-Path $out) { Remove-Item $out -Force }
# .dxt is a plain zip of the dxt/ contents (manifest.json at the archive root)
Compress-Archive -Path (Join-Path $dxt "manifest.json"), (Join-Path $dxt "server") -DestinationPath "$out.zip" -Force
Move-Item "$out.zip" $out -Force

Write-Host "==> Built $out"
Write-Host "    Install: open Claude Desktop -> Settings -> Extensions -> Install from file."
