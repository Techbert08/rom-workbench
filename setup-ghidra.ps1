#requires -Version 7.0
<#
.SYNOPSIS
    Sets up Ghidra 12.0.4 and the WPC loader extension for analyzing Williams pinball ROMs.

.DESCRIPTION
    Idempotent installer. Steps:
      1. Verify Java 21+ and git are on PATH.
      2. Download and unpack Ghidra 12.0.4 to %LOCALAPPDATA%\Programs.
      3. Clone c0rner/ghidra_wpc_loader, build it with the bundled gradle wrapper,
         and install the resulting extension into Ghidra so it's available headless.

    Re-running with no changes does nothing. Pass -Force to re-download/re-extract/re-build.

.PARAMETER Force
    Re-download and re-extract Ghidra even if already present.

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

# --- Constants ----------------------------------------------------------------

$GhidraVersion   = '12.0.4'
$GhidraBuildDate = '20260303'
$GhidraDirName   = "ghidra_${GhidraVersion}_PUBLIC"
$GhidraZipName   = "ghidra_${GhidraVersion}_PUBLIC_${GhidraBuildDate}.zip"
$GhidraUrl       = "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${GhidraVersion}_build/${GhidraZipName}"
$GhidraSha256    = 'c3b458661d69e26e203d739c0c82d143cc8a4a29d9e571f099c2cf4bda62a120'
$MinJavaMajor    = 21

$WpcRepoUrl      = 'https://github.com/c0rner/ghidra_wpc_loader.git'
$WpcExtName      = 'ghidra_wpc_loader'    # matches top-level dir in the built extension zip

# --- Helpers ------------------------------------------------------------------

