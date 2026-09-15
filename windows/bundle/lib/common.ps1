# Shared helpers for the anycam2rtsp Windows scripts. Dot-sourced, not run directly.
#
# Three hard-won rules live here:
#
#  1. Capture a native command's stderr with an OS-level redirect, never
#     `2>&1`. Merged stderr becomes PowerShell ErrorRecords, which terminate
#     under $ErrorActionPreference='Stop' -- and ffmpeg writes its device list
#     to stderr, so the data and the "error" are the same bytes.
#
#  2. Start-Process joins -ArgumentList with spaces and does NOT quote the
#     elements. Any argument containing a space must carry its own quotes, or
#     a device name like "HD Pro Webcam C920" arrives as "HD".
#
#  3. A child process is a resource, exactly like the temp files next to it,
#     and must be released on every exit path. Windows does not kill a
#     Start-Process child when its parent dies, so an ffmpeg that outlives the
#     script keeps the camera open and makes every later run fail with
#     "Could not run graph". Nothing here may wait on a child without a
#     deadline, and nothing may return without killing one still running.

function Resolve-Ffmpeg {
    param([Parameter(Mandatory)][string] $Root)

    $local = Join-Path $Root 'bin\ffmpeg.exe'
    if (Test-Path $local) { return $local }
    $found = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw 'ffmpeg not found. Run .\setup.ps1 first.'
}

function Stop-ChildProcess {
    <#
    .SYNOPSIS
      Kill a child process and wait for it to actually go away.

    .DESCRIPTION
      Returns $true if the process is gone afterwards. Never throws: this runs
      from `finally` blocks, where a throw would replace the real error with
      this one.
    #>
    param(
        [System.Diagnostics.Process] $Process,
        [int] $GraceSeconds = 5
    )

    if (-not $Process) { return $true }
    try {
        if ($Process.HasExited) { return $true }
        $Process.Kill()
        return $Process.WaitForExit($GraceSeconds * 1000)
    } catch {
        return $false
    }
}

function Invoke-Ffmpeg {
    <#
    .SYNOPSIS
      Run ffmpeg, returning exit code plus captured stdout/stderr.

    .DESCRIPTION
      Anything that opens a capture device MUST pass -TimeoutSeconds. A wedged
      DirectShow graph never returns, and an unbounded wait then hangs the
      script with a live ffmpeg sitting on the camera -- which is what makes
      every subsequent run report the camera as busy.

      On timeout the child is killed, ExitCode is $null and TimedOut is $true.
      The child is also killed if the wait is abandoned some other way (Ctrl+C
      raises PipelineStoppedException, and a throw under
      $ErrorActionPreference='Stop' unwinds the same route).
    #>
    param(
        [Parameter(Mandatory)][string]   $Ffmpeg,
        [Parameter(Mandatory)][string[]] $Arguments,
        # 0 waits forever. Only safe for calls that cannot touch a device.
        [int] $TimeoutSeconds = 0
    )

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $proc = $null
    $timedOut = $false
    try {
        $proc = Start-Process -FilePath $Ffmpeg -ArgumentList $Arguments `
                              -NoNewWindow -PassThru `
                              -RedirectStandardOutput $outFile `
                              -RedirectStandardError $errFile

        # Touching .Handle caches the process handle. Without it a -PassThru
        # object that was not also given -Wait reports ExitCode as $null after
        # the process ends, because the handle it needs has already closed.
        $null = $proc.Handle

        if ($TimeoutSeconds -gt 0) {
            $timedOut = -not $proc.WaitForExit($TimeoutSeconds * 1000)
            if ($timedOut) { $null = Stop-ChildProcess -Process $proc }
        } else {
            $proc.WaitForExit()
        }

        return [pscustomobject]@{
            ExitCode = if ($timedOut) { $null } else { $proc.ExitCode }
            TimedOut = $timedOut
            StdOut   = (Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue)
            StdErr   = (Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue)
        }
    } finally {
        # The process is the other resource this function acquires, and the
        # one that does damage if it leaks. Release it before the temp files:
        # they stay locked while it holds them open.
        $null = Stop-ChildProcess -Process $proc
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-CameraHolder {
    <#
    .SYNOPSIS
      Return the path of the process Windows records as using the webcam, or
      $null if nothing holds it.

    .DESCRIPTION
      Windows logs camera use in the CapabilityAccessManager consent store,
      where a LastUsedTimeStop of 0 means "still open right now". This is the
      only check that can name the holder: ffmpeg reports "Could not run
      graph" no matter who has the device, including when the holder is one of
      our own leaked probes.

      Best-effort. Returns $null if the key is absent, which is what happens
      for a holder that never went through the consent store.
    #>

    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam'
    )
    foreach ($root in $roots) {
        foreach ($path in @($root, (Join-Path $root 'NonPackaged'))) {
            if (-not (Test-Path $path)) { continue }
            foreach ($key in (Get-ChildItem $path -ErrorAction SilentlyContinue)) {
                $value = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
                if (-not $value) { continue }
                if ($value.PSObject.Properties.Name -notcontains 'LastUsedTimeStop') { continue }
                if ($value.LastUsedTimeStop -ne 0) { continue }
                # Non-packaged apps are keyed by full path with '#' for '\'.
                return $key.PSChildName.Replace('#', '\')
            }
        }
    }
    return $null
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
      Returns 'works', 'cuda-failed', 'busy', 'timeout', or 'failed'.

      This has to open the real camera. Neither cheaper check is trustworthy:
      `ffmpeg -decoders` only proves the codec was compiled in, and decoding a
      FILE can succeed on a machine where the same decoder fails against a
      live capture device with CUDA_ERROR_NO_DEVICE.

      Opening the real camera is also why this needs a deadline. Five frames
      at 30fps is a fifth of a second and a cold device open costs a few
      seconds, so the default leaves an order of magnitude of headroom -- long
      enough that a slow camera is never called wedged, short enough that a
      wedged one does not hang the run.
    #>
    param(
        [Parameter(Mandatory)][string] $Ffmpeg,
        [Parameter(Mandatory)][string] $Device,
        [switch] $Hardware,
        [int] $Width = 1920,
        [int] $Height = 1080,
        [int] $Fps = 30,
        [int] $TimeoutSeconds = 20
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

    $result = Invoke-Ffmpeg -Ffmpeg $Ffmpeg -Arguments $arguments -TimeoutSeconds $TimeoutSeconds
    $stderr = if ($result.StdErr) { $result.StdErr } else { '' }

    if ($result.TimedOut) { return 'timeout' }
    if ($result.ExitCode -eq 0) { return 'works' }
    if ($stderr -match 'CUDA|cuvid')                          { return 'cuda-failed' }
    if ($stderr -match 'already in use|Could not run graph')   { return 'busy' }
    return 'failed'
}
