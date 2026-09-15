#Requires -Version 5.1
<#
.SYNOPSIS
  One-time setup for the anycam2rtsp Windows capture host.

.DESCRIPTION
  Creates a local Python venv, installs the bundled anycam2rtsp package, and
  fetches ffmpeg and MediaMTX into .\bin. Nothing is installed system-wide
  and no administrator rights are needed. Re-running is safe.

  Only the capture half runs here. The consumer (anycam2rtsp watch) runs in WSL
  or on another Linux host and is not installed by this script.
#>
[CmdletBinding()]
param(
    [string] $MediaMtxVersion = 'v1.9.3',
    [switch] $SkipBinaries,
    # Override Python autodetection, e.g.
    #   .\setup.ps1 -PythonExe 'C:\Python313\python.exe'
    [string] $PythonExe
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
# Detection notes, learned the hard way:
#   * $args cannot be assigned in a [CmdletBinding()] script -- it is not set,
#     and referencing it throws.
#   * $parts[1..($parts.Length-1)] on a one-element array indexes out of
#     bounds, which Set-StrictMode turns into a terminating error.
#   * Double quotes inside a `python -c` argument are mangled by PowerShell's
#     native-argument passing, so `--version` is used instead: nothing to quote.
Write-Step 'Checking for Python 3.12 or newer'

function Get-PythonCandidate {
    param([string] $Exe, [string[]] $Prefix = @())

    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    $cmdArgs = @($Prefix) + @('--version')
    try {
        $out = (& $Exe @cmdArgs 2>&1 | Out-String)
    } catch {
        return $null
    }
    if ($out -match 'Python\s+(\d+)\.(\d+)') {
        return [pscustomobject]@{
            Exe     = $Exe
            Prefix  = $Prefix
            Label   = ((@($Exe) + $Prefix) -join ' ')
            Version = [version]("{0}.{1}" -f $Matches[1], $Matches[2])
            Raw     = $out.Trim()
        }
    }
    return $null
}

$minVersion = [version]'3.12'
$python = $null
$tried = @()

if ($PythonExe) {
    $python = Get-PythonCandidate -Exe $PythonExe
    if (-not $python) {
        Write-Host "The -PythonExe you gave could not be run: $PythonExe" -ForegroundColor Red
        exit 1
    }
    if ($python.Version -lt $minVersion) {
        Write-Host "$PythonExe is Python $($python.Version); 3.12 or newer is required." -ForegroundColor Red
        exit 1
    }
} else {
    foreach ($candidate in @(
        @{ Exe = 'py';      Prefix = @('-3') },
        @{ Exe = 'py';      Prefix = @()     },
        @{ Exe = 'python';  Prefix = @()     },
        @{ Exe = 'python3'; Prefix = @()     }
    )) {
        $found = Get-PythonCandidate -Exe $candidate.Exe -Prefix $candidate.Prefix
        $label = ((@($candidate.Exe) + $candidate.Prefix) -join ' ')
        if (-not $found) {
            $tried += "  $label -> not found, or did not report a version"
            continue
        }
        $tried += "  $label -> $($found.Raw)"
        if ($found.Version -ge $minVersion) { $python = $found; break }
    }
}

if (-not $python) {
    Write-Host ''
    Write-Host 'No Python 3.12 or newer was found.' -ForegroundColor Red
    Write-Host ''
    if ($tried.Count -gt 0) {
        Write-Host 'What was tried:'
        $tried | ForEach-Object { Write-Host $_ }
        Write-Host ''
    }
    Write-Host 'Install it from https://www.python.org/downloads/windows/ and tick'
    Write-Host '"Add python.exe to PATH", then re-run this script.'
    Write-Host ''
    Write-Host 'If Python is installed somewhere this did not look, point at it directly:'
    Write-Host "    .\setup.ps1 -PythonExe 'C:\Path\To\python.exe'"
    exit 1
}

Write-Ok "Using $($python.Label) ($($python.Raw))"

# --- venv -----------------------------------------------------------------
Write-Step 'Creating the local virtual environment'
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
    $venvArgs = @($python.Prefix) + @('-m', 'venv', $VenvDir)
    & $python.Exe @venvArgs
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
Write-Step 'Installing the anycam2rtsp package (capture half only)'
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet 'PyYAML>=6.0'
if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyYAML.' }
& $VenvPython -m pip install --quiet --no-deps -e $Root
if ($LASTEXITCODE -ne 0) { throw 'Failed to install the anycam2rtsp package.' }
Write-Ok 'Installed (PyYAML only; PyAV and NumPy deliberately skipped)'

if ($SkipBinaries) {
    Write-Warn 'Skipping binary download as requested (-SkipBinaries).'
    Write-Host ''
    Write-Host 'Setup complete.' -ForegroundColor Green
    exit 0
}

# --- binaries -------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("anycam2rtsp-" + [guid]::NewGuid().ToString('N'))
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

# Capture via an OS-level redirect rather than `2>&1`: merged stderr becomes
# ErrorRecords, which terminate under $ErrorActionPreference='Stop'.
function Invoke-FfmpegQuery {
    param([string] $Flag)
    $o = [System.IO.Path]::GetTempFileName()
    $e = [System.IO.Path]::GetTempFileName()
    try {
        Start-Process -FilePath $ffmpeg -ArgumentList '-hide_banner', $Flag `
                      -NoNewWindow -Wait `
                      -RedirectStandardOutput $o -RedirectStandardError $e | Out-Null
        return ((Get-Content -LiteralPath $o -ErrorAction SilentlyContinue) +
                (Get-Content -LiteralPath $e -ErrorAction SilentlyContinue)) -join "`n"
    } finally {
        Remove-Item -LiteralPath $o, $e -Force -ErrorAction SilentlyContinue
    }
}

$encoders = Invoke-FfmpegQuery -Flag '-encoders'
$decoders = Invoke-FfmpegQuery -Flag '-decoders'

if ($encoders -match 'h264_nvenc') {
    Write-Ok 'h264_nvenc present (NVIDIA hardware encoding available)'
} else {
    Write-Warn 'h264_nvenc NOT found. Set encode.codec to libx264 in config.yaml,'
    Write-Warn 'with preset ultrafast and tune zerolatency.'
}

if ($decoders -match 'mjpeg_cuvid') {
    Write-Ok 'mjpeg_cuvid is listed by this build'
    Write-Warn 'Listed is not the same as working. On some systems it decodes'
    Write-Warn 'files fine but fails against a live DirectShow camera with'
    Write-Warn 'CUDA_ERROR_NO_DEVICE, even with a working NVIDIA GPU and a'
    Write-Warn 'working h264_nvenc encoder. If start.ps1 shows CUDA errors,'
    Write-Warn 'regenerate with:  .\generate.ps1 -NoHwMjpeg'
} else {
    Write-Warn 'mjpeg_cuvid NOT listed. Run generate.ps1 -NoHwMjpeg to use CPU'
    Write-Warn 'MJPEG decoding.'
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
Write-Host 'Then from WSL or your Linux host:  anycam2rtsp watch -c config.yaml'
