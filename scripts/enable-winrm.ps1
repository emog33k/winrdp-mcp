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

Set-Item -Path WSMan:\localhost\Service\Auth\Basic       -Value $true  -ErrorAction SilentlyContinue
Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $true  -ErrorAction SilentlyContinue
Set-Item -Path WSMan:\localhost\Client\TrustedHosts      -Value '*' -Force -ErrorAction SilentlyContinue

# Give non-builtin local admins a full token over the network.
$p = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null

Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
netsh advfirewall firewall add rule name='WinRM-HTTP-In-5985' dir=in action=allow protocol=TCP localport=5985 2>$null

Write-Output 'WINRDP_WINRM_ENABLED'
