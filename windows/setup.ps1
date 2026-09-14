#Requires -Version 5.1
<#
.SYNOPSIS
  One-time setup for the anycam-to-rtsp Windows capture host.

.DESCRIPTION
  Creates a local Python venv, installs the bundled anycam package, and
  fetches ffmpeg and MediaMTX into .\bin. Nothing is installed system-wide
  and no administrator rights are needed. Re-running is safe.

  Only the capture half runs here. The consumer (anycam watch) runs in WSL
  or on another Linux host and is not installed by this script.
#>
[CmdletBinding()]
param(
    [string] $MediaMtxVersion = 'v1.9.3',
    [switch] $SkipBinaries
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $Root 'bin'
$VenvDir = Join-Path $Root '.venv'

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string] $Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string] $Message) Write-Host "    $Message" -ForegroundColor Yellow }

# --- Python ---------------------------------------------------------------
Write-Step 'Checking for Python 3.12 or newer'

$python = $null
foreach ($candidate in @('py -3.12', 'py -3', 'python')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $args = @($parts[1..($parts.Length - 1)]) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
        $version = & $exe @args 2>$null
    } catch { continue }
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
    $parsed = [version]("$version".Trim())
    if ($parsed -ge [version]'3.12') {
        $python = $candidate
        Write-Ok "Using $candidate (Python $version)"
        break
    }
}

if (-not $python) {
    Write-Host ''
    Write-Host 'Python 3.12 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it from https://www.python.org/downloads/windows/ and'
    Write-Host 'tick "Add python.exe to PATH" during installation, then re-run this script.'
    exit 1
}

# --- venv -----------------------------------------------------------------
Write-Step 'Creating the local virtual environment'
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
    $parts = $python.Split(' ')
    & $parts[0] @($parts[1..($parts.Length - 1)]) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
    Write-Ok "Created $VenvDir"
} else {
    Write-Ok 'Already present, reusing it'
}

$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

# --- package --------------------------------------------------------------
# PyYAML is the ONLY runtime dependency the capture half needs. The package
# declares PyAV and NumPy for the WSL consumer, so it is installed with
# --no-deps to keep large, irrelevant wheels off this machine. Verified: the
# `serve-config` and `devices` paths import neither (both are lazy imports
# inside the consumer code path).
Write-Step 'Installing the anycam package (capture half only)'
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet 'PyYAML>=6.0'
if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyYAML.' }
& $VenvPython -m pip install --quiet --no-deps -e $Root
if ($LASTEXITCODE -ne 0) { throw 'Failed to install the anycam package.' }
Write-Ok 'Installed (PyYAML only; PyAV and NumPy deliberately skipped)'

if ($SkipBinaries) {
    Write-Warn 'Skipping binary download as requested (-SkipBinaries).'
    Write-Host ''
    Write-Host 'Setup complete.' -ForegroundColor Green
    exit 0
}

# --- binaries -------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("anycam-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

try {
    # ffmpeg: gyan.dev release-essentials includes h264_nvenc and the dshow
    # input device, which is everything the capture command needs.
    if (Test-Path (Join-Path $BinDir 'ffmpeg.exe')) {
        Write-Step 'ffmpeg already present, skipping download'
    } else {
        Write-Step 'Downloading ffmpeg (this is the large one, ~80-100 MB)'
        $ffmpegZip = Join-Path $tempDir 'ffmpeg.zip'
        Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' `
                          -OutFile $ffmpegZip -UseBasicParsing
        Expand-Archive -Path $ffmpegZip -DestinationPath $tempDir -Force
        $found = Get-ChildItem -Path $tempDir -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
        if (-not $found) { throw 'ffmpeg.exe was not found inside the downloaded archive.' }
        Copy-Item $found.FullName -Destination $BinDir -Force
        $probe = Get-ChildItem -Path $tempDir -Recurse -Filter 'ffprobe.exe' | Select-Object -First 1
        if ($probe) { Copy-Item $probe.FullName -Destination $BinDir -Force }
        Write-Ok "ffmpeg -> $BinDir"
    }

    if (Test-Path (Join-Path $BinDir 'mediamtx.exe')) {
        Write-Step 'MediaMTX already present, skipping download'
    } else {
        Write-Step "Downloading MediaMTX $MediaMtxVersion"
        # The config schema this project generates was verified against
        # v1.9.3. MediaMTX aborts on unknown configuration keys, so a newer
        # release may reject the generated file (`protocols` was renamed to
        # `rtspTransports` in a later version). Pin deliberately.
        $mtxZip = Join-Path $tempDir 'mediamtx.zip'
        $mtxUrl = "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion/mediamtx_${MediaMtxVersion}_windows_amd64.zip"
        Invoke-WebRequest -Uri $mtxUrl -OutFile $mtxZip -UseBasicParsing
        Expand-Archive -Path $mtxZip -DestinationPath $tempDir -Force
        $found = Get-ChildItem -Path $tempDir -Recurse -Filter 'mediamtx.exe' | Select-Object -First 1
        if (-not $found) { throw 'mediamtx.exe was not found inside the downloaded archive.' }
        Copy-Item $found.FullName -Destination $BinDir -Force
        Write-Ok "MediaMTX -> $BinDir"
    }
} finally {
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}

# --- codec check ----------------------------------------------------------
Write-Step 'Checking which hardware codecs this ffmpeg build has'
$ffmpeg = Join-Path $BinDir 'ffmpeg.exe'
$encoders = & $ffmpeg -hide_banner -encoders 2>&1 | Out-String
$decoders = & $ffmpeg -hide_banner -decoders 2>&1 | Out-String

if ($encoders -match 'h264_nvenc') {
    Write-Ok 'h264_nvenc present (NVIDIA hardware encoding available)'
} else {
    Write-Warn 'h264_nvenc NOT found. Set encode.codec to libx264 in config.yaml,'
    Write-Warn 'with preset ultrafast and tune zerolatency.'
}

if ($decoders -match 'mjpeg_cuvid') {
    Write-Ok 'mjpeg_cuvid present (MJPEG decodes on the GPU, no host copy)'
} else {
    Write-Warn 'mjpeg_cuvid NOT found. Run generate.ps1 -NoHwMjpeg to fall back'
    Write-Warn 'to CPU MJPEG decoding.'
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'Next:'
Write-Host '  1. .\devices.ps1              list your cameras, copy the exact names'
Write-Host '  2. notepad config.yaml        put those names in'
Write-Host '  3. .\generate.ps1             build mediamtx.yml from config.yaml'
Write-Host '  4. .\start.ps1                run the server'
Write-Host ''
Write-Host 'Then from WSL or your Linux host:  anycam watch -c config.yaml'
