#Requires -Version 5.1
<#
.SYNOPSIS
  Write config.yaml from the cameras this machine can actually see.

.DESCRIPTION
  Enumerates DirectShow video devices and writes a config.yaml using their
  exact names, so nobody retypes one. Transcription is where this goes wrong:
  a mistyped name produces a camera that never starts, and an apostrophe or
  odd character copied by hand is worse still.

  Existing config.yaml is never overwritten without -Force.
#>
[CmdletBinding()]
param(
    [string] $Output = 'config.yaml',
    [int]    $Width  = 1920,
    [int]    $Height = 1080,
    [int]    $Fps    = 30,
    # Use libx264 instead of h264_nvenc (no NVIDIA GPU, or NVENC unavailable).
    [switch] $Software,
    # Pick cameras interactively instead of taking all of them.
    [switch] $Choose,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'lib\common.ps1')

$outPath = if ([System.IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path $Root $Output }
if ((Test-Path $outPath) -and -not $Force) {
    Write-Host "$Output already exists. Use -Force to overwrite it." -ForegroundColor Yellow
    exit 1
}

$ffmpeg = Resolve-Ffmpeg -Root $Root
$cameras = @(Get-DshowVideoDevices -Ffmpeg $ffmpeg)

if ($cameras.Count -eq 0) {
    Write-Host 'No video capture devices found.' -ForegroundColor Red
    Write-Host 'Check the camera is plugged in and not held by another application.'
    exit 1
}

Write-Host "Found $($cameras.Count) camera(s):" -ForegroundColor Cyan
for ($i = 0; $i -lt $cameras.Count; $i++) {
    Write-Host ("  [{0}] {1}" -f $i, $cameras[$i]) -ForegroundColor Green
}
Write-Host ''

$selected = $cameras
if ($Choose -and $cameras.Count -gt 1) {
    $answer = Read-Host 'Which? (comma-separated indices, or blank for all)'
    if ($answer.Trim()) {
        $picked = @()
        foreach ($tokenRaw in $answer.Split(',')) {
            $token = $tokenRaw.Trim()
            $index = 0
            if ([int]::TryParse($token, [ref] $index) -and $index -ge 0 -and $index -lt $cameras.Count) {
                $picked += $cameras[$index]
            } else {
                Write-Host "Ignoring '$token' -- not one of the listed indices." -ForegroundColor Yellow
            }
        }
        if ($picked.Count -gt 0) { $selected = $picked }
    }
}

$codec = if ($Software) { 'libx264' } else { 'h264_nvenc' }
$preset = if ($Software) { 'ultrafast' } else { 'p1' }
$tune = if ($Software) { 'zerolatency' } else { 'ull' }

$lines = @('server:', '  rtsp_port: 8554', '', 'cameras:')
for ($i = 0; $i -lt $selected.Count; $i++) {
    # YAML double-quoted scalars escape backslash and double quote. Device
    # names can contain both -- the "alternative name" forms are full of
    # backslashes.
    $escaped = $selected[$i].Replace('\', '\\').Replace('"', '\"')
    $lines += "  - id: cam$i"
    $lines += "    source: {type: dshow, device: `"$escaped`"}"
    $lines += "    video:  {width: $Width, height: $Height, fps: $Fps}"
    $lines += "    encode: {codec: $codec, preset: $preset, tune: $tune, bitrate: 8M, gop: 30}"
}
$lines += @('', 'client:', '  watchdog_timeout_s: 2.0', '  backoff: {initial_s: 0.2, factor: 2.0, max_s: 5.0}')

Set-Content -LiteralPath $outPath -Value $lines -Encoding UTF8

Write-Host "Wrote $outPath" -ForegroundColor Green
for ($i = 0; $i -lt $selected.Count; $i++) {
    Write-Host ("  cam{0} -> {1}" -f $i, $selected[$i])
}
Write-Host ''
Write-Host 'Next:  .\run.ps1   (or .\generate.ps1 then .\start.ps1)' -ForegroundColor Cyan
