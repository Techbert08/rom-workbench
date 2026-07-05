---
name: build
description: Apply JSON patch specs to a WPC ROM zip and produce a patched, checksum-correct output zip ready for VPinMAME. Use when you have patch specs in source/patches/ and want to build dist/<rom>_modded.zip, or when you want to validate a patch by replaying a recorded session against it via record.
---

# build

> **Orientation:** if you haven't already, load `rom-workbench:overview` for the
> end-to-end mod workflow (setup → record → synthesize → debug → build → test)
> and where this step fits.

Applies ordered JSON patch specs to a WPC game ROM, recalculates the WPC 16-bit checksum, and repackages the ROM zip. The output drops into VP's `roms/` directory for immediate testing via `record`'s single-sided `replay.py`.

## When to invoke

- "build the modded ROM" / "apply the patches"
- "I've written a patch spec, build and test it"
- "deploy the patched ROM to VPinMAME"
- Verifying a mod by replaying a recorded session against the patched ROM

## Quickstart

```bash
# Build with checksum disabled (safest during development)
python3 '${CLAUDE_PLUGIN_ROOT}/bin/build.py' --disable-checksum

# Build with real checksum, deploy into VP's roms/ dir
python3 '${CLAUDE_PLUGIN_ROOT}/bin/build.py' --deploy

# Validate against a recorded session (single-sided replay against the modded ROM).
# First time the modded zip changes the WPC checksum word ($FFEE), produce a
# freshly-reset NVRAM snapshot so the replay doesn't pay the factory-reset cost.
python3 '${CLAUDE_PLUGIN_ROOT}/bin/init_nvram.py' --rom-zip ./dist/congo_21_modded.zip --force
python3 '${CLAUDE_PLUGIN_ROOT}/bin/replay.py' \
    --rom congo_21 --rom-zip ./dist/congo_21_modded.zip \
    --session ./sessions/<UTC> \
    --nvram ./dist/congo_21_modded.nv \
    --trace state,dmd
```

> **⚠️ ROM name vs. zip filename.** PinMAME looks a game up by the `--rom` NAME
> (its driver id), NOT the zip's filename. A modded ROM has no driver of its own,
> so it must REUSE the real game's name: the emulator stages whatever `--rom-zip`
> you give it under `<--rom>.zip`. Always pass `--rom <realgame>` (e.g. `--rom lotr`)
> even when `--rom-zip dist/lotr_modded.zip`. `init_nvram.py` derives the name from
> the zip stem but only strips a trailing `_modded`/`_mod` — so name modded zips
> `<realgame>_modded.zip`, or pass `--rom <realgame>` explicitly. A wrong name
> surfaces as `PinmameRun returned status 2 (GAME_NOT_FOUND)`, not a ROM/boot fault.

## Prerequisites

- **`python3`** on PATH (3.9+). `build.py` is stdlib-only — no extra packages needed.
- **ROM zip** in `./orig/` (or pass `--rom-zip <path>` / `--rom <name>`). The build never modifies `orig/` — it reads the source zip and writes to `dist/`.
- **Patch specs** in `./source/patches/` (or pass `--patch-dir <path>`). May be empty; the script will still fix the checksum / disable it.
- **`--deploy` only**: `VPINMAME_DIR` (Windows) / `PINMAME_DIR` (macOS) env var set (run the `setup` skill once).

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

Address formats (same as `rom.py`):

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
(Alternatively, just pass `--disable-checksum` to `build.py` and skip the patch file.)

## WPC checksum

The WPC boot self-test sums **every byte of the game ROM** into a 16-bit wrapping accumulator and requires the total to equal the word stored big-endian at `$FFEE`. The two bytes at `$FFEC`/`$FFED` are an ordinary-valued **correction knob** — summed as plain bytes, combined range `0..510` — *not* a 16-bit "delta word". A mismatch shows as **`G11 CHECKSUM ERROR`** in the operator test report (G11 = the CPU/game ROM chip).

> **Verified against the factory ROM as oracle:** `sum(all 512 KiB of cg_g11.2_1) == 0x2121 == word @ $FFEE`, with `$FFEC/$FFED = 8D DE` counted as the plain bytes `0x8D + 0xDE = 363`. (An earlier model here zeroed `$FFEC/$FFED` and added them back as a big-endian *word* — over-counting the correction by up to `~0x8C73` and shipping ROMs that passed the build self-check but failed the real test with a G11 error. Fixed 2026-06-13.)

`build.py` recalculates this automatically after applying patches. Two modes:

**Real checksum** (default):

Rule: `sum(all bytes) ≡ ($FFEE << 8) | $FFEF  (mod 65536)`. With correction bytes `c0=$FFEC`, `c1=$FFED` and `B` = the sum of all bytes *except* the four at `$FFEC..$FFEF`, the `$FFEF` low byte cancels and this reduces to `c0 + c1 ≡ 255*$FFEE - B (mod 65536)`.

1. Compute `B` (all four checksum-field bytes treated as zero).
2. Keep the checksum word at its current value if the needed correction `(255*$FFEE - B) mod 65536` fits the `0..510` two-byte range; set `$FFEC/$FFED` to split it.
3. Otherwise **float** the `$FFEE` high byte to the nearest value that admits a valid 2-byte correction (preserving `$FFEF`), then set `$FFEC/$FFED`. The build logs `word floated 0xAAAA -> 0xBBBB` when it does this.
4. Verify directly: `sum(all bytes) == ($FFEE << 8) | $FFEF`.

