#Requires -Version 5.1
<#
.SYNOPSIS
  Regression tests for windows\bundle\lib\common.ps1.

.DESCRIPTION
  Run:  powershell -NoProfile -File windows\tests\common.tests.ps1

  These cover the process-lifetime rules, which is where a defect here does
  real damage: an ffmpeg that outlives the script keeps the camera open, and
  every later run then fails with "Could not run graph" and blames whatever
  application happens to be running.

  ping.exe stands in for ffmpeg. The bug these guard against is in how the
  wait is written, not in ffmpeg, and ping is on every Windows machine -- so
  the tests need no camera, no downloaded binaries and no GPU.

  Not shipped in the bundle; build-zip.sh only packages windows\bundle\.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CommonPath = Join-Path $Root 'bundle\lib\common.ps1'
. $CommonPath

$script:Failures = 0
$script:Ran = 0

function Test-Case {
    param([Parameter(Mandatory)][string] $Name, [Parameter(Mandatory)][scriptblock] $Body)

    $script:Ran++
    try {
        & $Body
        Write-Host "  PASS  $Name" -ForegroundColor Green
    } catch {
        $script:Failures++
        Write-Host "  FAIL  $Name" -ForegroundColor Red
        Write-Host "        $($_.Exception.Message)" -ForegroundColor DarkGray
    }
}

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}

function Get-PingPids { @(Get-Process -Name 'ping' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) }

$ping = Join-Path $env:SystemRoot 'System32\ping.exe'
# 127.0.0.1 keeps every case off the network: a DNS or routing stall would
# show up as a timeout and make these tests lie.
$slow = @('-n', '60', '127.0.0.1')
$fast = @('-n', '1', '127.0.0.1')

Write-Host ''
Write-Host 'common.ps1' -ForegroundColor Cyan

Test-Case 'a command that finishes reports its exit code and output' {
    $r = Invoke-Ffmpeg -Ffmpeg $ping -Arguments $fast -TimeoutSeconds 30
    Assert-True ($r.ExitCode -eq 0)      "expected exit code 0, got $($r.ExitCode)"
    Assert-True (-not $r.TimedOut)       'expected TimedOut to be false'
    Assert-True ($r.StdOut -match '127') "expected stdout to be captured, got: $($r.StdOut)"
}

Test-Case 'a failing command reports a non-zero exit code, not a timeout' {
    $r = Invoke-Ffmpeg -Ffmpeg $ping -Arguments @('-n', '1', '256.256.256.256') -TimeoutSeconds 30
    Assert-True ($r.ExitCode -ne 0) 'expected a non-zero exit code'
    Assert-True (-not $r.TimedOut)  'expected TimedOut to be false'
}

Test-Case 'a hung command times out instead of waiting forever' {
    $started = Get-Date
    $r = Invoke-Ffmpeg -Ffmpeg $ping -Arguments $slow -TimeoutSeconds 3
    $elapsed = ((Get-Date) - $started).TotalSeconds

    Assert-True $r.TimedOut              'expected TimedOut to be true'
    Assert-True ($null -eq $r.ExitCode)  'expected ExitCode to be null on timeout'
    Assert-True ($elapsed -lt 30)        "expected to return promptly, took ${elapsed}s"
}

Test-Case 'a timed-out command is killed, not left running' {
    $before = Get-PingPids
    $null = Invoke-Ffmpeg -Ffmpeg $ping -Arguments $slow -TimeoutSeconds 3
    $leaked = @(Get-PingPids | Where-Object { $before -notcontains $_ })
    Assert-True ($leaked.Count -eq 0) "leaked $($leaked.Count) process(es): $($leaked -join ', ')"
}

Test-Case 'a wait interrupted like Ctrl+C still kills the child' {
    # This is the reported defect itself. The old cleanup handler released the
    # temp files but not the child, so interrupting a probe left ffmpeg on the
    # camera. PowerShell.Stop() raises PipelineStoppedException in the running
    # pipeline, which is what Ctrl+C does, so the finally block unwinds by the
    # same route it would for a real interrupt.
    $before = Get-PingPids

    $shell = [PowerShell]::Create()
    try {
        $null = $shell.AddScript({
            param($CommonPath, $Exe, $ExeArgs)
            . $CommonPath
            Invoke-Ffmpeg -Ffmpeg $Exe -Arguments $ExeArgs -TimeoutSeconds 0
        }).AddArgument($CommonPath).AddArgument($ping).AddArgument($slow)

        $null = $shell.BeginInvoke()
        # Long enough for Start-Process to have returned a live child.
        Start-Sleep -Seconds 3
        $shell.Stop()
    } finally {
        $shell.Dispose()
    }

    # The kill and its WaitForExit happen as the pipeline unwinds.
    Start-Sleep -Seconds 2
    $leaked = @(Get-PingPids | Where-Object { $before -notcontains $_ })
    foreach ($leakedPid in $leaked) { Stop-Process -Id $leakedPid -Force -ErrorAction SilentlyContinue }
    Assert-True ($leaked.Count -eq 0) "interrupted wait leaked $($leaked.Count) process(es)"
}

Test-Case 'Stop-ChildProcess is safe on a null and on an already-dead process' {
    Assert-True (Stop-ChildProcess -Process $null) 'expected $true for a null process'
    # Redirected so ping's output does not land in the middle of the results.
    $sink = [System.IO.Path]::GetTempFileName()
    $p = Start-Process -FilePath $ping -ArgumentList $fast -NoNewWindow -PassThru `
                       -RedirectStandardOutput $sink
    $null = $p.Handle
    $p.WaitForExit()
    Remove-Item -LiteralPath $sink -Force -ErrorAction SilentlyContinue
    Assert-True (Stop-ChildProcess -Process $p) 'expected $true for an already-exited process'
}

Test-Case 'Get-CameraHolder returns a path or nothing, and never throws' {
    $holder = Get-CameraHolder
    Assert-True (($null -eq $holder) -or ($holder -is [string] -and $holder.Length -gt 0)) `
        "expected a non-empty string or null, got: $holder"
}

Write-Host ''
if ($script:Failures -gt 0) {
    Write-Host "$script:Failures of $script:Ran failed" -ForegroundColor Red
    exit 1
}
Write-Host "$script:Ran passed" -ForegroundColor Green
exit 0
