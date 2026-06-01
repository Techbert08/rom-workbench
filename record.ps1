#requires -Version 7.0
<#
.SYNOPSIS
    Records a gameplay session against a WPC ROM in a format that replay.py can drive
    headlessly later. Default mode (VpRecord) launches Visual Pinball + VPinMAME and
    captures the replayable switch-edge stream via the patched VPinMAME64.dll.

.DESCRIPTION
    VpRecord mode (default): launches Visual Pinball on the configured .vpx table. VP
    drives the switch matrix through the VPinMAME COM Controller, which funnels through
    `vp_putSwitch`; the patched VPinMAME64.dll (VPINMAME_SWITCHLOG) logs every switch
    EDGE there with an emulation-clock timestamp into sessions/<utc>/switchlog.jsonl,
    which this script folds into session.jsonl as `kind:"switch"` records. This is the
    stream replay.py injects via PinmameSetSwitch. A legacy MAME .inp is also written
    (VPINMAME_RECORD) but is nonfunctional for VP-driven sessions (it only captures the
    input-port plane, which VP's COM switches bypass) — kept as a forensic artifact only.

    InpOnly mode: spawns `pinmame.exe -record session.inp <rom>` directly. No table view,
    no .vpx required; you play with the keyboard in the standalone PinMAME window. This
    produces a byte-deterministic MAME-layer .inp (replayable via `replay.py
    --playback-inp`), in the same `sessions/<utc>/` wrapper.

    Press Ctrl-C in this terminal (or close the VP / PinMAME window) to stop recording.

.PARAMETER Rom
    VPM ROM name (matches the .zip stem under %VPINMAME_DIR%\roms\). Default: congo_21.

.PARAMETER Table
    Path to a .vpx table file. Default: looked up from -ConfigPath by ROM name.
    Ignored in InpOnly mode.

.PARAMETER ConfigPath
    Path to the project config file. Default: .\config.json (relative to CWD, i.e. the
    project root). Written by pinball-setup\add-rom.ps1.

.PARAMETER Mode
    VpRecord (default), InpOnly, or VpVpm (broken; reference only).

.PARAMETER OutDir
    Output directory. Default: .\sessions\<UTC-yyyyMMddTHHmmssZ>.

.PARAMETER PollHz
    Switch-matrix poll rate in Hz (VpVpm mode only — the broken COM-polling path).
    Default 1000. The default VpRecord mode is edge-triggered (no polling window), so
    this is ignored there and in InpOnly mode.

.PARAMETER MaxSeconds
    Safety stop. Default 600 (10 min). Recording auto-terminates after this many seconds.

.PARAMETER SwitchSet
    Override path to the per-ROM switch-set JSON. Default: schemas\switches\<rom>.json.
#>
[CmdletBinding()]
param(
    [string] $Rom = 'congo_21',
    [string] $Table,
    [ValidateSet('VpRecord', 'VpVpm', 'InpOnly')]
    # VpRecord: launch Visual Pinball with VPINMAME_RECORD set; patched VPinMAME64.dll
    #           records the .inp automatically. Requires setup.ps1 to have deployed the
    #           patched DLL. This is the recommended mode for full-table gameplay recording.
    # InpOnly:  standalone pinmame.exe -record; keyboard input only works when run
    #           interactively (not headless). Produces a MAME-native .inp.
    # VpVpm:    broken (InprocServer32 isolation); kept for reference only.
    [string] $Mode = 'VpRecord',
    [string] $OutDir,
    [int] $PollHz = 1000,
    [int] $MaxSeconds = 600,
    [string] $ConfigPath = '.\config.json',
    [string] $SwitchSet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'lib\Common.ps1')
. (Join-Path $PSScriptRoot 'lib\SessionSchema.ps1')
. (Join-Path $PSScriptRoot 'lib\VpmCom.ps1')

# --- Resolve env / paths -----------------------------------------------------

$vpinball  = Get-RpEnvVar 'VPINBALL_DIR'
$pinmame   = Get-RpEnvVar 'PINMAME_DIR'
$vpinmame  = Get-RpEnvVar 'VPINMAME_DIR'

if (-not $pinmame -or -not (Test-Path $pinmame)) {
    throw "PINMAME_DIR not set (run pinball-setup\setup-pinball.ps1)."
}

if (-not $OutDir) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $OutDir = Join-Path (Get-Location) "sessions\$stamp"
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path $OutDir).Path
Write-Step "Output: $OutDir"

