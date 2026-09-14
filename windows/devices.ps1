#Requires -Version 5.1
<#
.SYNOPSIS
  List the DirectShow capture devices this machine can see.

.DESCRIPTION
  Copy the device names EXACTLY as printed, including punctuation and case,
  into config.yaml. A name that does not match is the most common reason a
  camera never starts.

  ffmpeg prints this list to STDERR, not stdout, and may exit non-zero -- both
  are by design for -list_devices. Neither is a failure and this script treats
  neither as one.

  The capture uses Start-Process with an OS-level stderr redirect. Merging
  with `2>&1` instead would turn every stderr line into a PowerShell
  ErrorRecord, which under $ErrorActionPreference='Stop' becomes a terminating
  NativeCommandError -- so the device list and the "error" would be the same
  bytes, and the script would fail while holding exactly the data it wanted.
#>
[CmdletBinding()]
param(
    # Show every raw line rather than just the parsed device names.
    [switch] $Raw
)

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

$errFile = [System.IO.Path]::GetTempFileName()
$outFile = [System.IO.Path]::GetTempFileName()
try {
    Start-Process -FilePath $ffmpeg `
                  -ArgumentList '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy' `
                  -NoNewWindow -Wait `
                  -RedirectStandardError $errFile `
                  -RedirectStandardOutput $outFile | Out-Null
    $lines = @(Get-Content -LiteralPath $errFile -ErrorAction SilentlyContinue)
} finally {
    Remove-Item -LiteralPath $errFile, $outFile -Force -ErrorAction SilentlyContinue
}

if ($Raw) {
    Write-Host 'Raw ffmpeg output:' -ForegroundColor DarkGray
    $lines | ForEach-Object { Write-Host ('  | ' + $_) -ForegroundColor DarkGray }
    Write-Host ''
}

Write-Host 'DirectShow capture devices:' -ForegroundColor Cyan
Write-Host ''

$videoCount = 0
foreach ($line in $lines) {
    if ($line -match '"(.+?)"\s*\(video\)') {
        $videoCount++
        Write-Host ('  video : ' + $Matches[1]) -ForegroundColor Green
    } elseif ($line -match '"(.+?)"\s*\(audio\)') {
        Write-Host ('  audio : ' + $Matches[1]) -ForegroundColor DarkGray
    } elseif ($line -match 'Alternative name\s+"(.+?)"') {
        Write-Host ('          alt: ' + $Matches[1]) -ForegroundColor DarkGray
    }
}

Write-Host ''

if ($videoCount -eq 0) {
    Write-Host 'No video capture devices were found.' -ForegroundColor Yellow
    Write-Host 'Check that the camera is plugged in and not in use by another'
    Write-Host 'application, then re-run. Use -Raw to see ffmpeg''s full output.'
    exit 1
}

Write-Host 'Put a video name into config.yaml as:' -ForegroundColor Cyan
Write-Host '    source: {type: dshow, device: "Your Camera Name"}'
Write-Host ''
Write-Host 'If two cameras report the SAME name, use the "alt:" form above --' -ForegroundColor Yellow
Write-Host 'the plain name cannot tell them apart.' -ForegroundColor Yellow
