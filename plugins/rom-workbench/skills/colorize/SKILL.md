---
name: colorize
description: Convert a PIN2DMD .pac colorization into a libserum Serum .cROMc that VPinMAME/VPX loads on any platform, by bridging PIN2DMD's plane-CRC triggers to libserum's per-pixel hashing using real replayed DMD frames. Use to port a PIN2DMD colorization to Serum, build/extend a trigger inventory for a game, emit a .cROMc, and deploy it to the AltColor path. Covers decrypt → build converter → replay-capture frames → accumulate triggers (Replace + ColorMask/LayeredColorMask masks) → emit .cROMc → validate in libserum.
---

# colorize

> **Orientation:** load `rom-workbench:overview` first. This skill reuses the
> `record` skill's replay substrate (sessions, NVRAM, `replay.py`) to capture
> the DMD frames it needs. For the *why* behind every CRC/format decision here,
> load `rom-workbench:colorize-formats` — it holds the reverse-engineered PAC/
> PAL/VNI/Serum formats and the cracked hashing schemes.

## The one thing to understand first

PIN2DMD and libserum **hash different things**, so you cannot transcode a `.pac`
into a `.cROMc` as a pure file operation:

- **PIN2DMD** keys a trigger on the CRC32 of a **512-byte bit-plane** of the
  original DMD frame (masked, for ColorMask/LayeredColorMask).
- **libserum** recomputes `crc32_fast` over the **4096-byte per-pixel** live
  frame and matches that against the stored hashcode.

A CRC is one-way; the `.pac` only stores PIN2DMD's plane-CRC, which libserum can
never reproduce from the per-pixel frame. **The bridge:** replay the game,
capture the real per-pixel frames, use PIN2DMD's plane-CRC only to *look up*
which colorization a captured frame belongs to, and store the **libserum-domain**
hash of that same captured frame as the trigger. The colorized output pixels
come from the `.pac`'s VNI animation. This is exact and verified end-to-end.

Consequence: **coverage = frames you actually observe.** A trigger whose screen
you never display in a replay can't be added (it can't be reverse-derived from
the CRC). Drive gameplay (record / synthetic-record) to the screens you need.

## Pipeline

```
 pin2dmd.pac ──decrypt_pac.py (AES-128-CBC key=IV, gunzip)──► game.pal + game.vni
                                                                    │
 factory ROM ──replay.py --trace dmd (RAW 0–3 mode)──► captured DMD frames (.bin)
                                                                    │
                              pac2serum --pal --vni --frames <dirs> --out game.cROMc
                                                                    │
                                     deploy to <AltColor>/<rom>/<rom>.cROMc
```

### Step 1 — decrypt the PAC

```
python3 '${CLAUDE_PLUGIN_ROOT}/serum/scripts/decrypt_pac.py' \
    --pac pin2dmd.pac --key <32-hex-chars> --out out/
# → out/<game>.pal (palettes + trigger table + masks) and out/<game>.vni (frames)
```
The AES key is usually shipped with the colorization as `vni.key` (hex).

### Step 2 — build the converter (once per machine)

```
'${CLAUDE_PLUGIN_ROOT}/serum/build.sh'   # → serum/build/pac2serum + refreshes lib/libserum.dylib
```
Pins libserum to the exact PPUC v2.6.0 release VPX bundles (so the cereal
`SerumData` layout matches byte-for-byte). A prebuilt
`serum/bin/pac2serum.macos-arm64` is available if you can't build. The build also
produces the **runtime** `lib/libserum.dylib` (the `serum_shared` target) from the
same patched sources — this is what `setup` deploys into the VPX bundle, and it
carries patch 0002 (`maxFramesToSkip=20`) so uncolorized screens **pass through** to
the original DMD instead of freezing. Writer and reader stay in lockstep.

### Step 3 — capture DMD frames from the FACTORY ROM (RAW mode)

Replay sessions against the **factory** ROM (the one the colorization was authored
against — modded ROMs produce different DMD content → different CRCs → no match).
`replay.py`/`replay_host.py` already set `PinmameSetDmdMode(RAW=1)`, so captured
`.bin` frames are 0–3 shade indices (verify: pixel values should be `[0,1,2,3]`,
not PWM 0–255).

