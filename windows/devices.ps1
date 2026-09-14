#Requires -Version 5.1
<#
.SYNOPSIS
  List the DirectShow capture devices this machine can see.

.DESCRIPTION
  Copy the device names EXACTLY as printed, including punctuation and case,
  into config.yaml. A name that does not match is the most common reason a
  camera never starts.

  ffmpeg exits non-zero here BY DESIGN and prints the list to stderr. That is
  not a failure and this script does not treat it as one.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ffmpeg = Join-Path $Root 'bin\ffmpeg.exe'
if (-not (Test-Path $ffmpeg)) {
    $fallback = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if (-not $fallback) {
        Write-Host 'ffmpeg not found. Run .\setup.ps1 first.' -ForegroundColor Red
        exit 1
    }
    $ffmpeg = $fallback.Source
}

Write-Host 'DirectShow capture devices:' -ForegroundColor Cyan
Write-Host ''

# stderr is where the list goes; 2>&1 merges it so we can print it.
& $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1 |
    ForEach-Object { $_.ToString() } |
    Where-Object { $_ -notmatch 'Immediate exit requested' } |
    ForEach-Object {
        if ($_ -match '"(.+)"\s*\(video\)') {
            Write-Host ('  video : ' + $Matches[1]) -ForegroundColor Green
        } elseif ($_ -match '"(.+)"\s*\(audio\)') {
            Write-Host ('  audio : ' + $Matches[1]) -ForegroundColor DarkGray
        } elseif ($_ -match 'Alternative name\s+"(.+)"') {
            Write-Host ('          alt: ' + $Matches[1]) -ForegroundColor DarkGray
        }
    }

Write-Host ''
Write-Host 'Put a video name into config.yaml as:' -ForegroundColor Cyan
Write-Host '    source: {type: dshow, device: "Your Camera Name"}'
Write-Host ''
Write-Host 'If two cameras report the SAME name, use the "alt:" form instead --' -ForegroundColor Yellow
Write-Host 'the plain name cannot distinguish them.' -ForegroundColor Yellow
