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
# NOTE: do NOT pipe pip's output through `2>&1 | ...`. On Windows PowerShell 5.1 that wraps
# pip's (harmless) stderr advisories in a NativeCommandError which, under ErrorActionPreference
# 'Stop', aborts the build even though the install succeeded. Let pip write to the console and
# gate on the real exit code instead.
& $Python -m pip install --upgrade --target $lib "$root"
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }

# Drop test/dist metadata that bloats the bundle
Get-ChildItem $lib -Directory -Filter "*.dist-info" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $lib -Directory -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==> Zipping bundle"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$out = Join-Path $dist "winrdp-mcp.dxt"
if (Test-Path $out) { Remove-Item $out -Force }

# .dxt is a plain zip with manifest.json at the archive root. Build it manually with
# forward-slash entry names: Windows PowerShell 5.1's Compress-Archive writes BACKSLASH
# separators, which violate the ZIP spec (APPNOTE 4.4.17) and can break the DXT loader.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$fsOut = [System.IO.File]::Open($out, [System.IO.FileMode]::CreateNew)
$zip = New-Object System.IO.Compression.ZipArchive($fsOut, [System.IO.Compression.ZipArchiveMode]::Create)
function Add-ZipEntry($srcFile, $entryName) {
    $entryName = ($entryName -replace '\\', '/')
    $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $es = $entry.Open()
    $in = [System.IO.File]::OpenRead($srcFile)
    try { $in.CopyTo($es) } finally { $in.Dispose(); $es.Dispose() }
}
Add-ZipEntry (Join-Path $dxt "manifest.json") "manifest.json"
$serverDir = Join-Path $dxt "server"
Get-ChildItem $serverDir -Recurse -File | ForEach-Object {
    $rel = "server/" + ($_.FullName.Substring($serverDir.Length).TrimStart('\', '/') -replace '\\', '/')
    Add-ZipEntry $_.FullName $rel
}
$zip.Dispose(); $fsOut.Dispose()

Write-Host "==> Built $out"
Write-Host "    Install: open Claude Desktop -> Settings -> Extensions -> Install from file."
