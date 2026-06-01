#requires -Version 7.0
<#
.SYNOPSIS
    Installs the toolchain the record-pinball skill needs: Visual Pinball X,
    PinMAME (standalone + libpinmame), and VPinMAME (COM).

.DESCRIPTION
    Idempotent installer. Game-agnostic — installs tools only; use add-rom.ps1 to
    register a specific ROM + VPX table once the toolchain is in place.

    Steps:
      1. Verify PowerShell 7+ (this file's `#requires`) and ensure uv is installed
         (installing it if missing) — the Python tools run via `uv run`.
      2. Download + extract Visual Pinball X to %LOCALAPPDATA%\Programs\vpinball.
      3. Download + extract PinMAME standalone + libpinmame to %LOCALAPPDATA%\Programs\pinmame.
      4. Download + extract VPinMAME (the COM DLL) to %LOCALAPPDATA%\Programs\vpinmame
         and register it (needs an elevated PowerShell — script will detect and tell you).

    Sets user-scope env vars VPINBALL_DIR, PINMAME_DIR, VPINMAME_DIR.
    Re-running with no changes does nothing. Pass -Force to re-download/re-extract.

.PARAMETER Force
    Re-download and re-extract everything, even if already present.

.PARAMETER InstallRoot
    Override the install root. Defaults to $env:LOCALAPPDATA\Programs.
#>
[CmdletBinding()]
param(
    [switch] $Force,
    [string] $InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'lib\Common.ps1')

# --- Constants ---------------------------------------------------------------

# Pinned to known-good stable releases. Trust-on-first-use SHA-256: hashes recorded
# in the cache sidecar on first download. To upgrade, change Version + Url and (optionally)
# paste the new SHA-256 into Expected* below; an empty value means "trust on first use".
$VpxVersion    = '10.8.0-2051-28dd6c3'
$VpxFileName   = "Developer.VPinballX-$VpxVersion-Release-win-x64.zip"
$VpxUrl        = "https://github.com/vpinball/vpinball/releases/download/v$VpxVersion/$VpxFileName"
$ExpectedVpxSha256 = ''   # trust-on-first-use; pin once verified

$PinMameVersion    = '3.6.0-1227-ecd032e'
$PinMameFileName   = "PinMAME-$PinMameVersion-win-x64.zip"
$PinMameUrl        = "https://github.com/vpinball/pinmame/releases/download/v$PinMameVersion/$PinMameFileName"
$ExpectedPinMameSha256 = ''

$LibPinMameFileName = "libpinmame-$PinMameVersion-win-x64.zip"
$LibPinMameUrl      = "https://github.com/vpinball/pinmame/releases/download/v$PinMameVersion/$LibPinMameFileName"
$ExpectedLibPinMameSha256 = ''

$VpmComFileName = "VPinMAME-$PinMameVersion-win-x64.zip"
$VpmComUrl      = "https://github.com/vpinball/pinmame/releases/download/v$PinMameVersion/$VpmComFileName"
$ExpectedVpmComSha256 = ''

$VpxInstallDir     = Join-Path $InstallRoot 'vpinball'
$PinMameInstallDir = Join-Path $InstallRoot 'pinmame'
$VpmInstallDir     = Join-Path $InstallRoot 'vpinmame'

# --- Step 1: uv (runs every .py in this repo; provisions Python + deps) -------

Write-Step "Checking uv"

# The Python tools are PEP 723 single-file scripts run via `uv run`, so uv
# (not a system Python) is the only Python prerequisite — uv downloads a
# matching interpreter and each script's declared deps into an ephemeral env.
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    Write-Ok "uv $((& uv --version) -replace '^uv\s+','') at $($uvCmd.Source)"
} else {
    Write-Warn2 "uv not found — installing from https://astral.sh/uv ..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        throw "Failed to install uv automatically ($_). Install it manually (https://docs.astral.sh/uv/getting-started/installation/) and re-run."
    }
    # The installer adds uv under %USERPROFILE%\.local\bin; surface it now so
    # this run (and the verification below) can find it without a new shell.
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path (Join-Path $uvBin 'uv.exe')) { $env:PATH = "$uvBin;$env:PATH" }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv installed but not on PATH. Open a new shell (or add $uvBin to PATH) and re-run."
    }
    Write-Ok "uv installed: $((& uv --version))"
}

