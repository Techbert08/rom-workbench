---
name: build-wpc-rom
description: Apply JSON patch specs to a WPC ROM zip and produce a patched, checksum-correct output zip ready for VPinMAME. Use when you have patch specs in source/patches/ and want to build dist/<rom>_modded.zip, or when you want to validate a patch by replaying a recorded session against it via record-pinball.
---

# build-wpc-rom

Applies ordered JSON patch specs to a WPC game ROM, recalculates the WPC 16-bit checksum, and repackages the ROM zip. The output drops into VPinMAME's `roms\` directory for immediate testing via `record-pinball`'s single-sided `replay.py`.

## When to invoke

- "build the modded ROM" / "apply the patches"
- "I've written a patch spec, build and test it"
- "deploy the patched ROM to VPinMAME"
- Verifying a mod by replaying a recorded session against the patched ROM

## Quickstart

```powershell
# Build with checksum disabled (safest during development)
& '.claude\skills\build-wpc-rom\build.ps1' -DisableChecksum

# Build with real checksum, deploy to VPinMAME
& '.claude\skills\build-wpc-rom\build.ps1' -Deploy

# Validate against a recorded session (single-sided replay against the modded ROM).
# First time the modded zip changes the WPC checksum word ($FFEE), produce a
# freshly-reset NVRAM snapshot so the replay doesn't pay the factory-reset cost.
uv run .\.claude\skills\record-pinball\init_nvram.py --rom-zip '.\dist\congo_21_modded.zip' --force
uv run .\.claude\skills\record-pinball\replay.py `
    --rom congo_21 --rom-zip '.\dist\congo_21_modded.zip' `
    --session '.\sessions\<UTC>' `
    --nvram '.\dist\congo_21_modded.nv' `
    --trace state,dmd
```

## Prerequisites

