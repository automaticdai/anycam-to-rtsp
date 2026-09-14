#Requires -Version 5.1
<#
.SYNOPSIS
  Allow the WSL client to reach this machine's RTSP port.

.DESCRIPTION
  REQUIRES ADMINISTRATOR. Changes Windows Firewall state.

  Under WSL2's default NAT networking, WSL reaches Windows via the default
  gateway, and inbound connections on the vEthernet (WSL) adapter are blocked
  by default. The symptom is "connection refused" from WSL with nothing
  explaining why.

  An alternative that removes this whole class of problem instead of working
  around it: put

      [wsl2]
      networkingMode=mirrored

  in %USERPROFILE%\.wslconfig, then run `wsl --shutdown`. Requires Windows 11
  22H2 or newer. With mirrored networking, WSL reaches the host on 127.0.0.1
  and no firewall rule is needed.

.PARAMETER Remove
  Delete the rule instead of creating it.
#>
[CmdletBinding()]
param(
    [int] $Port = 8554,
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This script needs Administrator rights.' -ForegroundColor Red
    Write-Host 'Right-click PowerShell and choose "Run as administrator", then re-run it.'
    exit 1
}

$name = "anycam-to-rtsp RTSP $Port (WSL)"

if ($Remove) {
    $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $name
        Write-Host "Removed: $name" -ForegroundColor Green
    } else {
        Write-Host "No such rule: $name" -ForegroundColor Yellow
    }
    exit 0
}

if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
    Write-Host "Rule already exists: $name" -ForegroundColor Yellow
    exit 0
}

New-NetFirewallRule -DisplayName $name `
                    -Direction Inbound `
                    -Protocol TCP `
                    -LocalPort $Port `
                    -Action Allow `
                    -Profile Any | Out-Null

Write-Host "Added: $name (inbound TCP $Port)" -ForegroundColor Green
Write-Host ''
Write-Host 'Verify from WSL:' -ForegroundColor Cyan
Write-Host "    curl -v telnet://`$(ip route show default | awk '{print `$3}'):$Port"
