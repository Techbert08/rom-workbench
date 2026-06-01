#requires -Version 7.0
<#
.SYNOPSIS
    Records a gameplay session against a WPC ROM in a format that replay.py can drive
    headlessly later. Launches Visual Pinball + VPinMAME and captures the replayable
    switch-edge stream via the patched VPinMAME64.dll. This is the Windows counterpart
    to record.sh (macOS); both produce the same session.jsonl.

.DESCRIPTION
    Launches Visual Pinball on the configured .vpx table. VP drives the switch matrix
    through the VPinMAME COM Controller, which funnels through `vp_putSwitch`; the
    patched VPinMAME64.dll (VPINMAME_SWITCHLOG) logs every switch EDGE there with an
    emulation-clock timestamp into sessions/<utc>/switchlog.jsonl, which this script
    folds into session.jsonl as `kind:"switch"` records. This is the stream replay.py
    injects via PinmameSetSwitch, so gameplay reproduces faithfully at replay time.

    Press Ctrl-C in this terminal (or close the Visual Pinball window) to stop recording.

.PARAMETER Rom
    VPM ROM name (matches the .zip stem under %VPINMAME_DIR%\roms\). Default: congo_21.

.PARAMETER Table
    Path to a .vpx table file. Default: looked up from -ConfigPath by ROM name.

.PARAMETER ConfigPath
    Path to the project config file. Default: .\config.json (relative to CWD, i.e. the
    project root). Written by pinball-setup\add-rom.ps1.

.PARAMETER OutDir
    Output directory. Default: .\sessions\<UTC-yyyyMMddTHHmmssZ>.

.PARAMETER MaxSeconds
    Safety stop. Default 600 (10 min). Recording auto-terminates after this many seconds.
#>
[CmdletBinding()]
param(
    [string] $Rom = 'congo_21',
    [string] $Table,
    [string] $OutDir,
    [int] $MaxSeconds = 600,
    [string] $ConfigPath = '.\config.json'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'lib\Common.ps1')
. (Join-Path $PSScriptRoot 'lib\SessionSchema.ps1')

# --- Resolve env / paths -----------------------------------------------------

$vpinball  = Get-RpEnvVar 'VPINBALL_DIR'
$vpinmame  = Get-RpEnvVar 'VPINMAME_DIR'

if (-not $vpinball -or -not (Test-Path $vpinball)) {
    throw "VPINBALL_DIR not set or missing (run pinball-setup\setup-pinball.py)."
}
if (-not $vpinmame -or -not (Test-Path $vpinmame)) {
    throw "VPINMAME_DIR not set or missing (run pinball-setup\setup-pinball.py)."
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
    throw "ROM zip not staged at $romZip. Run pinball-setup\setup-pinball.py, or place $Rom.zip in %VPINMAME_DIR%\roms\."
}
$romZipSha256 = Get-FileSha256 -Path $romZip

# --- Resolve VPX exe + table -------------------------------------------------

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

# Switch-stream log. The patched VPinMAME64.dll writes every externally driven
# switch change (the COM Controller's put_Switch/put_Switches funnel through
# vp_putSwitch) here as JSONL "switch" records stamped with the emulation clock,
# when VPINMAME_SWITCHLOG names a file. This is the *replayable* input stream.
$switchLog = Join-Path $OutDir 'switchlog.jsonl'
Remove-Item $switchLog -ErrorAction SilentlyContinue

# --- Write meta line (first line of session.jsonl) ---------------------------

$writer = New-RpSessionWriter -Path $sessionPath
$writer.WriteMeta(@{
    rom               = $Rom
    rom_zip_sha256    = $romZipSha256
    table_path        = $Table
    table_sha256      = $tableSha256
    vpm_version       = $null
    pinmame_version   = $null
    mode              = 'VpRecord'
    start_ts          = (Get-Date).ToUniversalTime().ToString('o')
    end_ts            = $null
    host              = $env:COMPUTERNAME
    comment           = "Windows record.ps1 VpRecord session"
})
$writer.Flush()
$writer.Close()

# --- Launch Visual Pinball ---------------------------------------------------

Write-Step "Launching Visual Pinball (VpRecord mode)"
Write-Host "  Table:  $Table" -ForegroundColor DarkGray
Write-Warn2 "Play, then close the Visual Pinball window to stop recording."

# Set VPINMAME_SWITCHLOG in the child environment so the patched DLL picks it up
# and writes the replayable switch stream.
$env:VPINMAME_SWITCHLOG = $switchLog
$timedOut = $false
try {
    $vpProc = Start-Process -FilePath $vpxExe -ArgumentList @('-play', "`"$Table`"") `
        -WorkingDirectory $vpinball -PassThru
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $vpProc.HasExited -and $sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
        Start-Sleep -Milliseconds 500
    }
    if (-not $vpProc.HasExited) {
        Write-Warn2 "MaxSeconds reached; closing Visual Pinball."
        $timedOut = $true
        try { $vpProc.CloseMainWindow() | Out-Null } catch {}
        Start-Sleep -Seconds 2
        if (-not $vpProc.HasExited) { $vpProc.Kill() }
    }
} finally {
    $env:VPINMAME_SWITCHLOG = ''
}

# --- Fold the captured switch stream into session.jsonl ----------------------
# replay.py / replay_host.py inject these via PinmameSetSwitch at their recorded
# emulation-clock timestamps — the same swMatrix plane VP drove during recording.

$swCount = 0
if (Test-Path $switchLog) {
    $swLines = @(Get-Content -LiteralPath $switchLog | Where-Object { $_.Trim() })
    if ($swLines.Count -gt 0) {
        Add-Content -LiteralPath $sessionPath -Value $swLines -Encoding utf8
        $swCount = $swLines.Count
        Write-Ok "Switch stream: $swCount events folded into session.jsonl"
    } else {
        Write-Warn2 "switchlog.jsonl is empty — no switches were captured (did you actually play?)."
    }
} else {
    Write-Warn2 "No switchlog.jsonl produced — the deployed VPinMAME64.dll may predate the switch recorder."
    Write-Warn2 "Verify setup-pinball.py ran successfully and VPINMAME_DIR\VPinMAME64.dll is the patched version."
}

# --- Write session.meta.json -------------------------------------------------

@{
    rom            = $Rom
    rom_zip_sha256 = $romZipSha256
    mode           = 'VpRecord'
    table_path     = $Table
    table_sha256   = $tableSha256
    session_jsonl  = (Split-Path -Leaf $sessionPath)
    labels         = @()
    notes          = ''
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $metaPath

Write-Host ""
Write-Host "Recording complete (VpRecord)." -ForegroundColor Green
Write-Host "  Session:  $sessionPath"
Write-Host "  Switches: $swCount"
if ($timedOut) { Write-Warn2 "Session timed out after ${MaxSeconds}s." }
Write-Host ""
Write-Host "Next: uv run replay.py --rom $Rom --rom-zip <zip> --session $OutDir --nvram <nv> --trace state,dmd" -ForegroundColor Yellow
