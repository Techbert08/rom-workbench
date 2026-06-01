# Shared helpers for record-pinball. Dot-source from setup.ps1 / record.ps1 / replay.ps1.
# Mirrors analyze-wpc-rom\setup.ps1 conventions: Write-Step / Write-Ok / Write-Warn2,
# SHA-256 helpers, user-env helpers, and a cache root under %LOCALAPPDATA%\record-pinball.

#requires -Version 7.0

if ($MyInvocation.InvocationName -eq '.') {
    # dot-sourced; OK
}

function Write-Step  ([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    ([string] $msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2 ([string] $msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Bad   ([string] $msg) { Write-Host "    $msg" -ForegroundColor Red }

function Get-RpRoot { Join-Path $env:LOCALAPPDATA 'record-pinball' }

function Get-RpCacheDir {
    $d = Join-Path (Get-RpRoot) 'cache'
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
    return $d
}

function Get-RpConfigPath { Join-Path (Get-RpRoot) 'config.json' }

function Read-RpConfig {
    param([string] $Path = (Get-RpConfigPath))
    if (-not (Test-Path $Path)) { return [pscustomobject]@{} }
    try {
        return (Get-Content -Raw $Path | ConvertFrom-Json)
    } catch {
        Write-Warn2 "Could not parse $Path ($_); treating as empty."
        return [pscustomobject]@{}
    }
}

function Write-RpConfig {
    param([Parameter(Mandatory)] $Config, [string] $Path = (Get-RpConfigPath))
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $Config | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $Path
}

function Test-FileSha256 {
    param([Parameter(Mandatory)][string] $Path,
          [Parameter(Mandatory)][string] $Expected)
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash
    return $actual.Equals($Expected, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-FileSha256 {
    param([Parameter(Mandatory)][string] $Path)
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

# Download with cache + trust-on-first-use SHA-256.
#  - If $ExpectedSha256 is provided, downloaded file must match (errors otherwise).
#  - Else, if a sibling .sha256 file exists from a prior run, the file must still match it.
#  - Else, the hash is computed and persisted (first-use trust).
# Returns the absolute cached path.
function Get-CachedDownload {
    param(
        [Parameter(Mandatory)][string] $Url,
        [Parameter(Mandatory)][string] $FileName,
        [string] $ExpectedSha256,
        [switch] $Force
    )
    $cacheDir = Get-RpCacheDir
    $dest     = Join-Path $cacheDir $FileName
    $hashSidecar = "$dest.sha256"

    if ($Force -and (Test-Path $dest)) { Remove-Item $dest -Force }

    if (Test-Path $dest) {
        if ($ExpectedSha256) {
            Write-Step "Verifying cached $FileName"
            if (Test-FileSha256 -Path $dest -Expected $ExpectedSha256) {
                Write-Ok "Checksum OK (cached)."
                return $dest
            }
            Write-Warn2 "Cached checksum mismatch; re-downloading."
            Remove-Item $dest -Force
        }
        elseif (Test-Path $hashSidecar) {
            $expected = (Get-Content -Raw $hashSidecar).Trim()
            if (Test-FileSha256 -Path $dest -Expected $expected) {
                Write-Ok "Cached $FileName matches recorded hash $($expected.Substring(0,12))…"
                return $dest
            }
            Write-Warn2 "Cached $FileName diverged from recorded hash; re-downloading."
            Remove-Item $dest -Force
        }
        else {
            Write-Ok "Cached $FileName present (no hash on file; recording on this run)."
            $sha = Get-FileSha256 -Path $dest
            $sha | Set-Content -Encoding ascii $hashSidecar
            return $dest
        }
    }

    Write-Step "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $dest -UseBasicParsing
    Write-Ok "Downloaded to $dest"

    if ($ExpectedSha256) {
        if (-not (Test-FileSha256 -Path $dest -Expected $ExpectedSha256)) {
            $actual = Get-FileSha256 -Path $dest
            throw "SHA-256 mismatch for $dest. Expected $ExpectedSha256, got $actual."
        }
        Write-Ok "Checksum OK."
        $ExpectedSha256.ToLowerInvariant() | Set-Content -Encoding ascii $hashSidecar
    }
    else {
        $sha = Get-FileSha256 -Path $dest
        $sha | Set-Content -Encoding ascii $hashSidecar
        Write-Warn2 "No SHA-256 pinned for $FileName. Trust-on-first-use hash recorded: $sha"
        Write-Warn2 "To pin: paste this hash into setup.ps1's `$Expected* constant for $FileName."
    }
    return $dest
}

# Set a user-scope env var idempotently and mirror into the current process.
function Set-RpEnvVar {
    param([Parameter(Mandatory)][string] $Name,
          [Parameter(Mandatory)][string] $Value)
    $current = [Environment]::GetEnvironmentVariable($Name, 'User')
    if ($current -ne $Value) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
        Write-Ok "$Name set (user). Current shell mirrored."
    }
    else {
        Write-Ok "$Name already set."
    }
    Set-Item -Path "Env:$Name" -Value $Value
}

function Get-RpEnvVar {
    param([Parameter(Mandatory)][string] $Name)
    $v = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($Name, 'User') }
    return $v
}

function Test-IsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Extract a zip into a target directory; if -Strip is set and the zip has exactly one
# top-level directory, its contents are moved up one level so $Dest ends up holding
# the *contents* (matches the analyze-wpc-rom convention).
function Expand-RpArchive {
    param([Parameter(Mandatory)][string] $ZipPath,
          [Parameter(Mandatory)][string] $Dest,
          [switch] $Strip)
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    New-Item -ItemType Directory -Path $Dest | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $Dest -Force
    if ($Strip) {
        $top = Get-ChildItem -Path $Dest -Force
        if ($top.Count -eq 1 -and $top[0].PSIsContainer) {
            $inner = $top[0].FullName
            Get-ChildItem -Path $inner -Force | ForEach-Object {
                Move-Item -Path $_.FullName -Destination $Dest -Force
            }
            Remove-Item $inner -Recurse -Force
        }
    }
}
