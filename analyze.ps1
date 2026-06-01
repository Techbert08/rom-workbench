#requires -Version 7.0
<#
.SYNOPSIS
    Runs Ghidra headless analysis on a WPC pinball ROM using the WPC loader.

.DESCRIPTION
    Accepts either a ROM zip (typical IPDB/archive distribution) or a single ROM
    binary file. If a zip is provided, extracts it and selects the largest file
    whose size matches a valid WPC ROM size (128 KiB / 256 KiB / 512 KiB / 1 MiB).

    Imports into a Ghidra project at -ProjectDir (default: ./ghidra-project) and
    runs auto-analysis. Optionally exports a disassembly listing via -ExportListing.

    Requires setup.ps1 to have completed (GHIDRA_INSTALL_DIR must point at a
    Ghidra 12.0.4 install with the WPC loader extension installed).

.PARAMETER RomZip
    Path to a zip archive containing the ROM binary.

.PARAMETER RomFile
    Path to an already-extracted ROM binary.

.PARAMETER ProjectDir
    Directory to hold the Ghidra project. Default: ./ghidra-project (relative to CWD).

.PARAMETER ProjectName
    Ghidra project name. Default: derived from the ROM filename.

.PARAMETER Overwrite
    Re-import even if the program already exists in the project.

.PARAMETER NoDecompile
    Skip the post-analysis decompilation pass. By default, the script runs
    DecompileAllScript.py to emit <ProjectName>.decompiled.c next to the project.