$sessionPath = Join-Path $OutDir 'session.jsonl'
$metaPath    = Join-Path $OutDir 'session.meta.json'

# --- ROM zip sanity check ----------------------------------------------------

$romZip = Join-Path (Join-Path $vpinmame 'roms') "$Rom.zip"
if (-not (Test-Path $romZip)) {
    throw "ROM zip not staged at $romZip. Run pinball-setup\setup-pinball.ps1, or place $Rom.zip in %VPINMAME_DIR%\roms\."
}
$romZipSha256 = Get-FileSha256 -Path $romZip

# ============================================================================
# VpRecord mode — launch Visual Pinball; patched VPinMAME64.dll records .inp
# ============================================================================
if ($Mode -eq 'VpRecord') {
    if (-not $vpinball -or -not (Test-Path $vpinball)) {
        throw "VPINBALL_DIR not set or missing (run pinball-setup\setup-pinball.ps1)."
    }
    if (-not $vpinmame -or -not (Test-Path $vpinmame)) {
        throw "VPINMAME_DIR not set or missing (run pinball-setup\setup-pinball.ps1)."
    }

    $vpxExe = Join-Path $vpinball 'VPinballX64.exe'
    if (-not (Test-Path $vpxExe)) { throw "VPinballX64.exe not found at $vpxExe." }

    # Resolve table from config.json. Check the project-local config first
    # (-ConfigPath, default .\config.json), then fall back to the global one at
    # %LOCALAPPDATA%\record-pinball\config.json where add-rom.ps1 writes by default.
    if (-not $Table) {
        $configCandidates = @($ConfigPath, (Get-RpConfigPath)) |
            Select-Object -Unique |
            Where-Object { Test-Path $_ }
        foreach ($cp in $configCandidates) {
            $cfg = Read-RpConfig -Path $cp
            if ($cfg.PSObject.Properties.Match('tables').Count -gt 0 -and
                $cfg.tables -and
                $cfg.tables.PSObject.Properties.Match($Rom).Count -gt 0) {
                $Table = [string]$cfg.tables.$Rom
                break
            } elseif ($cfg.PSObject.Properties.Match('table_path').Count -gt 0) {
                $Table = [string]$cfg.table_path
                break
            }
        }
    }
    if (-not $Table -or -not (Test-Path $Table)) {
        throw "No table registered for ROM '$Rom'. Run pinball-setup\add-rom.ps1 -RomZip <path> -Table <vpx>, or pass -Table here."
    }
    $Table = (Resolve-Path $Table).Path
    $tableSha256 = Get-FileSha256 -Path $Table

    # VPinMAME writes .inp files into its configured inp\ subdirectory.
    # We use a fixed stem so we can find it reliably after VPX exits.
    $inpStem   = 'record-pinball-current'
    $inpSrc    = Join-Path $vpinmame "inp\$inpStem.inp"
    $inpPath   = Join-Path $OutDir 'session.inp'

    # Switch-stream log. The patched VPinMAME64.dll writes every externally
    # driven switch change (the COM Controller's put_Switch/put_Switches funnel
    # through vp_putSwitch) here as JSONL "switch" records stamped with the
    # emulation clock, when VPINMAME_SWITCHLOG names a file. This is the
    # *replayable* input stream — unlike the MAME .inp, which only captures the
    # input-port plane and stays empty under VP's COM-driven switch matrix.
    $switchLog = Join-Path $OutDir 'switchlog.jsonl'

    # Remove any stale recording from a previous run
    Remove-Item $inpSrc -ErrorAction SilentlyContinue
    Remove-Item $switchLog -ErrorAction SilentlyContinue

    $writer = New-RpSessionWriter -Path $sessionPath
    $writer.WriteMeta(@{
        rom               = $Rom
        rom_zip_sha256    = $romZipSha256
        table_path        = $Table
        table_sha256      = $tableSha256
        vpm_version       = $null
        pinmame_version   = $null
        mode              = 'VpRecord'
        poll_hz           = 0
        start_ts          = (Get-Date).ToUniversalTime().ToString('o')
        end_ts            = $null
        sample_loss_count = 0
        host              = $env:COMPUTERNAME
        comment           = "VPinMAME VPINMAME_RECORD recording"
    })
    $writer.Flush()
    $writer.Close()

    Write-Step "Launching Visual Pinball (VpRecord mode)"
    Write-Host "  Table:  $Table" -ForegroundColor DarkGray
    Write-Host "  Record: $inpSrc" -ForegroundColor DarkGray
    Write-Warn2 "Play, then close the Visual Pinball window to stop recording."

    # Set VPINMAME_RECORD (legacy .inp) and VPINMAME_SWITCHLOG (the real,
    # replayable switch stream) in the child environment so the patched DLL
    # picks them up. The .inp is kept for now but is nonfunctional for VP-driven
    # sessions; switchlog.jsonl is what replay actually consumes.
    $env:VPINMAME_RECORD    = $inpStem
    $env:VPINMAME_SWITCHLOG = $switchLog
    try {
        $vpProc = Start-Process -FilePath $vpxExe -ArgumentList @('-play', "`"$Table`"") `
            -WorkingDirectory $vpinball -PassThru
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while (-not $vpProc.HasExited -and $sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
            Start-Sleep -Milliseconds 500
        }
        if (-not $vpProc.HasExited) {
            Write-Warn2 "MaxSeconds reached; closing Visual Pinball."
            try { $vpProc.CloseMainWindow() | Out-Null } catch {}
            Start-Sleep -Seconds 2
            if (-not $vpProc.HasExited) { $vpProc.Kill() }
        }
    } finally {
        $env:VPINMAME_RECORD    = ''
        $env:VPINMAME_SWITCHLOG = ''
    }

    # Collect the .inp (legacy; kept until the switch-log path is fully proven)
    if (Test-Path $inpSrc) {
        Copy-Item $inpSrc $inpPath -Force
        Write-Ok ".inp written: $inpPath ($((Get-Item $inpPath).Length) bytes)"
    } else {
        Write-Warn2 "No .inp produced at $inpSrc — VPinMAME may not have started, or the patched DLL is not deployed."
        Write-Warn2 "Verify setup.ps1 ran successfully and VPINMAME_DIR\VPinMAME64.dll is the patched version."
    }

    # Fold the captured switch stream into session.jsonl as kind:"switch"
    # records (after the meta line). replay.py / replay_host.py inject these via
    # PinmameSetSwitch at their recorded emulation-clock timestamps — the same
    # swMatrix plane VP drove during recording, so gameplay reproduces faithfully.
    if (Test-Path $switchLog) {
        $swLines = @(Get-Content -LiteralPath $switchLog | Where-Object { $_.Trim() })
        if ($swLines.Count -gt 0) {
            Add-Content -LiteralPath $sessionPath -Value $swLines -Encoding utf8
            Write-Ok "Switch stream: $($swLines.Count) events folded into session.jsonl"
        } else {
            Write-Warn2 "switchlog.jsonl is empty — no switches were captured (did you actually play?)."
        }
    } else {
        Write-Warn2 "No switchlog.jsonl produced — the deployed VPinMAME64.dll may predate the switch recorder."
    }

    @{
        rom            = $Rom
        rom_zip_sha256 = $romZipSha256
        mode           = 'VpRecord'
        table_path     = $Table
        table_sha256   = $tableSha256
        inp            = (Split-Path -Leaf $inpPath)
        session_jsonl  = (Split-Path -Leaf $sessionPath)
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $metaPath

    Write-Host ""
    Write-Host "Recording complete (VpRecord)." -ForegroundColor Green
    Write-Host "  Session: $sessionPath"
    Write-Host "  Inputs:  $inpPath"
    return
}

# ============================================================================
# InpOnly mode
# ============================================================================
if ($Mode -eq 'InpOnly') {
    $pinmameExe = $null
    foreach ($n in 'PinMAME.exe','pinmame.exe') {
        $cand = Join-Path $pinmame $n
        if (Test-Path $cand) { $pinmameExe = $cand; break }
    }
    if (-not $pinmameExe) { throw "pinmame.exe not found under $pinmame. Re-run pinball-setup\setup-pinball.ps1." }

    $inpPath = Join-Path $OutDir 'session.inp'
    # ROMs are staged into %VPINMAME_DIR%\roms\ by pinball-setup\add-rom.ps1, but pinmame.exe looks
    # under its own CWD by default. Point it explicitly via -rompath.
    $romPath = Join-Path $vpinmame 'roms'
    if (-not (Test-Path (Join-Path $romPath "$Rom.zip"))) {
        throw "ROM zip missing at $(Join-Path $romPath "$Rom.zip"). Run pinball-setup\add-rom.ps1 -RomZip <path>."
    }
    # MAME treats -record <name> as <inppath>/<name>, so pass the directory via
    # -input_directory and just the basename via -record.
    $inpDir  = Split-Path -Parent $inpPath
    $inpName = Split-Path -Leaf $inpPath
    Write-Step "Launching PinMAME with -record"
    Write-Host "    $pinmameExe -rompath `"$romPath`" -input_directory `"$inpDir`" -record $inpName $Rom" -ForegroundColor DarkGray
    Write-Warn2 "Play, then close the PinMAME window to stop recording."

    $writer = New-RpSessionWriter -Path $sessionPath
    try {
        $writer.WriteMeta(@{
            rom               = $Rom
            rom_zip_sha256    = $romZipSha256
            table_path        = $null
            table_sha256      = $null
            vpm_version       = $null
            pinmame_version   = $null
            mode              = 'InpOnly'
            poll_hz           = 0
            start_ts          = (Get-Date).ToUniversalTime().ToString('o')
            end_ts            = $null
            sample_loss_count = 0
            host              = $env:COMPUTERNAME
            comment           = "pinmame.exe -record produced $((Split-Path -Leaf $inpPath))"
        })
        $writer.Flush()
    } finally {
        $writer.Close()
    }

    # Block until PinMAME exits or MaxSeconds elapses.
    $pmArgs = @('-rompath', $romPath, '-input_directory', $inpDir, '-record', $inpName, $Rom)
    $stdoutLog = Join-Path $OutDir 'pinmame.stdout.log'
    $stderrLog = Join-Path $OutDir 'pinmame.stderr.log'
    $proc = Start-Process -FilePath $pinmameExe -ArgumentList $pmArgs -WorkingDirectory $pinmame `
        -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $proc.HasExited -and $sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
        Start-Sleep -Milliseconds 500
    }
    if (-not $proc.HasExited) {
        Write-Warn2 "MaxSeconds reached; closing PinMAME."
        try { $proc.CloseMainWindow() | Out-Null } catch {}
        Start-Sleep -Seconds 2
        if (-not $proc.HasExited) { $proc.Kill() }
    }

    if ($sw.Elapsed.TotalSeconds -lt 3) {
        Write-Warn2 "PinMAME exited after $([math]::Round($sw.Elapsed.TotalSeconds,1))s (exit $($proc.ExitCode)). Likely a startup error."
        if ((Test-Path $stderrLog) -and (Get-Item $stderrLog).Length -gt 0) {
            Write-Host "--- pinmame.stderr.log ---" -ForegroundColor Yellow
            Get-Content $stderrLog | Write-Host
        }
        if ((Test-Path $stdoutLog) -and (Get-Item $stdoutLog).Length -gt 0) {
            Write-Host "--- pinmame.stdout.log ---" -ForegroundColor Yellow
            Get-Content $stdoutLog | Write-Host
        }
    }

    # Patch meta with end_ts. Be defensive — the session.jsonl may not exist yet
    # if PinMAME crashed before we wrote anything observable.
    if ((Test-Path $sessionPath) -and (Get-Item $sessionPath).Length -gt 0) {
        $lines = @(Get-Content -LiteralPath $sessionPath)
        if ($lines.Count -gt 0 -and $lines[0].Trim()) {
            try {
                $first = $lines[0] | ConvertFrom-Json
                $first.end_ts = (Get-Date).ToUniversalTime().ToString('o')
                $lines[0] = ($first | ConvertTo-Json -Compress -Depth 6)
                Set-Content -LiteralPath $sessionPath -Value $lines -Encoding utf8
            } catch {
                Write-Warn2 "Could not patch end_ts in $sessionPath ($_); leaving as-is."
            }
        }
    }

    if (-not (Test-Path $inpPath) -or (Get-Item $inpPath).Length -eq 0) {
        Write-Warn2 "No .inp produced (or empty). PinMAME may have exited before any input."
    } else {
        Write-Ok ".inp written: $inpPath ($((Get-Item $inpPath).Length) bytes)"
    }

    # Copy session.meta.json (handy for tooling that doesn't want to parse JSONL).
    @{
        rom            = $Rom
        rom_zip_sha256 = $romZipSha256
        mode           = 'InpOnly'
        inp            = (Split-Path -Leaf $inpPath)
        session_jsonl  = (Split-Path -Leaf $sessionPath)
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $metaPath

    Write-Host ""
    Write-Host "Recording complete (InpOnly)." -ForegroundColor Green
    Write-Host "  Session: $sessionPath"
    Write-Host "  Inputs:  $inpPath"
    return
}

# ============================================================================
# VpVpm mode
# ============================================================================

if (-not $vpinball -or -not (Test-Path $vpinball)) {
    throw "VPINBALL_DIR not set (run pinball-setup\setup-pinball.ps1)."
}
if (-not $vpinmame -or -not (Test-Path $vpinmame)) {
    throw "VPINMAME_DIR not set (run pinball-setup\setup-pinball.ps1)."
}

# Resolve table from config.json:
#   1. New schema: cfg.tables[$Rom] -> path
#   2. Legacy schema: cfg.table_path (pre-add-rom.ps1) — used if nothing else matches.
if (-not $Table) {
    $cfg = Read-RpConfig -Path $ConfigPath
    if ($cfg.PSObject.Properties.Match('tables').Count -gt 0 -and
        $cfg.tables -and
        $cfg.tables.PSObject.Properties.Match($Rom).Count -gt 0) {
        $Table = [string]$cfg.tables.$Rom
    }
    elseif ($cfg.PSObject.Properties.Match('table_path').Count -gt 0) {
        $Table = [string]$cfg.table_path
    }
}
if (-not $Table -or -not (Test-Path $Table)) {
    throw "No table registered for ROM '$Rom'. Run pinball-setup\add-rom.ps1 -RomZip <path> [-Table <vpx>], or pass -Table here, or use -Mode InpOnly to skip the table view."
}
$Table = (Resolve-Path $Table).Path
$tableSha256 = Get-FileSha256 -Path $Table

$vpxExe = Join-Path $vpinball 'VPinballX64.exe'
if (-not (Test-Path $vpxExe)) { throw "VPinballX64.exe not found at $vpxExe." }

# Load switch set.
if (-not $SwitchSet) {
    $SwitchSet = Join-Path $PSScriptRoot "schemas\switches\$Rom.json"
}
$switchList = $null
if (Test-Path $SwitchSet) {
    $sw = Get-Content -Raw $SwitchSet | ConvertFrom-Json
    $direct = @()
    if ($sw.PSObject.Properties.Match('direct_switches').Count -gt 0) {
        $direct = @($sw.direct_switches.PSObject.Properties.Name | ForEach-Object { [int]$_ })
    }
    $matrix = @()
    if ($sw.PSObject.Properties.Match('matrix_switches').Count -gt 0) {
        $matrix = @($sw.matrix_switches.PSObject.Properties.Name | ForEach-Object { [int]$_ })
    }
    $switchList = ($direct + $matrix) | Sort-Object -Unique
    Write-Ok "Switch set: $($switchList.Count) switches from $SwitchSet"
}
else {
    Write-Warn2 "No switch set at $SwitchSet — will discover responsive switches after VPM starts."
}

# --- Launch VP / attach to VPM ----------------------------------------------

Write-Step "Launching Visual Pinball with $Table"
$vpProc = Start-Process -FilePath $vpxExe -ArgumentList @('-play', "`"$Table`"") -WorkingDirectory $vpinball -PassThru
Write-Ok "VP pid $($vpProc.Id)"

Write-Step "Attaching to VPinMAME COM"
$ctrl = New-VpmController
Wait-VpmRunning -Controller $ctrl -TimeoutSeconds 90 -ExpectedRom $Rom
$vpmVersion = Get-VpmVersion -Controller $ctrl
Write-Ok "VPM running ($vpmVersion)"

# If we didn't have a switch set, discover one now.
if (-not $switchList) {
    Write-Step "Discovering responsive switches"
    $switchList = Find-VpmResponsiveSwitches -Controller $ctrl
    Write-Ok "Discovered $($switchList.Count) responsive switches."
    # Persist for future runs.
    $discPath = Join-Path $PSScriptRoot "schemas\switches\$Rom.discovered.json"
    @{
        rom = $Rom
        discovered_at = (Get-Date).ToUniversalTime().ToString('o')
        switches = $switchList
    } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $discPath
    Write-Ok "Wrote $discPath"
}

# --- Recording loop ----------------------------------------------------------

$writer = New-RpSessionWriter -Path $sessionPath
$startTs = (Get-Date).ToUniversalTime().ToString('o')
$writer.WriteMeta(@{
    rom               = $Rom
    rom_zip_sha256    = $romZipSha256
    table_path        = $Table
    table_sha256      = $tableSha256
    vpm_version       = $vpmVersion
    pinmame_version   = $null
    mode              = 'VpVpm'
    poll_hz           = $PollHz
    start_ts          = $startTs
    end_ts            = $null
    sample_loss_count = 0
    host              = $env:COMPUTERNAME
    comment           = $null
})
$writer.Flush()

# Subscribe to OnSolenoid events.
$solEvt = $null
try {
    $solEvt = Register-ObjectEvent -InputObject $ctrl -EventName 'OnSolenoid' `
        -MessageData @{ Writer = $writer; Origin = [Diagnostics.Stopwatch]::StartNew() } `
        -Action {
            $w = $Event.MessageData.Writer
            $o = $Event.MessageData.Origin
            $t = $o.Elapsed.TotalSeconds
            $n = [int]$Event.SourceEventArgs.solenoid
            $on = [bool]$Event.SourceEventArgs.isActive
            $w.WriteSolenoidEvt($t, $n, $on)
        }
    Write-Ok "OnSolenoid subscribed."
} catch {
    Write-Warn2 "Could not subscribe to OnSolenoid (`$_`); continuing without event-driven solenoid log."
}

# State for delta detection.
$prevSwitches = @{}
foreach ($n in $switchList) { $prevSwitches[$n] = $false }
$prevLamps    = @{}
$prevSols     = @{}
$prevGis      = @{}
$sampleLoss   = 0

$origin = [Diagnostics.Stopwatch]::StartNew()
$periodTicks = [int]([Diagnostics.Stopwatch]::Frequency / $PollHz)
$nextTick = $origin.ElapsedTicks + $periodTicks

Write-Step "Recording — play; press Ctrl-C or close VP to stop. Max ${MaxSeconds}s."

$stopFlag = $false
try {
    while (-not $stopFlag) {
        # Sleep until next tick. Spin-wait for sub-ms accuracy past Start-Sleep's resolution.
        $now = $origin.ElapsedTicks
        $wait = $nextTick - $now
        if ($wait -gt 200000) {
            # > ~20ms: cheap sleep
            $ms = [Math]::Floor(($wait * 1000) / [Diagnostics.Stopwatch]::Frequency)
            if ($ms -gt 1) { Start-Sleep -Milliseconds ([int]($ms - 1)) }
        }
        while ($origin.ElapsedTicks -lt $nextTick) {
            [Threading.Thread]::SpinWait(50)
        }
        $tSec = $origin.Elapsed.TotalSeconds

        # Detect overrun (we missed a window).
        $expected = $nextTick
        $nextTick += $periodTicks
        if ($origin.ElapsedTicks - $expected -gt 2 * $periodTicks) {
            $sampleLoss++
            # Advance nextTick to catch up rather than spam ticks.
            while ($nextTick -lt $origin.ElapsedTicks) { $nextTick += $periodTicks }
        }

        # Has VP exited?
        try {
            if ($vpProc.HasExited) { Write-Warn2 "VP exited (code $($vpProc.ExitCode))."; break }
        } catch {}
        # Has VPM left Running?
        try {
            if (-not $ctrl.Running) { Write-Warn2 "VPM stopped Running."; break }
        } catch {
            Write-Warn2 "VPM COM threw on Running check; stopping."
            break
        }

        # Switch deltas.
        foreach ($n in $switchList) {
            $cur = $false
            try { $cur = [bool]$ctrl.Switch($n) } catch { continue }
            if ($cur -ne $prevSwitches[$n]) {
                $writer.WriteSwitch($tSec, $n, $cur)
                $prevSwitches[$n] = $cur
            }
        }

        # ChangedLamps -> {{n, v}, ...}. Per VPM docs the call returns a 2D array.
        try {
            $changes = $ctrl.ChangedLamps()
            if ($changes) {
                # COM may marshal as object[,] — iterate by row index.
                $rows = $changes.GetLength(0)
                for ($i = 0; $i -lt $rows; $i++) {
                    $n = [int]$changes.GetValue($i, 0)
                    $v = [int]$changes.GetValue($i, 1)
                    if (-not $prevLamps.ContainsKey($n) -or $prevLamps[$n] -ne $v) {
                        $writer.WriteObservation($tSec, 'lamp', $n, $v)
                        $prevLamps[$n] = $v
                    }
                }
            }
        } catch { }
        try {
            $changes = $ctrl.ChangedSolenoids()
            if ($changes) {
                $rows = $changes.GetLength(0)
                for ($i = 0; $i -lt $rows; $i++) {
                    $n = [int]$changes.GetValue($i, 0)
                    $v = [int]$changes.GetValue($i, 1)
                    if (-not $prevSols.ContainsKey($n) -or $prevSols[$n] -ne $v) {
                        $writer.WriteObservation($tSec, 'sol', $n, $v)
                        $prevSols[$n] = $v
                    }
                }
            }
        } catch { }
        try {
            $changes = $ctrl.ChangedGIStrings()
            if ($changes) {
                $rows = $changes.GetLength(0)
                for ($i = 0; $i -lt $rows; $i++) {
                    $n = [int]$changes.GetValue($i, 0)
                    $v = [int]$changes.GetValue($i, 1)
                    if (-not $prevGis.ContainsKey($n) -or $prevGis[$n] -ne $v) {
                        $writer.WriteObservation($tSec, 'gi', $n, $v)
                        $prevGis[$n] = $v
                    }
                }
            }
        } catch { }

        if ($tSec -ge $MaxSeconds) {
            Write-Warn2 "MaxSeconds reached."
            break
        }
    }
}
finally {
    Write-Step "Stopping"

    if ($solEvt) {
        try { Unregister-Event -SourceIdentifier $solEvt.Name -ErrorAction SilentlyContinue } catch {}
        try { $solEvt | Remove-Job -Force -ErrorAction SilentlyContinue } catch {}
    }

    # Patch meta with end_ts + sample_loss_count.
    $writer.Flush()
    $writer.Close()
    $lines = Get-Content -LiteralPath $sessionPath
    if ($lines.Count -gt 0) {
        $first = $lines[0] | ConvertFrom-Json
        $first.end_ts            = (Get-Date).ToUniversalTime().ToString('o')
        $first.sample_loss_count = $sampleLoss
        $lines[0] = ($first | ConvertTo-Json -Compress -Depth 6)
        Set-Content -LiteralPath $sessionPath -Value $lines -Encoding utf8
    }

    @{
        rom            = $Rom
        rom_zip_sha256 = $romZipSha256
        mode           = 'VpVpm'
        table_path     = $Table
        table_sha256   = $tableSha256
        session_jsonl  = (Split-Path -Leaf $sessionPath)
        sample_loss_count = $sampleLoss
        switch_set_path   = $SwitchSet
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $metaPath

    # Try to close VP cleanly.
    try {
        if (-not $vpProc.HasExited) {
            Write-Warn2 "Closing VP."
            $vpProc.CloseMainWindow() | Out-Null
            Start-Sleep -Seconds 1
            if (-not $vpProc.HasExited) { $vpProc.Kill() }
        }
    } catch {}
}

Write-Host ""
Write-Host "Recording complete." -ForegroundColor Green
Write-Host "  Session:    $sessionPath"
Write-Host "  Meta:       $metaPath"
Write-Host "  Sample loss: $sampleLoss"
Write-Host ""
Write-Host "Next: python replay.py --rom <name> --rom-zip <zip> --session $OutDir --nvram <nv> --trace state,dmd" -ForegroundColor Yellow
