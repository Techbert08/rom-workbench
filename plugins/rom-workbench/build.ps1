#requires -Version 7.0
<#
.SYNOPSIS
    Apply JSON patch specs to a WPC ROM zip and produce a patched ROM zip.

.DESCRIPTION
    Reads every *.json file in -PatchDir (sorted by name, so prefix numbers control
    order), validates the expected bytes at each target location, writes the new bytes,
    then updates the WPC checksum so the ROM passes startup verification.

    Patch spec format (one patch per .json file):

        {
          "name":    "human-readable description",
          "address": "$FFEE",          // system ROM: $8000-$FFFF, no page needed
                                       // OR: "$4C0E@p37"  banked page
                                       // OR: "0x7FFEE"    raw file offset
          "old_hex": "21 21",          // expected bytes at that location (safety check)
          "new_hex": "21 29"           // replacement bytes
        }

    Exactly one of "address" or "offset" is required. "offset" is a raw file offset
    (decimal or 0x-prefixed hex string).

    After applying all patches, the script recomputes the WPC 16-bit checksum and
    writes the corrected delta word at $FFEC. Pass -DisableChecksum to instead write
    delta=0x00FF, which tells the WPC OS to skip verification entirely (useful during
    development when the checksum formula isn't yet confirmed).

.PARAMETER RomZip
    Source ROM zip. Default: .\orig\congo_21.zip (or the first zip found in .\orig\).

.PARAMETER PatchDir
    Directory containing *.json patch specs. Default: .\source\patches.

.PARAMETER OutZip
    Output zip path. Default: .\dist\<rom-stem>_modded.zip.

.PARAMETER DisableChecksum
    Write delta=0x00FF instead of recomputing the real checksum. Safe for development.

.PARAMETER Deploy
    After building, copy the output zip to %VPINMAME_DIR%\roms\ so it can be tested
    immediately with record-pinball or VPinMAME.

.PARAMETER Force
    Overwrite output zip if it already exists.

.EXAMPLE
    # Dry-run: build with checksum disabled, no deploy
    & '.claude\skills\build-wpc-rom\build.ps1' -DisableChecksum

.EXAMPLE
    # Full build with real checksum, deploy to VPinMAME
    & '.claude\skills\build-wpc-rom\build.ps1' -Deploy
#>
[CmdletBinding()]
param(
    [string] $RomZip,
    [string] $PatchDir   = '.\source\patches',
    [string] $OutZip,
    [switch] $DisableChecksum,
    [switch] $Deploy,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ── Helpers ───────────────────────────────────────────────────────────────────

function Write-Step  ([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    ([string]$msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2 ([string]$msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Bad   ([string]$msg) { Write-Host "    $msg" -ForegroundColor Red; throw $msg }

# WPC ROM size → first page number (matches WPCLoader.java)
$SIZE_TO_FIRST_PAGE = @{
    131072  = 0x38   # 128 KiB
    262144  = 0x34   # 256 KiB
    524288  = 0x20   # 512 KiB
    1048576 = 0x00   # 1 MiB
}
$BANK_SIZE = 0x4000
$SYS_SIZE  = 0x8000

function Get-RomFirstPage([byte[]]$Rom) {
    $sz = $Rom.Length
    if (-not $SIZE_TO_FIRST_PAGE.ContainsKey($sz)) {
        throw "Unsupported ROM size $sz bytes (expected 128K/256K/512K/1M)."
    }
    return $SIZE_TO_FIRST_PAGE[$sz]
}

# Convert address spec → file offset.
# Supports: "$NNNN" (sys ROM), "$NNNN@pXX" (banked), "0xNNNNN" / plain integer (file offset).
function Resolve-Address([byte[]]$Rom, [string]$Spec) {
    $Spec = $Spec.Trim()

    # Raw file offset: 0xNNNNN or plain number
    if ($Spec -match '^0x[0-9a-fA-F]+$') { return [int]("0x" + $Spec.Substring(2)) }
    if ($Spec -match '^\d+$')             { return [int]$Spec }

    # Banked: $NNNN@pXX
    if ($Spec -match '^\$?([0-9a-fA-F]{4})@p([0-9a-fA-F]{2})$') {
        $addr      = [int]("0x$($Matches[1])")
        $page      = [int]("0x$($Matches[2])")
        $firstPage = Get-RomFirstPage $Rom
        if ($page -lt $firstPage -or $page -gt 0x3D) {
            throw "Page 0x$($page.ToString('X2')) out of range (first=0x$($firstPage.ToString('X2')))."
        }
        return ($page - $firstPage) * $BANK_SIZE + ($addr - $BANK_SIZE)
    }

    # System ROM: $NNNN
    if ($Spec -match '^\$([0-9a-fA-F]{4})$') {
        $addr = [int]("0x$($Matches[1])")
        if ($addr -lt 0x8000) { throw "\$$($Matches[1]) is below `$8000; use `$NNNN@pXX for banked pages." }
        return ($Rom.Length - $SYS_SIZE) + ($addr - 0x8000)
    }

    throw "Cannot parse address spec '$Spec'. Use `$NNNN, `$NNNN@pXX, or 0xNNNNN."
}

function Format-HexBytes([byte[]]$B) { ($B | ForEach-Object { '{0:X2}' -f $_ }) -join ' ' }

function Parse-HexBytes([string]$Hex) {
    # Leading comma keeps a 1-element array from being unwrapped to a scalar
    # by PowerShell's pipeline output semantics — otherwise $bytes.Length blows up.
    $bytes = [byte[]]@($Hex.Trim() -split '\s+' | ForEach-Object { [byte][int]("0x$_") })
    return ,$bytes
}

# ── WPC checksum ──────────────────────────────────────────────────────────────
#
# WPC startup walks the ROM byte-by-byte adding into a 16-bit wrapping
# accumulator, EXCEPT the two bytes at $FFEC/$FFED are read as a big-endian
# 16-bit "delta" word and that single value is added (in place of summing the
# two bytes individually). The total must equal the word stored at $FFEE.
#
# So if S = sum-of-all-bytes-with-FFEC-FFED-zeroed and C = the word at $FFEE,
# the delta we need to write is just (C - S) mod 65536. No 510 byte-sum cap;
# any single 16-bit value works, so any patch can be balanced by tuning delta.
#
function Update-WpcChecksum([byte[]]$Rom) {
    # WPC rule: the verifier sums byte-by-byte EXCEPT it reads $FFEC as a 16-bit
    # big-endian word and adds *that value* to the sum (not the two bytes individually).
    # The total must equal the word stored at $FFEE.
    #
    # That means the delta can be any value in [0, 65535] — there is no
    # "byte-sum cap" — so any patch can be balanced by a single 16-bit delta.
    # Empirically verified 2026-05-28 by building a ROM with byte-sum != target
    # but S_excl_delta + (delta as word) == target and observing the WPC OS
    # accept it.
    $sysBase  = $Rom.Length - $SYS_SIZE
    $deltaOff = $sysBase + (0xFFEC - 0x8000)
    $cksumOff = $sysBase + (0xFFEE - 0x8000)

    $target = (([int]$Rom[$cksumOff] -shl 8) -bor $Rom[$cksumOff + 1])

    # Sum-of-all-bytes with delta bytes treated as zero.
    $Rom[$deltaOff] = 0; $Rom[$deltaOff + 1] = 0
    $sExclDelta = 0
    foreach ($b in $Rom) { $sExclDelta = ($sExclDelta + $b) -band 0xFFFF }

    # Delta as a 16-bit big-endian word; no overflow possible after the mod.
    $needed = ($target - $sExclDelta + 0x10000) -band 0xFFFF
    $dH = ($needed -shr 8) -band 0xFF
    $dL = $needed -band 0xFF
    $Rom[$deltaOff]     = [byte]$dH
    $Rom[$deltaOff + 1] = [byte]$dL

    # Verify under the actual model: byte-sum minus the two delta bytes, plus delta as a word.
    $verify = 0
    foreach ($b in $Rom) { $verify = ($verify + $b) -band 0xFFFF }
    $verify = ($verify - $Rom[$deltaOff] - $Rom[$deltaOff + 1] + (([int]$Rom[$deltaOff] -shl 8) -bor $Rom[$deltaOff + 1])) -band 0xFFFF
    if ($verify -ne $target) {
        throw "Checksum verify failed: got 0x$($verify.ToString('X4')), want 0x$($target.ToString('X4'))."
    }
    Write-Ok ("Checksum: delta=0x{0:X2}{1:X2}  target=0x{2:X4}  [OK]" -f $dH, $dL, $target)
}

function Disable-WpcChecksum([byte[]]$Rom) {
    $sysBase  = $Rom.Length - $SYS_SIZE
    $deltaOff = $sysBase + (0xFFEC - 0x8000)
    $Rom[$deltaOff]     = 0x00
    $Rom[$deltaOff + 1] = 0xFF
    Write-Warn2 "Checksum disabled (delta=0x00FF). ROM will pass startup without checksum verification."
}

# ── Resolve defaults ──────────────────────────────────────────────────────────

if (-not $RomZip) {
    $candidate = Get-ChildItem '.\orig\' -Filter '*.zip' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
    if (-not $candidate) { Write-Bad "No ROM zip found in .\orig\. Pass -RomZip <path>." }
    $RomZip = $candidate.FullName
}
$RomZip = (Resolve-Path $RomZip).Path
$romStem = [System.IO.Path]::GetFileNameWithoutExtension($RomZip)

if (-not $OutZip) {
    $distDir = '.\dist'
    if (-not (Test-Path $distDir)) { New-Item -ItemType Directory -Path $distDir | Out-Null }
    $OutZip = Join-Path $distDir "${romStem}_modded.zip"
}

if ((Test-Path $OutZip) -and -not $Force) {
    Write-Bad "Output zip already exists: $OutZip. Pass -Force to overwrite."
}

# ── Load game ROM from zip ────────────────────────────────────────────────────

Write-Step "Loading ROM from $RomZip"

$zipArchive = [System.IO.Compression.ZipFile]::OpenRead($RomZip)
$allEntries = @($zipArchive.Entries)

function Get-GameRomEntry([System.IO.Compression.ZipArchiveEntry[]]$Entries) {
    $validSizes = @(131072, 262144, 524288, 1048576)
    function Score($e) {
        $n = $e.Name.ToLower(); $s = 0
        if ($n -match '_g')                      { $s += 10 }
        if ($n -match '^[a-z]{2,4}s\d')         { $s -= 10 }
        if ($n -match 'sound|snd|dcs')           { $s -= 5  }
        return $s
    }
    $Entries | Where-Object { $validSizes -contains $_.Length } |
               Sort-Object  { -1 * (Score $_) } |
               Select-Object -First 1
}

$gameEntry = Get-GameRomEntry $allEntries
if (-not $gameEntry) { $zipArchive.Dispose(); Write-Bad "No valid game ROM found in $RomZip." }

$stream  = $gameEntry.Open()
$romBytes = [byte[]]::new($gameEntry.Length)
$read = 0
while ($read -lt $romBytes.Length) {
    $n = $stream.Read($romBytes, $read, $romBytes.Length - $read)
    if ($n -eq 0) { break }
    $read += $n
}
$stream.Dispose()

Write-Ok "Game ROM: $($gameEntry.Name)  $($romBytes.Length / 1024) KiB"

# Validate page layout
$_ = Get-RomFirstPage $romBytes

# ── Apply patches ─────────────────────────────────────────────────────────────

Write-Step "Applying patches from $PatchDir"

$patchFiles = @(Get-ChildItem -Path $PatchDir -Filter '*.json' -ErrorAction SilentlyContinue |
                Sort-Object Name)

if ($patchFiles.Count -eq 0) {
    Write-Warn2 "No *.json patch files found in $PatchDir. Output ROM will be identical to source (checksum/disable only)."
} else {
    Write-Ok "$($patchFiles.Count) patch file(s) found."
}

foreach ($pf in $patchFiles) {
    $spec = Get-Content -Raw $pf.FullName | ConvertFrom-Json

    # Resolve offset
    if     ($spec.PSObject.Properties['address']) { $offsetSpec = $spec.address }
    elseif ($spec.PSObject.Properties['offset'])  { $offsetSpec = $spec.offset  }
    else   { throw "Patch '$($pf.Name)' has neither 'address' nor 'offset' field." }

    $offset = Resolve-Address $romBytes $offsetSpec.ToString()

    $newBytes = Parse-HexBytes $spec.new_hex
    $numBytes = $newBytes.Length

    # Validate old bytes if specified
    if ($spec.PSObject.Properties['old_hex'] -and $spec.old_hex) {
        $expected = Parse-HexBytes $spec.old_hex
        if ($expected.Length -ne $numBytes) {
            throw "Patch '$($pf.Name)': old_hex and new_hex have different lengths."
        }
        $actual = $romBytes[$offset..($offset + $numBytes - 1)]
        if (Compare-Object $expected $actual) {
            $expHex = Format-HexBytes $expected
            $actHex = Format-HexBytes $actual
            throw "Patch '$($pf.Name)': byte mismatch at offset 0x$($offset.ToString('X5')). Expected [$expHex] got [$actHex]."
        }
    }

    # Apply
    for ($i = 0; $i -lt $numBytes; $i++) { $romBytes[$offset + $i] = $newBytes[$i] }

    if ($spec.PSObject.Properties['name']) { $name = $spec.name } else { $name = $pf.BaseName }
    $oldHex = '???'; if ($spec.PSObject.Properties['old_hex']) { $oldHex = $spec.old_hex }
    Write-Ok ("  {0,-40} @ 0x{1:X5}  [{2}] -> [{3}]" -f $name, $offset, $oldHex, $spec.new_hex)
}

# ── Update checksum ───────────────────────────────────────────────────────────

Write-Step "Checksum"
if ($DisableChecksum) {
    Disable-WpcChecksum $romBytes
} else {
    Update-WpcChecksum $romBytes
}

# ── Repackage zip ─────────────────────────────────────────────────────────────

Write-Step "Packaging $OutZip"

if (Test-Path $OutZip) { Remove-Item $OutZip -Force }

$outStream = [System.IO.File]::Create($OutZip)
$newZip    = [System.IO.Compression.ZipArchive]::new($outStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)

# Copy all original entries, replacing the game ROM.
foreach ($entry in $allEntries) {
    $newEntry  = $newZip.CreateEntry($entry.Name, [System.IO.Compression.CompressionLevel]::Optimal)
    $outStream2 = $newEntry.Open()
    if ($entry.Name -eq $gameEntry.Name) {
        $outStream2.Write($romBytes, 0, $romBytes.Length)
    } else {
        $src = $entry.Open()
        $src.CopyTo($outStream2)
        $src.Dispose()
    }
    $outStream2.Dispose()
}

$newZip.Dispose()
$outStream.Dispose()
$zipArchive.Dispose()

Write-Ok "Written: $OutZip ($([math]::Round((Get-Item $OutZip).Length / 1KB, 0)) KB)"

# ── Optionally deploy ─────────────────────────────────────────────────────────

if ($Deploy) {
    Write-Step "Deploying to VPinMAME"
    $vpinmame = [System.Environment]::GetEnvironmentVariable('VPINMAME_DIR', 'User')
    if (-not $vpinmame) { Write-Warn2 "VPINMAME_DIR not set; skipping deploy. Run pinball-setup/setup-pinball.py first." }
    else {
        $dest = Join-Path $vpinmame "roms\${romStem}_modded.zip"
        Copy-Item -Path $OutZip -Destination $dest -Force
        Write-Ok "Copied to $dest"
        Write-Warn2 "To test: uv run record-pinball\record.py --rom ${romStem}_modded"
    }
}

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "  Source:  $RomZip"
Write-Host "  Patches: $($patchFiles.Count)"
Write-Host "  Output:  $OutZip"
Write-Host ""
$outZipStem = [System.IO.Path]::GetFileNameWithoutExtension($OutZip)
$outNvHint  = Join-Path (Split-Path -Parent $OutZip) "$outZipStem.nv"
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  NVRAM:    uv run .claude\skills\record-pinball\init_nvram.py --rom-zip $OutZip --force"
Write-Host "  Validate: uv run .claude\skills\record-pinball\replay.py --rom <name> --rom-zip $OutZip --session <session> --nvram $outNvHint --trace state,dmd"