#>
[CmdletBinding(DefaultParameterSetName = 'Zip')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Zip')]
    [string] $RomZip,

    [Parameter(Mandatory, ParameterSetName = 'File')]
    [string] $RomFile,

    [string] $ProjectDir = (Join-Path (Get-Location) 'ghidra-project'),
    [string] $ProjectName,
    [switch] $Overwrite,
    [switch] $NoDecompile
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step  ([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    ([string] $msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2 ([string] $msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# --- Locate Ghidra ------------------------------------------------------------

if (-not $env:GHIDRA_INSTALL_DIR) {
    $env:GHIDRA_INSTALL_DIR = [Environment]::GetEnvironmentVariable('GHIDRA_INSTALL_DIR', 'User')
}
if (-not $env:GHIDRA_INSTALL_DIR -or -not (Test-Path $env:GHIDRA_INSTALL_DIR)) {
    throw "GHIDRA_INSTALL_DIR is not set or does not exist. Run setup.ps1 first."
}
$analyzeHeadless = Join-Path $env:GHIDRA_INSTALL_DIR 'support\analyzeHeadless.bat'
if (-not (Test-Path $analyzeHeadless)) {
    throw "analyzeHeadless.bat missing at $analyzeHeadless. Re-run setup.ps1."
}

$wpcExt = Join-Path $env:GHIDRA_INSTALL_DIR 'Ghidra\Extensions\ghidra_wpc_loader\Module.manifest'
if (-not (Test-Path $wpcExt)) {
    throw "WPC loader not installed at $wpcExt. Re-run setup.ps1."
}

# --- Resolve the ROM file -----------------------------------------------------

$validRomSizes = @(131072, 262144, 524288, 1048576)  # 128K, 256K, 512K, 1M

if ($PSCmdlet.ParameterSetName -eq 'Zip') {
    if (-not (Test-Path $RomZip)) { throw "ROM zip not found: $RomZip" }
    $RomZip = (Resolve-Path $RomZip).Path

    $extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wpc-rom-" + [System.IO.Path]::GetFileNameWithoutExtension($RomZip))
    if (Test-Path $extractRoot) { Remove-Item -Recurse -Force $extractRoot }
    New-Item -ItemType Directory -Path $extractRoot | Out-Null

    Write-Step "Extracting $RomZip to $extractRoot"
    Expand-Archive -Path $RomZip -DestinationPath $extractRoot -Force

    $candidates = Get-ChildItem -Path $extractRoot -Recurse -File |
                  Where-Object { $validRomSizes -contains $_.Length }

    if (-not $candidates) {
        $all = Get-ChildItem -Path $extractRoot -Recurse -File | Select-Object Name, Length
        throw "No file in $RomZip matches a valid WPC ROM size (128K/256K/512K/1M). Contents: $($all | Format-Table | Out-String)"
    }

    # WPC archives mix the 6809 game ROM with DCS sound ROMs (ADSP-2105, different CPU).
    # Naming convention: game ROMs contain "_g" (e.g. "cg_g11.2_1"); sound ROMs use
    # "s<digit>" near the game-code prefix (e.g. "cgs2v1_1.rom" -> Congo Sound U2).
    # Score each candidate so the game ROM wins automatically.
    function Get-RomScore([string] $name) {
        $n = $name.ToLower()
        $score = 0
        if ($n -match '_g')                       { $score += 10 }    # explicit game-ROM marker
        if ($n -match '^[a-z]{2,4}s\d')           { $score -= 10 }    # e.g. cgs2, ttsnd2
        if ($n -match 'sound|snd|dcs')            { $score -= 5 }
        return $score
    }
    $scored = $candidates | ForEach-Object {
        [pscustomobject]@{ File = $_; Score = (Get-RomScore $_.Name) }
    } | Sort-Object -Property @{Expression='Score';Descending=$true},
                              @{Expression={$_.File.Length};Descending=$true}

    if ($candidates.Count -gt 1) {
        Write-Warn2 "Multiple ROM-sized files found:"
        $scored | ForEach-Object {
            Write-Warn2 ("  score={0,3}  {1,10} bytes  {2}" -f $_.Score, $_.File.Length, $_.File.Name)
        }
    }
    $RomFile = $scored[0].File.FullName
    Write-Ok "Selected ROM: $RomFile ($($candidates[0].Length) bytes)"
}
else {
    if (-not (Test-Path $RomFile)) { throw "ROM file not found: $RomFile" }
    $RomFile = (Resolve-Path $RomFile).Path
    $size = (Get-Item $RomFile).Length
    if ($validRomSizes -notcontains $size) {
        Write-Warn2 "ROM size $size bytes is not a standard WPC size; the loader may reject it."
    }
}

# --- Project setup ------------------------------------------------------------

if (-not $ProjectName) {
    $ProjectName = [System.IO.Path]::GetFileNameWithoutExtension($RomFile)
}

if (-not (Test-Path $ProjectDir)) {
    New-Item -ItemType Directory -Path $ProjectDir | Out-Null
}
$ProjectDir = (Resolve-Path $ProjectDir).Path

Write-Step "Project: $ProjectName at $ProjectDir"

# --- Build headless command ---------------------------------------------------

$scriptDir = Join-Path $PSScriptRoot 'ghidra_scripts'
if (-not (Test-Path $scriptDir)) {
    throw "Custom Ghidra scripts dir missing: $scriptDir"
}

$ghidraArgs = @(
    $ProjectDir,
    $ProjectName,
    '-import',     $RomFile,
    '-loader',     'WPCLoader',
    '-processor',  '6809:BE:16:default',
    '-scriptPath', $scriptDir,
    '-scriptlog',  (Join-Path $ProjectDir 'scriptlog.txt'),
    '-log',        (Join-Path $ProjectDir 'analyzeHeadless.log')
)
if ($Overwrite) { $ghidraArgs += '-overwrite' }

# Discovery passes run *first* so the decompile pass sees the new functions.
# Order matters: each pass exposes more disassembled code that the next can mine.
#   1. WpcBankXrefs      — harvest dangling $4xxx refs in system ROM, probe pages.
#   2. WpcPrologueScan   — scan code-dense pages for PSHS function-entry bytes.
#   3. WpcThunkResolve   — recover (bank, addr) pairs from $90C4 thunk callers.
#   4. WpcDisplayScripts — force-disassemble inline-parameter display scripts
#                          passed to WPC OS display utilities (e.g. JSR $D9A6
#                          <script-ptr>) so their bodies hit the decompile.
$ghidraArgs += @('-postScript', 'WpcBankXrefs.java')
$ghidraArgs += @('-postScript', 'WpcPrologueScan.java')
$ghidraArgs += @('-postScript', 'WpcThunkResolve.java')
$ghidraArgs += @('-postScript', 'WpcDisplayScripts.java')

$decompPath = $null
if (-not $NoDecompile) {
    $decompPath = Join-Path $ProjectDir "$ProjectName.decompiled.c"
    $ghidraArgs += @('-postScript', 'DecompileAllScript.java', $decompPath)
}

Write-Step "Running analyzeHeadless"
Write-Host "    $analyzeHeadless $($ghidraArgs -join ' ')" -ForegroundColor DarkGray

# launch.bat invokes `pause` if it thinks it was double-clicked (true when
# called via cmd /c, as PowerShell does). Pipe an EOF on stdin so pause
# returns immediately instead of blocking.
$null | & $analyzeHeadless @ghidraArgs
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    throw "analyzeHeadless exited with code $rc. See $ProjectDir\analyzeHeadless.log for details."
}

# analyzeHeadless returns 0 even if a post-script fails. Verify our output.
if ($decompPath -and -not (Test-Path $decompPath)) {
    throw "Analysis completed but $decompPath was not produced. Check $ProjectDir\analyzeHeadless.log for a SCRIPT ERROR."
}

Write-Host ""
Write-Host "Analysis complete." -ForegroundColor Green
Write-Host "  Project:    $ProjectDir\$ProjectName.gpr"
Write-Host "  Log:        $ProjectDir\analyzeHeadless.log"
if ($decompPath) {
    Write-Host "  Decompiled: $decompPath"
}