# --- Step 2: Visual Pinball X ------------------------------------------------

Write-Step "Visual Pinball X at $VpxInstallDir"
$vpxExe = Join-Path $VpxInstallDir 'VPinballX64.exe'
if ((Test-Path $vpxExe) -and -not $Force) {
    Write-Ok "VPinballX64.exe present; skipping."
}
else {
    $zip = Get-CachedDownload -Url $VpxUrl -FileName $VpxFileName -ExpectedSha256 $ExpectedVpxSha256 -Force:$Force
    Write-Step "Extracting Visual Pinball X to $VpxInstallDir"
    Expand-RpArchive -ZipPath $zip -Dest $VpxInstallDir -Strip
    if (-not (Test-Path $vpxExe)) {
        # The VPX zip layout sometimes drops VPinballX64.exe at the root; sometimes inside
        # a single top-level dir. Try one more pass without strip.
        $any = Get-ChildItem -Path $VpxInstallDir -Recurse -Filter 'VPinballX64.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $any) {
            throw "Visual Pinball X extraction did not produce VPinballX64.exe under $VpxInstallDir."
        }
        # Move its directory's contents to the install root.
        $srcDir = $any.DirectoryName
        Get-ChildItem -Path $srcDir -Force | ForEach-Object {
            Move-Item -Path $_.FullName -Destination $VpxInstallDir -Force
        }
    }
    Write-Ok "Visual Pinball X installed."
}
Set-RpEnvVar -Name 'VPINBALL_DIR' -Value $VpxInstallDir

# --- Step 3: PinMAME standalone + libpinmame ---------------------------------

Write-Step "PinMAME standalone + libpinmame at $PinMameInstallDir"
$pinmameExe   = Join-Path $PinMameInstallDir 'PinMAME.exe'
$libPinmameDll = Join-Path $PinMameInstallDir 'libpinmame.dll'

