#requires -Version 7.0
<#
.SYNOPSIS
    Registers a WPC ROM (and optionally its Visual Pinball table) with the record-pinball
    skill: stages the ROM zip into VPM's roms\ directory and records the .vpx path in
    config.json so record.py can look it up by ROM name.

.DESCRIPTION
    Idempotent. Run once per game you want to record/replay.

      add-rom.ps1 -RomZip <path-to-zip> [-Rom <name>] [-Table <path-to-vpx>] [-Force]

    The ROM zip is copied (not extracted) into %VPINMAME_DIR%\roms\<rom>.zip — VPM reads
    the zip directly and resolves by gamename. If -Rom is omitted it defaults to the
    zip's basename (e.g. `congo_21.zip` -> `congo_21`).

    The VPX table is third-party community content (VPUniverse / VPForums) and is never
    auto-downloaded. Pass -Table to register a path you already have; omit -Table to be
    prompted, or pass -SkipTable to stage the ROM now and register a table later. A table
    is required before record.py can capture a session.

.PARAMETER RomZip
    Path to a ROM zip (the same shape VPM expects under its roms\ dir).

.PARAMETER Rom
    The gamename. Default: the zip's filename without extension.

.PARAMETER Table
    Path to a .vpx file. If supplied, it is copied to %VPINBALL_DIR%\Tables\ and the
    config records the in-Tables location.

.PARAMETER SkipTable
    Don't prompt for a table; stage the ROM only. record.py needs a table registered
    (re-run add-rom.ps1 -Table <vpx>) before it can record.

.PARAMETER ConfigPath
    Path to the project config file. Default: .\config.json (relative to CWD, i.e. the
    project root). Falls back to %LOCALAPPDATA%\record-pinball\config.json only if you
    pass that path explicitly.

.PARAMETER Force
    Overwrite a staged ROM zip or registered table even if already present.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $RomZip,
    [string] $Rom,
    [string] $Table,
    [switch] $SkipTable,
    [string] $ConfigPath = '.\config.json',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'lib\Common.ps1')

# --- Resolve env / paths -----------------------------------------------------

$vpinmame = Get-RpEnvVar 'VPINMAME_DIR'
$vpinball = Get-RpEnvVar 'VPINBALL_DIR'
if (-not $vpinmame) { throw "VPINMAME_DIR not set. Run setup-pinball.py first." }
if (-not $vpinball) { throw "VPINBALL_DIR not set. Run setup-pinball.py first." }

if (-not (Test-Path $RomZip)) { throw "ROM zip not found: $RomZip" }
$RomZip = (Resolve-Path $RomZip).Path

if (-not $Rom) {
    $Rom = [System.IO.Path]::GetFileNameWithoutExtension($RomZip)
}
if (-not ($Rom -match '^[a-z0-9_]+$')) {
    Write-Warn2 "ROM name '$Rom' contains characters VPM may not accept (expected [a-z0-9_]+)."
}

# --- Stage ROM zip into %VPINMAME_DIR%\roms\<rom>.zip ------------------------

$romsDir   = Join-Path $vpinmame 'roms'
if (-not (Test-Path $romsDir)) { New-Item -ItemType Directory -Path $romsDir | Out-Null }
$stagedZip = Join-Path $romsDir "$Rom.zip"

Write-Step "Staging $Rom"
if ((Test-Path $stagedZip) -and -not $Force) {
    $srcHash = Get-FileSha256 -Path $RomZip
    $dstHash = Get-FileSha256 -Path $stagedZip
    if ($srcHash -eq $dstHash) {
        Write-Ok "Already staged at $stagedZip (hash matches)."
    }
    else {
        Write-Warn2 "Already staged at $stagedZip but content differs from $RomZip. Pass -Force to replace."
    }
}
else {
    Copy-Item -Path $RomZip -Destination $stagedZip -Force
    Write-Ok "Copied $RomZip -> $stagedZip"
}

# --- Register VPX table ------------------------------------------------------

$tablesDir = Join-Path $vpinball 'Tables'
if (-not (Test-Path $tablesDir)) { New-Item -ItemType Directory -Path $tablesDir | Out-Null }

