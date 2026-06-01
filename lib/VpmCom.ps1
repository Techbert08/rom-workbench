# Thin wrapper over the VPinMAME.Controller COM object. Used by record.ps1.
# All COM access is funnelled through here so failure modes (not registered,
# wrong bitness, game not running) produce one clear message.

#requires -Version 7.0

function New-VpmController {
    [CmdletBinding()]
    param()
    try {
        return New-Object -ComObject 'VPinMAME.Controller'
    } catch {
        $hint = @"
VPinMAME.Controller COM object could not be instantiated. Common causes:

  * VPinMAME is not registered. Run setup.ps1 elevated (it calls regsvr32 on VPinMAME.dll).
  * 32-bit / 64-bit mismatch. PowerShell 7 is 64-bit; VPinMAME's 64-bit DLL must be the one registered.
  * The 64-bit DLL is named VPinMAME64.dll in some distributions — the COM ProgID is still 'VPinMAME.Controller'.

Underlying error: $($_.Exception.Message)
"@
        throw $hint
    }
}

# Attach to a running VPinMAME instance (one that VP has already started via the .vpx script).
# Polls for the Running state because GetActiveObject only sees ROT-registered instances.
function Wait-VpmRunning {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Controller,
        [int] $TimeoutSeconds = 60,
        [string] $ExpectedRom
    )
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            if ($Controller.Running) {
                if ($ExpectedRom -and ($Controller.GameName -ne $ExpectedRom)) {
                    Write-Warning "VPM is running '$($Controller.GameName)' but expected '$ExpectedRom'."
                }
                return
            }
        } catch {
            # Controller may briefly be in a transient state; ignore and retry.
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out waiting for VPinMAME to enter Running state (${TimeoutSeconds}s)."
}

# Read the full switch state for a known switch list. Returns hashtable @{ n = $on }.
function Get-VpmSwitchMatrix {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Controller,
        [Parameter(Mandatory)][int[]] $Switches
    )
    $out = @{}
    foreach ($n in $Switches) {
        try {
            $out[$n] = [bool]$Controller.Switch($n)
        } catch {
            $out[$n] = $false
        }
    }
    return $out
}

# Scan the full 1..256 range, returning every switch that responds without error.
# Used by record.ps1 the first time it sees an unknown ROM.
function Find-VpmResponsiveSwitches {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Controller)
    $found = New-Object System.Collections.Generic.List[int]
    for ($n = 1; $n -le 256; $n++) {
        try {
            $null = $Controller.Switch($n)
            $found.Add($n)
        } catch { }
    }
    return $found.ToArray()
}

# Best-effort version string. VPinMAME exposes Version via the COM Controller.
function Get-VpmVersion {
    param([Parameter(Mandatory)] $Controller)
    try   { return [string]$Controller.Version }
    catch { return $null }
}
