# Shared helpers for the anycam Windows scripts. Dot-sourced, not run directly.
#
# Two hard-won rules live here:
#
#  1. Capture a native command's stderr with an OS-level redirect, never
#     `2>&1`. Merged stderr becomes PowerShell ErrorRecords, which terminate
#     under $ErrorActionPreference='Stop' -- and ffmpeg writes its device list
#     to stderr, so the data and the "error" are the same bytes.
#
#  2. Start-Process joins -ArgumentList with spaces and does NOT quote the
#     elements. Any argument containing a space must carry its own quotes, or
#     a device name like "HD Pro Webcam C920" arrives as "HD".

function Resolve-Ffmpeg {
    param([Parameter(Mandatory)][string] $Root)

    $local = Join-Path $Root 'bin\ffmpeg.exe'
    if (Test-Path $local) { return $local }
    $found = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw 'ffmpeg not found. Run .\setup.ps1 first.'
}

function Invoke-Ffmpeg {
    <#
    .SYNOPSIS
      Run ffmpeg, returning exit code plus captured stdout/stderr.
    #>
    param(
        [Parameter(Mandatory)][string]   $Ffmpeg,
        [Parameter(Mandatory)][string[]] $Arguments
    )

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $Ffmpeg -ArgumentList $Arguments `
                              -NoNewWindow -Wait -PassThru `
                              -RedirectStandardOutput $outFile `
                              -RedirectStandardError $errFile
        return [pscustomobject]@{
            ExitCode = $proc.ExitCode
            StdOut   = (Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue)
            StdErr   = (Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue)
        }
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-DshowVideoDevices {
    param([Parameter(Mandatory)][string] $Ffmpeg)

    $result = Invoke-Ffmpeg -Ffmpeg $Ffmpeg `
        -Arguments @('-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy')

    $names = @()
    foreach ($line in ($result.StdErr -split "`r?`n")) {
        if ($line -match '"(.+?)"\s*\(video\)') { $names += $Matches[1] }
    }
    return $names
}

function Test-CameraDecoder {
    <#
    .SYNOPSIS
      Open a camera briefly and report whether a decoder actually works on it.

    .DESCRIPTION
      Returns 'works', 'cuda-failed', 'busy', or 'failed'.

      This has to open the real camera. Neither cheaper check is trustworthy:
      `ffmpeg -decoders` only proves the codec was compiled in, and decoding a
      FILE can succeed on a machine where the same decoder fails against a
      live capture device with CUDA_ERROR_NO_DEVICE.
    #>
    param(
        [Parameter(Mandatory)][string] $Ffmpeg,
        [Parameter(Mandatory)][string] $Device,
        [switch] $Hardware,
        [int] $Width = 1920,
        [int] $Height = 1080,
        [int] $Fps = 30
    )

    $decoder = if ($Hardware) {
        @('-c:v', 'mjpeg_cuvid', '-hwaccel_output_format', 'cuda')
    } else {
        @('-vcodec', 'mjpeg')
    }

    $arguments = @('-hide_banner', '-loglevel', 'error',
                   '-f', 'dshow', '-rtbufsize', '64M',
                   '-framerate', "$Fps", '-video_size', "${Width}x${Height}") +
                 $decoder +
                 @('-i', ('"video=' + $Device + '"'), '-frames:v', '5', '-f', 'null', '-')

    $result = Invoke-Ffmpeg -Ffmpeg $Ffmpeg -Arguments $arguments
    $stderr = if ($result.StdErr) { $result.StdErr } else { '' }

    if ($result.ExitCode -eq 0) { return 'works' }
    if ($stderr -match 'CUDA|cuvid')                          { return 'cuda-failed' }
    if ($stderr -match 'already in use|Could not run graph')   { return 'busy' }
    return 'failed'
}