The correction field is only 510 wide, so a patch that perturbs the byte-sum by more than that (e.g. filling `0xFF` free-space with opcodes) **will** float the checksum word. That is harmless — `$FFEE` is an internal consistency value, not the displayed game version (that's `$FFBE/$FFBF`). Floating it changes the value shown on the test-report checksum audit but the audit still reads **GOOD** because it stays self-consistent.

**Disabled** (`--disable-checksum`):
- Writes `0x00FF` at `$FFEC` to skip verification during development. (Skip-sentinel behavior under the byte-sum model is unverified — prefer a real checksum for anything you boot in VP/hardware; rebuild without the flag before distribution.)

## Sega/Stern Whitestar checksum

`build.py` reads `game.json` (`load_game_manifest()`) and, when `platform` is
`whitestar`, switches both ROM selection and checksum handling:

- **CPU-ROM selection** mirrors `rom.py`: among valid-sized zip entries it scores
  names (`cpu`/`_g` bonus; `sound`/`dsp`/`bios` penalty) and breaks ties by the
  6809 reset vector (last two bytes ≥ `$8000`). A multi-file Whitestar zip
  resolves to the real CPU ROM (e.g. `lotrcpua.a00`) instead of a 1 MB sound ROM.
- **Banked addresses** (`$NNNN@pXX`) use the same `SIZE_TO_FIRST_PAGE` table as
  `rom.py`; a 128 KiB CPU ROM has first page `$38` (banked pages `$38-$3D`).

The Whitestar boot self-test sums **every CPU-ROM byte into an 8-bit accumulator**
and requires the total to be `0xFF`; alternatively, if the word at `$FFEE` is
`0xFFFF` the test is skipped. So:

- **Real checksum** (default): make `(sum of all bytes) & 0xFF == 0xFF`. `build.py`
  absorbs the patch's effect into one unused `0xFF` padding byte (auto-found by
  scanning backward from `$FFEC`, clear of the checksum word / delta slot / 6809
  vectors). No-op if the sum is already correct.
- **`--checksum-blank <addr>`**: use an *explicit*, disassembly-verified free-byte
  address instead of the auto-scan — e.g. `--checksum-blank '$F974'`. Since the
  self-test wraps mod 256, one byte always suffices no matter how much the
  patches shift the sum (the pad byte's value just gets set to whatever residue
  is needed). Useful for real/replacement CPU boards that re-run this same
  8-bit self-test independently: pinning the correction to a known location
  (ideally one already reserved as spare space by your patch, e.g. right after
  a code stub) makes the build deterministic and reviewable instead of an
  implicit runtime choice. Set a per-project default via `game.json`'s
  `checksum_blank` key so it applies to every build without repeating the flag
  (an explicit `--checksum-blank` still overrides it). Mutually exclusive with
  `--disable-checksum`.
  > **Not for restoring the exact factory byte-sum.** The self-test only checks
  > the sum mod 256; it does not — and cannot — restore the *exact* pre-patch
  > total. That's mathematically impossible once a patch replaces part of the
  > `0xFF` free-space run with real code: `0xFF` is the maximum byte value, so
  > the pad byte (also originally `0xFF`) can only be *lowered*, never raised,
  > and can't compensate for the sum decrease from writing lower-valued opcode
  > bytes elsewhere. If a board needs more than the documented mod-256 check
  > (e.g. an independent CRC/hash of the whole ROM), no filler-byte trick can
  > satisfy it — the ROM's content has genuinely changed.
- **Disabled** (`--disable-checksum`): writes `0xFFFF` at `$FFEE`.

(Whitestar uses a single 8-bit byte-sum target — there is no WPC-style delta word.
`rom.py info`'s WPC-model checksum/version line is not meaningful for Whitestar.)

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `--rom` | — | Game name; source is `./orig/<rom>.zip` |
| `--rom-zip` | First zip in `./orig/` | Source ROM zip (overrides `--rom`) |
| `--patch-dir` | `./source/patches` | Directory of `*.json` patch specs |
| `--out-zip` | `./dist/<stem>_modded.zip` | Output zip |
| `--disable-checksum` | off | Write `0x00FF` at `$FFEC` instead of recalculating |
| `--checksum-blank` | auto-scan | Whitestar: explicit free-byte address for the checksum pad (default in `game.json`'s `checksum_blank`) |
| `--deploy` | off | Copy output into VP's `roms/` dir |
| `--force` | off | Overwrite existing output zip |

## Workflow: write → build → validate

```
1. Identify bytes to patch (debug: rom.py dis/xref/funcs/dump/search;
   debug: the live debugger for confirming the path)
2. Write source/patches/NNN-description.json
3. python3 '${CLAUDE_PLUGIN_ROOT}/bin/build.py' --disable-checksum
4. python3 '${CLAUDE_PLUGIN_ROOT}/bin/init_nvram.py' \
       --rom-zip ./dist/congo_21_modded.zip --force
5. python3 '${CLAUDE_PLUGIN_ROOT}/bin/replay.py' \
       --rom congo_21 --rom-zip ./dist/congo_21_modded.zip \
       --session ./sessions/<UTC> --nvram ./dist/congo_21_modded.nv \
       --trace state,dmd
6. Inspect the trace — confirm the patched code ran and produced the intended
   effect (e.g. expected DMD content at expected frames). Optionally run
   diff_traces.py against a factory run to investigate unintended
   side effects; see "Two-run comparison" in record/SKILL.md for the
   caveats around NVRAM coupling.
7. When satisfied: rebuild without --disable-checksum for a clean checksum
```

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── skills/build/SKILL.md   # this file
└── bin/
    └── build.py            # main build script (cross-platform)
```
