# enable-winrm.ps1 — make a Windows box remotely manageable over WinRM.
# Paste into an elevated PowerShell (or RDP) session once. Idempotent.
$ErrorActionPreference = 'Stop'

# Public network profile blocks WinRM quickconfig; make active connections Private.
try {
    Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } |
        Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
} catch {}

Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
winrm quickconfig -quiet -force 2>$null

# NOTE: Basic auth, AllowUnencrypted, and TrustedHosts=* are intentionally NOT enabled.
# The default NTLM transport encrypts the message payload even over HTTP 5985, so they are
# unnecessary and would only weaken the box. Enable them yourself only if you specifically
# need Basic-over-HTTP.

# Give non-builtin local admins a full token over the network.
$p = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null

Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
netsh advfirewall firewall add rule name='WinRM-HTTP-In-5985' dir=in action=allow protocol=TCP localport=5985 2>$null

Write-Output 'WINRDP_WINRM_ENABLED'