- **PowerShell 7+** — `build.ps1` uses `#requires -Version 7.0`.
- **ROM zip** in `.\orig\` (or pass `-RomZip <path>`). The build never modifies `orig/` — it reads the source zip and writes to `dist/`.
- **Patch specs** in `.\source\patches\` (or pass `-PatchDir <path>`). May be empty; the script will still fix the checksum / disable it.
- **`-Deploy` only**: `VPINMAME_DIR` env var set (run `pinball-setup\setup-pinball.ps1` once).

## Patch spec format

One `.json` file per logical change. Files are sorted by name, so prefix with a number to control application order:

```json
{
  "name":    "human-readable description",
  "address": "$FFEE",
  "old_hex": "21 21",
  "new_hex": "21 29"
}
```

| Field | Required | Notes |
|---|---|---|
| `name` | No | Description, logged during build |
| `address` | One of these | WPC address: `$NNNN` (sys), `$NNNN@pXX` (banked), `0xNNNNN` (file offset) |
| `offset` | One of these | Raw file offset as decimal or `0x`-prefixed hex string |
| `old_hex` | Recommended | Space-separated hex bytes expected at the location (safety check; build aborts if mismatch) |
| `new_hex` | Yes | Space-separated hex bytes to write |

Address formats (same as `wpc-investigate rom.py`):

| Format | Meaning | Example |
|---|---|---|
| `$NNNN` | System ROM ($8000–$FFFF) | `"$FFEE"` |
| `$NNNN@pXX` | Banked page XX ($4000–$7FFF) | `"$4C0E@p37"` |
| `0xNNNNN` | Raw file offset | `"0x7FFEE"` |

**Example: disable checksum enforcement for testing**
```json
{
  "name":    "disable-checksum",
  "address": "$FFEC",
  "old_hex": "8D DE",
  "new_hex": "00 FF"
}
```
(Alternatively, just pass `-DisableChecksum` to `build.ps1` and skip the patch file.)

## WPC checksum

WPC startup walks the ROM byte-by-byte adding into a 16-bit wrapping accumulator, EXCEPT the two bytes at `$FFEC`/`$FFED` are read as a single big-endian 16-bit "delta" word and that value is added (in place of summing the two bytes individually). The total must equal the word stored at `$FFEE`.

`build.ps1` recalculates this automatically after applying patches. Two modes:

**Real checksum** (default):

Rule: `S_excl_delta + delta_word ≡ checksum_word_at_$FFEE  (mod 65536)`, where `S_excl_delta` is the sum of all ROM bytes with `$FFEC`/`$FFED` treated as zero, and `delta_word = ($FFEC << 8) | $FFED`.

1. Read target `C` = current checksum word at `$FFEE` (changes only if a patch modifies it).
2. Zero `$FFEC`–`$FFED` in a working copy and compute `S_excl_delta`.
3. `delta_word = (C - S_excl_delta) mod 65536`.
4. Write `delta_word` as a big-endian word at `$FFEC`–`$FFED`.
5. Verify under the same model: `S_excl_delta + delta_word ≡ C (mod 65536)`.

There is no byte-sum cap (no "510 maximum"); any single 16-bit value is a valid delta, so any patch — including one that changes `$FFEE` itself — can be balanced without compensating padding bytes. This is the actual WPC OS model, verified empirically on 2026-05-28 by building a ROM with a byte-sum that does NOT match the target and observing the WPC OS accept it (earlier "delta is two bytes summed additively, capped at 510" documentation was wrong).

**Disabled** (`-DisableChecksum`):
- Writes `0x00FF` at `$FFEC`. The WPC startup code sees `delta == 0xFF` and skips verification entirely. Safe for development builds; should be replaced with a real checksum before distribution.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `-RomZip` | First zip in `.\orig\` | Source ROM zip |
| `-PatchDir` | `.\source\patches` | Directory of `*.json` patch specs |
| `-OutZip` | `.\dist\<stem>_modded.zip` | Output zip |
| `-DisableChecksum` | off | Write `0x00FF` at `$FFEC` instead of recalculating |
| `-Deploy` | off | Copy output to `%VPINMAME_DIR%\roms\` |
| `-Force` | off | Overwrite existing output zip |

## Workflow: write → build → validate

```
1. Identify bytes to patch (wpc-investigate: rom.py dis/xref/funcs/dump/search;
   wpc-debug: the live debugger for confirming the path)
2. Write source/patches/NNN-description.json
3. & '.claude\skills\build-wpc-rom\build.ps1' -DisableChecksum
4. uv run .\.claude\skills\record-pinball\init_nvram.py \
       --rom-zip .\dist\congo_21_modded.zip --force
5. uv run .\.claude\skills\record-pinball\replay.py \
       --rom congo_21 --rom-zip .\dist\congo_21_modded.zip \
       --session .\sessions\<UTC> --nvram .\dist\congo_21_modded.nv \
       --trace state,dmd
6. Inspect the trace — confirm the patched code ran and produced the intended
   effect (e.g. expected DMD content at expected frames). Optionally run
   replay/diff_traces.py against a factory run to investigate unintended
   side effects; see "Two-run comparison" in record-pinball/SKILL.md for the
   caveats around NVRAM coupling.
7. When satisfied: rebuild without -DisableChecksum for a clean checksum
```

## Congo-specific notes

- Factory ROM: `orig/congo_21.zip` (game ROM `cg_g11.2_1`, 512 KiB)
- Checksum word `$FFEE` = 0x2121, delta `$FFEC` = 0x8DDE — **enforced** (this is
  ONLY the checksum target; it is NOT the displayed version — that was a long-held
  myth, disproven 2026-05-30)
- Displayed version = two ROM bytes `$FFBE` (major, 0x02) / `$FFBF` (minor, 0x10),
  loaded by `$42AE@p3A` and rendered by format engine `$4037@p39`. "2.1→2.2" =
  patch `$FFBF` 0x10→0x20. See `notes/congo-version-display.md`.
- Free space for new code: page $37 at `$7F80`+ (all 0xFF, ~128 bytes)

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── SKILL.md    # this file
└── build.ps1   # main build script
```
