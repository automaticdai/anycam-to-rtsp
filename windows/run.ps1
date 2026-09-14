#Requires -Version 5.1
<#
.SYNOPSIS
  Set up, configure, and start streaming. One command.

.DESCRIPTION
  Does whatever still needs doing, in order, and skips what is already done:

    1. setup      -- venv, package, ffmpeg, MediaMTX (only if missing)
    2. configure  -- write config.yaml from the cameras actually present
    3. probe      -- open each camera and find out which MJPEG decoder works
    4. generate   -- build mediamtx.yml
    5. start      -- run the server

  The probe is the step worth having. `ffmpeg -decoders` only proves a codec
  was compiled in, and decoding a FILE can succeed on a machine where the same
  decoder fails against a live camera with CUDA_ERROR_NO_DEVICE. Opening the
  camera is the only honest test, so this opens it -- and picks the decoder
  for you rather than making you read a wall of CUDA errors and guess.

.PARAMETER Reconfigure
  Rewrite config.yaml from the currently attached cameras.

.PARAMETER NoProbe
  Skip the decoder probe and trust config.yaml as written.
#>
[CmdletBinding()]
param(
    [switch] $Reconfigure,
    [switch] $NoProbe,
    [switch] $Software
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'lib.ps1')

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$ConfigPath = Join-Path $Root 'config.yaml'

function Write-Step { param([string] $M) Write-Host "==> $M" -ForegroundColor Cyan }

# --- 1. setup -------------------------------------------------------------
if (-not (Test-Path $VenvPython) -or -not (Test-Path (Join-Path $Root 'bin\ffmpeg.exe'))) {
    Write-Step 'First run: setting up'
    & (Join-Path $Root 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host ''
}

# --- 2. configure ---------------------------------------------------------
if ($Reconfigure -or -not (Test-Path $ConfigPath)) {
    Write-Step 'Detecting cameras and writing config.yaml'
    $configArgs = @('-Force')
    if ($Software) { $configArgs += '-Software' }
    & (Join-Path $Root 'configure.ps1') @configArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host ''
} else {
    Write-Step 'Using the existing config.yaml (-Reconfigure to rebuild it)'
}

# --- 3. probe -------------------------------------------------------------
$useHardwareDecode = $true

if ($NoProbe) {
    Write-Step 'Skipping the decoder probe (-NoProbe)'
} else {
    Write-Step 'Checking which MJPEG decoder your cameras accept'
    $ffmpeg = Resolve-Ffmpeg -Root $Root
    $listing = & $VenvPython (Join-Path $Root 'list_cameras.py') $ConfigPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $probed = $false
    foreach ($line in @($listing)) {
        $fields = $line -split "`t"
        if ($fields.Count -lt 3) { continue }
        $id = $fields[0]; $type = $fields[1]; $device = $fields[2]
        if ($type -ne 'dshow' -or -not $device) { continue }

        $verdict = Test-CameraDecoder -Ffmpeg $ffmpeg -Device $device -Hardware
        $probed = $true
        switch ($verdict) {
            'works' {
                Write-Host "    $id : GPU decode works" -ForegroundColor Green
            }
            'cuda-failed' {
                Write-Host "    $id : GPU decode unavailable, using the CPU" -ForegroundColor Yellow
                Write-Host "           (mjpeg_cuvid cannot open this camera; encoding still uses the GPU)" -ForegroundColor DarkGray
                $useHardwareDecode = $false
            }
            'busy' {
                Write-Host "    $id : camera is in use by another application" -ForegroundColor Yellow
                Write-Host "           Close whatever is holding it -- another MediaMTX, Teams, the Camera app." -ForegroundColor DarkGray
                Write-Host "           Falling back to CPU decode, which always works." -ForegroundColor DarkGray
                $useHardwareDecode = $false
            }
            default {
                Write-Host "    $id : could not open the camera; using CPU decode" -ForegroundColor Yellow
                $useHardwareDecode = $false
            }
        }
    }
    if (-not $probed) {
        Write-Host '    no dshow cameras in config.yaml, nothing to probe' -ForegroundColor DarkGray
    }
    Write-Host ''
}

# --- 4. generate ----------------------------------------------------------
Write-Step 'Generating mediamtx.yml'
$generateArgs = @()
if (-not $useHardwareDecode) { $generateArgs += '-NoHwMjpeg' }
& (Join-Path $Root 'generate.ps1') @generateArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ''

# --- 5. start -------------------------------------------------------------
Write-Step 'Starting MediaMTX'
Write-Host ''
& (Join-Path $Root 'start.ps1')