# libpinmame ships with a version suffix (e.g. libpinmame-3.6.dll). We treat any
# libpinmame*.dll under $PinMameInstallDir as good enough and (re)create a canonical
# libpinmame.dll copy alongside it for downstream code that loads by fixed name.
function Find-LibPinMameDll {
    Get-ChildItem -Path $PinMameInstallDir -Recurse -File -Filter 'libpinmame*.dll' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^libpinmame.*\.dll$' } |
        Select-Object -First 1
}
$pinmameOk = (Test-Path $pinmameExe) -and ((Find-LibPinMameDll) -ne $null)
if ($pinmameOk -and -not $Force) {
    Write-Ok "PinMAME.exe and libpinmame.dll present; skipping."
}
else {
    if (-not (Test-Path $PinMameInstallDir)) {
        New-Item -ItemType Directory -Path $PinMameInstallDir | Out-Null
    }

    # Extract standalone EXE archive.
    $zip1 = Get-CachedDownload -Url $PinMameUrl -FileName $PinMameFileName -ExpectedSha256 $ExpectedPinMameSha256 -Force:$Force
    Write-Step "Extracting PinMAME standalone"
    $tmp1 = Join-Path $env:TEMP "rp-pinmame-$([guid]::NewGuid())"
    try {
        Expand-RpArchive -ZipPath $zip1 -Dest $tmp1 -Strip
        Get-ChildItem -Path $tmp1 -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($tmp1.Length).TrimStart('\','/')
            $target = Join-Path $PinMameInstallDir $rel
            $tdir = Split-Path -Parent $target
            if (-not (Test-Path $tdir)) { New-Item -ItemType Directory -Path $tdir | Out-Null }
            Move-Item -Path $_.FullName -Destination $target -Force
        }
    } finally {
        if (Test-Path $tmp1) { Remove-Item -Recurse -Force $tmp1 }
    }

    # Extract libpinmame archive (separate asset).
    $zip2 = Get-CachedDownload -Url $LibPinMameUrl -FileName $LibPinMameFileName -ExpectedSha256 $ExpectedLibPinMameSha256 -Force:$Force
    Write-Step "Extracting libpinmame"
    $tmp2 = Join-Path $env:TEMP "rp-libpinmame-$([guid]::NewGuid())"
    try {
        Expand-RpArchive -ZipPath $zip2 -Dest $tmp2 -Strip
        Get-ChildItem -Path $tmp2 -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($tmp2.Length).TrimStart('\','/')
            $target = Join-Path $PinMameInstallDir $rel
            $tdir = Split-Path -Parent $target
            if (-not (Test-Path $tdir)) { New-Item -ItemType Directory -Path $tdir | Out-Null }
            Copy-Item -Path $_.FullName -Destination $target -Force
        }
    } finally {
        if (Test-Path $tmp2) { Remove-Item -Recurse -Force $tmp2 }
    }

    # Asset naming is case-inconsistent across releases (PinMAME.exe vs pinmame.exe).
    # Normalise on the lowercase canonical name by relying on Test-Path being case-insensitive.
    if (-not (Test-Path $pinmameExe)) {
        $found = Get-ChildItem -Path $PinMameInstallDir -Recurse -Filter 'pinmame*.exe' -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -match '^pinmame(d)?\.exe$' } | Select-Object -First 1
        if (-not $found) {
            throw "PinMAME extraction did not produce pinmame.exe under $PinMameInstallDir."
        }
        Write-Warn2 "PinMAME entry point at $($found.FullName); leaving in place."
    }
    $dllFound = Find-LibPinMameDll
    if (-not $dllFound) {
        throw "libpinmame*.dll missing under $PinMameInstallDir after extracting $LibPinMameFileName."
    }
    # Create a canonical libpinmame.dll copy if the asset shipped versioned.
    if ($dllFound.Name -ne 'libpinmame.dll') {
        Copy-Item -Path $dllFound.FullName -Destination $libPinmameDll -Force
        Write-Ok "libpinmame at $($dllFound.FullName); copied to libpinmame.dll for fixed-name loaders."
    }
    Write-Ok "PinMAME standalone + libpinmame installed."
}
Set-RpEnvVar -Name 'PINMAME_DIR' -Value $PinMameInstallDir

# --- Step 4: VPinMAME COM (regsvr32) -----------------------------------------

Write-Step "VPinMAME COM at $VpmInstallDir"
$vpmDll64 = Join-Path $VpmInstallDir 'VPinMAME64.dll'
$vpmDll32 = Join-Path $VpmInstallDir 'VPinMAME.dll'
$vpmDll = if (Test-Path $vpmDll64) { $vpmDll64 } else { $vpmDll32 }

$vpmExtracted = (Test-Path $vpmDll64) -or (Test-Path $vpmDll32)
if ($vpmExtracted -and -not $Force) {
    Write-Ok "VPinMAME DLL present; skipping extraction."
}
else {
    $zip = Get-CachedDownload -Url $VpmComUrl -FileName $VpmComFileName -ExpectedSha256 $ExpectedVpmComSha256 -Force:$Force
    Write-Step "Extracting VPinMAME COM"
    Expand-RpArchive -ZipPath $zip -Dest $VpmInstallDir -Strip
    if (-not ((Test-Path $vpmDll64) -or (Test-Path $vpmDll32))) {
        throw "VPinMAME extraction did not produce VPinMAME(64).dll under $VpmInstallDir."
    }
    Write-Ok "VPinMAME extracted."
    $vpmDll = if (Test-Path $vpmDll64) { $vpmDll64 } else { $vpmDll32 }
}
Set-RpEnvVar -Name 'VPINMAME_DIR' -Value $VpmInstallDir

# --- Step 5: Deploy patched VPinMAME64.dll (adds the VPINMAME_SWITCHLOG -----
#             switch-edge recorder used by record.ps1) -----