```
python3 '${CLAUDE_PLUGIN_ROOT}/bin/replay.py' --rom <rom> --rom-zip orig/<rom>.zip \
    --session sessions/<name> --nvram orig/<rom>.nv \
    --trace dmd --no-sound --overwrite --out-dir captures/<name>
```
Accumulate every gameplay session into one parent dir (`captures/`). More
distinct screens = more triggers. **Wrap each replay in a retry loop**:
libpinmame segfaults intermittently (~14%/run, native, content-independent); a
fresh process almost always clears it (see colorize-formats → "replay segfault").

### Step 4 — build the .cROMc (accumulating, with masks)

```
'${CLAUDE_PLUGIN_ROOT}/serum/build/pac2serum' \
    --pal out/<game>.pal --vni out/<game>.vni --rom <rom> \
    --out <rom>.cROMc \
    --frames captures            # repeatable; each dir scanned recursively
```
`pac2serum` matches each captured frame to a PAL trigger (unmasked plane0/plane1
CRC → Replace/Follow; masked `plane0 & mask` → ColorMask/LayeredColorMask),
emits one Serum entry per unique trigger keyed by the libserum-domain hash, wires
the PAL masks in as Serum compmasks (so one masked trigger fires across all
dynamic-region variations), and reports coverage by PAL mode.

### Step 5 — deploy

Drop `<rom>.cROMc` at `<AltColorPath>/<rom>/<rom>.cROMc` (VPX reads
`AltColor=...` from `VPinballX.ini`; libdmdutil's SerumThread loads it on the
first `Mode::Data` frame). The ROM name is the PinMAME zip stem (e.g. `lotr`).

### Preview the colorization headlessly (no VPX)

Render a captured session's DMD as colorized video straight through libserum — the
same pixels VPX/libdmdutil would show, but with no emulator/VPX in the loop. This is
the fastest validate-and-eyeball loop after a build:

```
python3 '${CLAUDE_PLUGIN_ROOT}/bin/render_dmd_video.py' <replay-out-dir> \
    --colorize --rom <rom>        # mp4 (or .gif without ffmpeg); --altcolor/--lib to override
```

It loads the deployed `<altcolor>/<rom>/<rom>.cROMc`, colorizes each RAW (0–3) frame
via `Serum_Colorize`, resamples to real-time playback, and prints how many frames
matched a Serum trigger — a quick coverage sanity check that doubles as the
"validate in libserum" step (a NULL load or zero matches flags a broken `.cROMc`).

### Verify in-game LIVE (VPX BGFX, macOS) — the real proof

The headless preview proves the `.cROMc` is correct; this proves the whole VPX
runtime colorizes it on a live ROM. **The BGFX standalone build routes Serum
completely differently from the old GL build** — get any of four things wrong and
the DMD stays grayscale or VPX crashes on start. `play-colorized.py` encodes the
working recipe (first confirmed in-game colorization: LOTR, 2026-06-24):

```
python3 '${CLAUDE_PLUGIN_ROOT}/bin/play-colorized.py' --rom <rom> \
    --table "<path>/<table>.vpx" \
    [--rom-zip orig/<rom>.zip] [--cromc output/<rom>.cROMc]   # else resolved by convention
```

What it does, and **why each step is load-bearing under BGFX**:

1. **Serum is its own `[Plugin.Serum]`** — not libdmdutil's built-in path, and
   **NOT `[Standalone] AltColor`** (that key is the libdmdutil/PIN2DMD *palette*
   technique, a different mechanism). The script sets `Enable = 1`.
2. **The Serum plugin ignores `~/.vpinball/altcolor`.** On game start it searches
   `<table_dir>/serum/<rom>/<rom>.cROMc`, then
   `<table_dir>/pinmame/altcolor/<rom>/<rom>.cROMc`, then `<SerumPath setting>/...`.
   The script stages the `.cROMc` into that **table-relative `pinmame/` tree**.
