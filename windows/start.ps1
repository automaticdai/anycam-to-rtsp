#Requires -Version 5.1
<#
.SYNOPSIS
  Run the MediaMTX server with the generated configuration.

.DESCRIPTION
  MediaMTX starts one ffmpeg process per camera and restarts any that die,
  so an unplugged camera recovers without restarting this server. Leave it
  running; Ctrl+C stops it.
#>
[CmdletBinding()]
param(
    [string] $Config = 'mediamtx.yml'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$mediamtx = Join-Path $Root 'bin\mediamtx.exe'
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }

if (-not (Test-Path $mediamtx)) {
    Write-Host 'mediamtx.exe not found. Run .\setup.ps1 first.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $configPath)) {
    Write-Host "$Config not found. Run .\generate.ps1 first." -ForegroundColor Red
    exit 1
}

# MediaMTX launches ffmpeg by bare name, so bin\ must be resolvable.
$env:PATH = (Join-Path $Root 'bin') + ';' + $env:PATH

Write-Host 'Starting MediaMTX. Ctrl+C to stop.' -ForegroundColor Cyan
Write-Host ''
Write-Host 'Check a camera from a browser on THIS machine:' -ForegroundColor Cyan
Write-Host '    http://localhost:8889/cam0'
Write-Host 'If the picture is there, capture and encoding work and any problem'
Write-Host 'on the WSL side is networking, not the camera.'
Write-Host ''

& $mediamtx $configPath