Write-Step "Deploying patched VPinMAME64.dll"
$patchedDll = Join-Path $PSScriptRoot 'bin\VPinMAME64.dll'
if (-not (Test-Path $patchedDll)) {
    Write-Warn2 "bin\VPinMAME64.dll not found in skill directory; skipping patch."
    Write-Warn2 "Record mode VpRecord will not be available until the DLL is present."
}
elseif (-not (Test-Path $vpmDll64)) {
    Write-Warn2 "VPinMAME64.dll not installed at $vpmDll64; cannot deploy patch."
}
else {
    # Back up original only if backup doesn't already exist
    $backupDll = $vpmDll64 + '.orig'
    if (-not (Test-Path $backupDll)) {
        Copy-Item $vpmDll64 $backupDll -Force
        Write-Ok "Backed up original to $(Split-Path -Leaf $backupDll)"
    }
    Copy-Item $patchedDll $vpmDll64 -Force
    Write-Ok "Patched VPinMAME64.dll deployed to $vpmDll64"
}

# Deploy patched pinmame64.dll (libpinmame) for replay_host.py
$patchedLib = Join-Path $PSScriptRoot 'bin\pinmame64.dll'
if (Test-Path $patchedLib) {
    Write-Step "Deploying patched pinmame64.dll (libpinmame) to $PinMameInstallDir"
    $dllFound = Find-LibPinMameDll
    if ($dllFound) {
        $backupLib = $dllFound.FullName + '.orig'
        if (-not (Test-Path $backupLib)) {
            Copy-Item $dllFound.FullName $backupLib -Force
            Write-Ok "Backed up original $($dllFound.Name)"
        }
        Copy-Item $patchedLib $dllFound.FullName -Force
        # Also refresh the canonical alias
        Copy-Item $patchedLib (Join-Path $PinMameInstallDir 'libpinmame.dll') -Force
        Write-Ok "Patched libpinmame deployed."
    } else {
        Write-Warn2 "libpinmame*.dll not found in $PinMameInstallDir; skipping (run step 3 first)."
    }
}

# COM registration check (HKCR\VPinMAME.Controller exists).
$regOk = $false
try {
    $regOk = [bool](Get-Item 'Registry::HKEY_CLASSES_ROOT\VPinMAME.Controller' -ErrorAction Stop)
} catch { $regOk = $false }

if ($regOk -and -not $Force) {
    Write-Ok "VPinMAME.Controller already COM-registered."
}
else {
    Write-Step "Registering VPinMAME.Controller via regsvr32"
    if (-not (Test-IsElevated)) {
        Write-Warn2 "regsvr32 needs Administrator. Skipping registration."
        Write-Warn2 "Run this once from an elevated PowerShell:"
        Write-Host  "    Start-Process regsvr32 -Verb RunAs -ArgumentList '`"$vpmDll`"'" -ForegroundColor Yellow
        Write-Warn2 "Then re-run setup.ps1 to verify."
    }
    else {
        $rsExe = "$env:windir\system32\regsvr32.exe"
        $proc = Start-Process -FilePath $rsExe -ArgumentList @('/s', $vpmDll) -PassThru -Wait
        if ($proc.ExitCode -ne 0) {
            throw "regsvr32 failed (exit $($proc.ExitCode)) on $vpmDll."
        }
        try {
            $regOk = [bool](Get-Item 'Registry::HKEY_CLASSES_ROOT\VPinMAME.Controller' -ErrorAction Stop)
        } catch { $regOk = $false }
        if (-not $regOk) {
            throw "regsvr32 reported success but HKCR\VPinMAME.Controller is missing."
        }
        Write-Ok "VPinMAME.Controller registered."
    }
}

# --- Summary -----------------------------------------------------------------

Write-Host ""
Write-Host "Toolchain setup complete." -ForegroundColor Green
Write-Host "  VPINBALL_DIR  = $VpxInstallDir"
Write-Host "  PINMAME_DIR   = $PinMameInstallDir"
Write-Host "  VPINMAME_DIR  = $VpmInstallDir"
Write-Host "  uv            = $((Get-Command uv -ErrorAction SilentlyContinue).Source)"
Write-Host ""
Write-Host "Next: register a ROM + VPX table with add-rom.ps1, e.g." -ForegroundColor Yellow
Write-Host "  add-rom.ps1 -RomZip '.\congo_21.zip' [-Table '<path-to-vpx>']" -ForegroundColor Yellow