# Load config and migrate from the old flat-table_path schema if present.
$cfg = Read-RpConfig -Path $ConfigPath
if (-not ($cfg.PSObject.Properties.Match('tables').Count -gt 0)) {
    $cfg | Add-Member -NotePropertyName 'tables' -NotePropertyValue ([pscustomobject]@{}) -Force
}
# Migration: an older config.json had `table_path`; promote it onto the new map
# keyed by best-guess rom (no rom recorded historically, so park under "_legacy").
if ($cfg.PSObject.Properties.Match('table_path').Count -gt 0 -and $cfg.table_path) {
    if (-not ($cfg.tables.PSObject.Properties.Match('_legacy').Count -gt 0)) {
        $cfg.tables | Add-Member -NotePropertyName '_legacy' -NotePropertyValue $cfg.table_path -Force
    }
    $cfg.PSObject.Properties.Remove('table_path')
}

$existing = $null
if ($cfg.tables.PSObject.Properties.Match($Rom).Count -gt 0) {
    $existing = [string]$cfg.tables.$Rom
    if (-not (Test-Path $existing)) { $existing = $null }
}

Write-Step "VPX table for $Rom"
$resolvedTable = $null
if ($Table) {
    $Table = $Table.Trim('"').Trim()
    if (-not (Test-Path $Table)) { throw "Table file not found: $Table" }
    $Table = (Resolve-Path $Table).Path
    $target = Join-Path $tablesDir (Split-Path -Leaf $Table)
    if ((Test-Path $target) -and (Resolve-Path $target).Path -eq $Table) {
        # Already in Tables\; just record it.
        $resolvedTable = $Table
    } else {
        Copy-Item -Path $Table -Destination $target -Force
        $resolvedTable = $target
    }
    Write-Ok "Table at $resolvedTable"
}
elseif ($existing -and -not $Force) {
    Write-Ok "Table already registered: $existing"
    $resolvedTable = $existing
}
elseif ($SkipTable) {
    Write-Warn2 "Skipping table registration. uv run record.py --rom $Rom needs a table — re-run add-rom.ps1 -Rom $Rom -Table <vpx> first."
}
else {
    Write-Host ""
    Write-Host "VPX tables are third-party community content (VPUniverse / VPForums) and are never auto-downloaded." -ForegroundColor Yellow
    Write-Host "Options:"
    Write-Host "  [1] Open VPUniverse search; drop the .vpx into $tablesDir and re-run add-rom.ps1 -Rom $Rom."
    Write-Host "  [2] Paste a .vpx path now."
    Write-Host "  [3] Skip for now (stage the ROM; register a table later to record)."
    Write-Host "  [4] Cancel."
    $choice = Read-Host "Choose [1/2/3/4]"
    switch ($choice) {
        '1' {
            $url = "https://vpuniverse.com/files/category/82-vpx-pinball-tables/?do=search&search=$Rom"
            Start-Process $url
            Write-Warn2 "Opened $url. Drop the .vpx into $tablesDir and re-run add-rom.ps1 -Rom $Rom -Table <path>."
        }
        '2' {
            $path = (Read-Host "Path to .vpx").Trim('"').Trim()
            if (-not (Test-Path $path)) { throw "Not a file: $path" }
            $path = (Resolve-Path $path).Path
            $target = Join-Path $tablesDir (Split-Path -Leaf $path)
            Copy-Item -Path $path -Destination $target -Force
            $resolvedTable = $target
            Write-Ok "Table at $resolvedTable"
        }
        '3' {
            Write-Warn2 "Skipping."
        }
        default {
            throw "Cancelled."
        }
    }
}

if ($resolvedTable) {
    $cfg.tables | Add-Member -NotePropertyName $Rom -NotePropertyValue $resolvedTable -Force
}
Write-RpConfig -Config $cfg -Path $ConfigPath

# --- Summary -----------------------------------------------------------------

Write-Host ""
Write-Host "Registered $Rom." -ForegroundColor Green
Write-Host "  Staged ROM:   $stagedZip"
if ($resolvedTable) {
    Write-Host "  VPX table:    $resolvedTable"
} else {
    Write-Host "  VPX table:    (none — register one with -Table before recording)" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next: uv run record.py --rom $Rom" -ForegroundColor Yellow
