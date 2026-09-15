#Requires -Version 5.1
<#
.SYNOPSIS
  Generate mediamtx.yml from config.yaml.

.DESCRIPTION
  Regenerate this every time config.yaml changes. Do NOT hand-edit
  mediamtx.yml: the ffmpeg command lines it contains are escaped for
  MediaMTX's own argument parser (go-shellquote, not cmd.exe), and getting
  that wrong silently produces a camera that never starts and logs nothing.
#>
[CmdletBinding()]
param(
    [string] $Config = 'config.yaml',
    [string] $Output = 'mediamtx.yml',
    # Use when this ffmpeg build lacks mjpeg_cuvid; falls back to CPU MJPEG
    # decoding. setup.ps1 tells you whether you need it.
    [switch] $NoHwMjpeg
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host 'Virtual environment not found. Run .\setup.ps1 first.' -ForegroundColor Red
    exit 1
}

$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Root $Config }
$outputPath = if ([System.IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path $Root $Output }

if (-not (Test-Path $configPath)) {
    Write-Host "Config not found: $configPath" -ForegroundColor Red
    Write-Host 'Copy config.example.yaml to config.yaml and edit it.'
    exit 1
}

$cliArgs = @('-m', 'anycam2rtsp.cli', 'serve-config', '-c', $configPath, '-o', $outputPath)
if ($NoHwMjpeg) { $cliArgs += '--no-hw-mjpeg' }

& $VenvPython @cliArgs
if ($LASTEXITCODE -ne 0) {
    # Exit code 2 means a clean configuration error was already printed.
    exit $LASTEXITCODE
}

Write-Host ''
if ($NoHwMjpeg) {
    Write-Host 'MJPEG will be decoded on the CPU (-NoHwMjpeg).' -ForegroundColor Cyan
} else {
    Write-Host 'MJPEG will be decoded on the GPU via mjpeg_cuvid.' -ForegroundColor Cyan
    Write-Host 'If start.ps1 logs CUDA_ERROR_NO_DEVICE or cuvid decode errors,' -ForegroundColor Yellow
    Write-Host 'that decoder cannot open your camera. Re-run:' -ForegroundColor Yellow
    Write-Host '    .\generate.ps1 -NoHwMjpeg' -ForegroundColor Yellow
    Write-Host 'Encoding still uses the GPU either way; only the decode moves.' -ForegroundColor Yellow
}
Write-Host ''
Write-Host 'Now run:  .\start.ps1' -ForegroundColor Cyan
