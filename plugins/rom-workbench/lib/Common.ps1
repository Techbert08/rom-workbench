# Shared helpers for record-pinball. Dot-sourced by add-rom.ps1.
# Write-Step / Write-Ok / Write-Warn2 / Write-Bad, SHA-256, user-env reads, and
# the config.json read/write under %LOCALAPPDATA%\record-pinball.
# (The cross-platform installer setup-pinball.py owns downloads + env writes.)

#requires -Version 7.0

if ($MyInvocation.InvocationName -eq '.') {
    # dot-sourced; OK
}

function Write-Step  ([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    ([string] $msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2 ([string] $msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Bad   ([string] $msg) { Write-Host "    $msg" -ForegroundColor Red }

function Get-RpRoot { Join-Path $env:LOCALAPPDATA 'record-pinball' }

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

function Get-FileSha256 {
    param([Parameter(Mandatory)][string] $Path)
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

# Read a user/process-scope env var (set by setup-pinball.py).
function Get-RpEnvVar {
    param([Parameter(Mandatory)][string] $Name)
    $v = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($Name, 'User') }
    return $v
}