3. **The BGFX PinMAME plugin loads ROMs from a table-relative rompath**
   (`vpmPath = <table_dir>/pinmame/` → `<table_dir>/pinmame/roms/<rom>.zip`), NOT
   the `PINMAME_DIR` the `record` skill stages to, NOT `[Plugin.PinMAME]
   PinMAMEPath`. Wrong path → "required files are missing" → game ends instantly →
   `libpinmame OnGameEnd` NULL-deref crash. The script stages the ROM there too.
4. **BGFX's plugin-pinmame needs an ABI-matched libpinmame.** `setup-pinball.py`
   builds the bundle's libpinmame from the bundle's *exact* pinned pinmame SHA
   (`vbousquet/pinmame`) **+ the vpintf switch recorder only**, so the one bundle
   libpinmame both colorizes (all ~38 plugin imports resolve) **and** records
   (`vp_switchlog`). The full debugger/memory build is separate — at `PINMAME_DIR`,
   loaded directly by `replay_host.py`, never through the bundle. `play-colorized.py`
   sanity-checks that the bundle lib is the matched build.

Confirmation in `~/.vpinball/vpinball.log`: ROM boots (no "NOT FOUND"), then
`DMD Source Changed: format=1` immediately followed by `format=2` — the Serum
plugin inserting its colorized RGB source. Then look at the DMD: it's in color.
Known cosmetic bug: VPX SIGSEGVs in `libpinmame OnGameEnd` on window close
(a VPX/plugin teardown bug, unrelated to colorization).

## Coverage tools

- **Catalog covered vs uncovered triggers** (renders each mapping's colorized
  frame as a PPM into `covered/` + `uncovered/`):
  ```
  pac2serum --pal ... --vni ... --dump-vni cat/ --frames captures
  # view: sips -s format png -z 192 768 cat/uncovered/<f>.ppm --out x.png  (then Read it)
  ```
  Use this to *see* which screens you still lack and which game modes to target.
  Tile many at once with `scripts/montage.py <out.ppm> <cols> <scale> <ppms...>`
  (→ `sips` to PNG, then Read) to scan coverage at a glance.
- **Crack the mask scheme on a NEW game** (if Replace-only matching misses the
  mask modes): `scripts/discover_masks.py --pal ... --frames captures` — finds
  the masked-plane convention and which mask each mapping uses.
- **Diagnostics:** `pac2serum --selftest-vni` (confirms VNI can't shortcut the
  trigger CRC); `scripts/inspect_display_rom.py` / `scan_rom_planes.py` (assess
  whether a display ROM's frames are statically extractable — for LOTR they are
  NOT: compressed, 68000-decoded).

## Adding a new game — checklist

1. Obtain `pin2dmd.pac` + AES key; `decrypt_pac.py` → `.pal` + `.vni`.
2. Confirm the **factory** ROM (CRC matches what PIN2DMD authored against).
3. `parse_pal_vni.py` to see mode mix (Replace/ColorMask/LayeredColorMask) + mask
   count. If mask modes dominate, run `discover_masks.py` to confirm the masked
   convention holds for this game (it may differ from LOTR's plane0/set/lsb).
4. Replay gameplay sessions (factory ROM, RAW) → `captures/`.
5. `pac2serum --frames captures` → `.cROMc`; check coverage-by-mode.
6. `--dump-vni` to find uncovered screens; add targeted sessions; repeat 4–5.
7. Deploy and validate in libserum (see colorize-formats → validation harness).

## Gotchas (full detail in colorize-formats)

- Capture against the **factory** ROM, in **RAW** mode (0–3, not PWM).
- Retry replays — libpinmame segfaults intermittently.
- `nocolors=4` (input DMD shades), `nccolors=64` (colorization palette); wrong
  values make `Serum_Load` return NULL or render too dark.
- Test API: call `Serum_Colorize(frame)` and read the returned
  `Serum_Frame_Struc*` (`.palette`/`.frame`) — NOT the multi-arg
  `Serum_ColorizeWithMetadatav2`.