function Write-Step  ([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    ([string] $msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2 ([string] $msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Get-JavaMajorVersion {
    $java = Get-Command java -ErrorAction SilentlyContinue
    if (-not $java) { return $null }

    # `java -version` prints to stderr. Capture both streams.
    $output = & java -version 2>&1 | Out-String
    if ($output -match '(?:version ")(\d+)') {
        return [int]$Matches[1]
    }
    return $null
}

function Test-FileSha256 {
    param([string] $Path, [string] $Expected)
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash
    return $actual.Equals($Expected, [System.StringComparison]::OrdinalIgnoreCase)
}

# --- Step 1: Java -------------------------------------------------------------

Write-Step "Checking Java"
$javaMajor = Get-JavaMajorVersion
if (-not $javaMajor) {
    throw "java not found on PATH. Install a JDK $MinJavaMajor or newer (Eclipse Temurin recommended: https://adoptium.net/) and re-run."
}
if ($javaMajor -lt $MinJavaMajor) {
    throw "Java $javaMajor found, but Ghidra $GhidraVersion needs JDK $MinJavaMajor+. Install a newer JDK and re-run."
}
Write-Ok "Java $javaMajor OK ($((Get-Command java).Source))"

Write-Step "Checking git"
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "git not found on PATH. Install Git for Windows (https://git-scm.com/download/win) and re-run."
}
Write-Ok "git OK ($($git.Source))"

# --- Step 2: Ghidra -----------------------------------------------------------

$GhidraInstallDir = Join-Path $InstallRoot $GhidraDirName

Write-Step "Checking Ghidra $GhidraVersion at $GhidraInstallDir"
$analyzeHeadless = Join-Path $GhidraInstallDir 'support\analyzeHeadless.bat'
$alreadyInstalled = (Test-Path $analyzeHeadless) -and (-not $Force)

if ($alreadyInstalled) {
    Write-Ok "Ghidra already installed (analyzeHeadless.bat found)."
}
else {
    if (Test-Path $GhidraInstallDir) {
        Write-Warn2 "Existing directory found at $GhidraInstallDir but analyzeHeadless.bat is missing. Removing."
        Remove-Item -Recurse -Force $GhidraInstallDir
    }

    if (-not (Test-Path $InstallRoot)) {
        New-Item -ItemType Directory -Path $InstallRoot | Out-Null
    }

    $cacheDir = Join-Path $env:LOCALAPPDATA 'analyze-wpc-rom\cache'
    if (-not (Test-Path $cacheDir)) {
        New-Item -ItemType Directory -Path $cacheDir | Out-Null
    }
    $zipPath = Join-Path $cacheDir $GhidraZipName

    $needsDownload = $true
    if ((Test-Path $zipPath) -and -not $Force) {
        Write-Step "Verifying cached download $zipPath"
        if (Test-FileSha256 -Path $zipPath -Expected $GhidraSha256) {
            Write-Ok "Cached zip checksum matches; skipping download."
            $needsDownload = $false
        }
        else {
            Write-Warn2 "Cached zip checksum mismatch; re-downloading."
            Remove-Item $zipPath -Force
        }
    }

    if ($needsDownload) {
        Write-Step "Downloading $GhidraUrl"
        Write-Warn2 "This is ~400 MB; may take a few minutes."
        # Invoke-WebRequest is fine for one large file; -UseBasicParsing avoids IE engine.
        Invoke-WebRequest -Uri $GhidraUrl -OutFile $zipPath -UseBasicParsing
        Write-Ok "Downloaded to $zipPath"

        Write-Step "Verifying SHA-256"
        if (-not (Test-FileSha256 -Path $zipPath -Expected $GhidraSha256)) {
            throw "SHA-256 mismatch for $zipPath. Expected $GhidraSha256."
        }
        Write-Ok "Checksum OK."
    }

    Write-Step "Extracting to $InstallRoot"
    Expand-Archive -Path $zipPath -DestinationPath $InstallRoot -Force
    if (-not (Test-Path $analyzeHeadless)) {
        throw "Extraction completed but $analyzeHeadless is missing. Aborting."
    }
    Write-Ok "Ghidra extracted."
}

# --- Step 3: Persist GHIDRA_INSTALL_DIR env var -------------------------------

Write-Step "Setting user env var GHIDRA_INSTALL_DIR"
$currentEnv = [Environment]::GetEnvironmentVariable('GHIDRA_INSTALL_DIR', 'User')
if ($currentEnv -ne $GhidraInstallDir) {
    [Environment]::SetEnvironmentVariable('GHIDRA_INSTALL_DIR', $GhidraInstallDir, 'User')
    Write-Ok "GHIDRA_INSTALL_DIR set (user). New shells will see it; current shell updated below."
}
else {
    Write-Ok "GHIDRA_INSTALL_DIR already set."
}
$env:GHIDRA_INSTALL_DIR = $GhidraInstallDir

# --- Step 4: WPC loader extension --------------------------------------------

$WpcInstallDir = Join-Path $GhidraInstallDir "Ghidra\Extensions\$WpcExtName"
Write-Step "Checking WPC loader extension at $WpcInstallDir"

$wpcAlreadyInstalled = (Test-Path (Join-Path $WpcInstallDir 'Module.manifest')) -and -not $Force
if ($wpcAlreadyInstalled) {
    Write-Ok "WPC loader already installed."
}
else {
    # Clone (or update) source into the cache dir.
    $srcRoot = Join-Path $env:LOCALAPPDATA 'analyze-wpc-rom\src'
    if (-not (Test-Path $srcRoot)) {
        New-Item -ItemType Directory -Path $srcRoot | Out-Null
    }
    $wpcSrc = Join-Path $srcRoot 'ghidra_wpc_loader'

    if (Test-Path (Join-Path $wpcSrc '.git')) {
        Write-Step "Updating existing clone $wpcSrc"
        Push-Location $wpcSrc
        try {
            & git fetch --quiet origin
            if ($LASTEXITCODE -ne 0) { throw "git fetch failed." }
            & git reset --hard origin/HEAD --quiet
            if ($LASTEXITCODE -ne 0) { throw "git reset failed." }
        }
        finally { Pop-Location }
    }
    else {
        if (Test-Path $wpcSrc) { Remove-Item -Recurse -Force $wpcSrc }
        Write-Step "Cloning $WpcRepoUrl"
        & git clone --quiet $WpcRepoUrl $wpcSrc
        if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
    }
    Write-Ok "Source ready at $wpcSrc"

    # Clean previous build output so we can deterministically pick up the new zip.
    $distDir = Join-Path $wpcSrc 'dist'
    if (Test-Path $distDir) { Remove-Item -Recurse -Force $distDir }

    Write-Step "Building extension with bundled gradle wrapper"
    Write-Warn2 "First build downloads gradle 9.3.1 (~150 MB); subsequent builds are fast."
    $gradlew = Join-Path $GhidraInstallDir 'support\gradle\gradlew.bat'
    if (-not (Test-Path $gradlew)) { throw "Gradle wrapper not found at $gradlew." }

    Push-Location $wpcSrc
    try {
        # GHIDRA_INSTALL_DIR is consumed by the extension's build.gradle.
        & $gradlew --no-daemon
        if ($LASTEXITCODE -ne 0) { throw "gradlew build failed (exit $LASTEXITCODE)." }
    }
    finally { Pop-Location }

    if (-not (Test-Path $distDir)) {
        throw "Build completed but $distDir is missing."
    }
    $builtZip = Get-ChildItem -Path $distDir -Filter "*${WpcExtName}*.zip" | Select-Object -First 1
    if (-not $builtZip) {
        throw "No $WpcExtName zip produced in $distDir."
    }
    Write-Ok "Built $($builtZip.Name)"

    # Install into <ghidra>\Ghidra\Extensions\ so headless picks it up automatically.
    if (Test-Path $WpcInstallDir) { Remove-Item -Recurse -Force $WpcInstallDir }
    $extensionsParent = Join-Path $GhidraInstallDir 'Ghidra\Extensions'
    Write-Step "Installing extension to $extensionsParent"
    Expand-Archive -Path $builtZip.FullName -DestinationPath $extensionsParent -Force

    if (-not (Test-Path (Join-Path $WpcInstallDir 'Module.manifest'))) {
        # The zip may have a top-level dir name that doesn't match $WpcExtName.
        # Detect what was actually extracted and report it.
        $candidates = Get-ChildItem -Path $extensionsParent -Directory -ErrorAction SilentlyContinue |
                      Where-Object { Test-Path (Join-Path $_.FullName 'Module.manifest') } |
                      Where-Object { $_.Name -like '*wpc*' -or $_.Name -like '*WPC*' }
        if ($candidates) {
            throw "Extension extracted as '$($candidates[0].Name)' but expected '$WpcExtName'. Update `$WpcExtName in setup.ps1."
        }
        throw "Extension extraction did not produce $WpcInstallDir\Module.manifest."
    }
    Write-Ok "WPC loader installed."
}

# --- Summary ------------------------------------------------------------------

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  GHIDRA_INSTALL_DIR = $GhidraInstallDir"
Write-Host "  analyzeHeadless    = $analyzeHeadless"
Write-Host "  WPC loader         = $WpcInstallDir"
Write-Host ""
Write-Host "Next: run analyze.ps1 -RomZip <path> to analyze a ROM." -ForegroundColor Yellow
