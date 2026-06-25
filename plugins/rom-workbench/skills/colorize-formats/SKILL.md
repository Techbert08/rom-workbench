---
name: colorize-formats
description: Reference for the PIN2DMD→Serum colorization internals — the .pac/.pal/.vni and Serum .cROMc binary formats, the reverse-engineered trigger-hash schemes (Replace = plane0 CRC; ColorMask/LayeredColorMask = masked plane0 CRC), why PIN2DMD's plane hashing can't be reproduced by libserum's per-pixel hashing (the bridge), the libserum version pin and API gotchas, and fallback techniques (silhouette/shape triggers) for hard-to-reach frames and animations. Load alongside `colorize` when implementing or debugging the converter.
---

# colorize-formats

Hard-won internals behind the `colorize` skill. All CRC32 here is standard
(`0xEDB88320`, init `0xffffffff`, final XOR) — `zlib.crc32` ≡ libserum
`crc32_fast` bit-for-bit; PIN2DMD uses the same algorithm. The differences are
all about *what bytes get hashed*.

## File formats

### PAC (encrypted container)
`"PAC "` magic + version byte, then chunks: `int16 BE` type (1=PAL, 2=VNI),
`int32 BE` encrypted byte count, data = AES-128-CBC (key = IV =
`bytes.fromhex(vni.key)`) then gzip. `decrypt_pac.py` handles it.

### PAL (big-endian) — palettes + trigger table + masks
- `uint8` version; `uint16` num_palettes; each: index, num_colors, type, RGB×n.
- `uint16` num_mappings; each: **`uint32` CRC32** (the trigger), `uint8`
  switch_mode, `uint16` palette_idx, `uint32` VNI byte offset.
- `uint8` num_masks; each 512 bytes = a 128×32 bit mask.
- switch_mode: 0=Palette 1=Replace 2=ColorMask 3=Event 4=Follow
  5=LayeredColorMask 6=FollowReplace 7=MaskedReplace. (LOTR mix: Replace 93,
  ColorMask 148, LayeredColorMask 74, Follow 1.)

### VNI (big-endian, magic `"VPIN"`, v5) — the colorized frames
Offset table at byte 8 (`num_anims × uint32`). Each animation: name, 16 deprecated
bytes, frame count, embedded palette (ignore — use PAL palette), w/h (128×32),
per-anim masks, then frames. Each frame: `int16` plane_size, `uint16` delay,
`uint32` hash (often 0/unused), `uint8` bit_depth(7), `uint8` compressed flag;
data is `bit_depth` planes, optionally heatshrink (window=10, lookahead=5).
**Each plane is prefixed by a 1-byte marker, and the layout is NOT a flat
7-bit value (this was wrong before 2026-06-24).** Per PPUC/libvni `read_planes`:
a "7-plane" frame = **6 colour-index planes (markers `0x00`–`0x05`) + 1 MASK
plane (marker `0x6d` 'm')**. The mask plane is **excluded from the colour index**
(libvni stores it in `frame.mask`); each data plane goes to bit = its marker, so
the index is **0–63** and matches the 64-colour PAL palette exactly. In LOTR the
`0x6d` mask plane is always stored **first**, so the naive "all 7 planes by read
order, then `& 0x3F`" decode folds the mask into bit 0, shifts the real index up,
and drops the top index plane — corrupting ~99% of colorised pixels (the symptom:
"palette looks wrong"). Decode by marker; skip `0x6d`. (The old "bit 6 =
colorize flag / bit 6 clear = passthrough" model was a misreading of the mask
plane.) Compressed frames carry the same `[marker][plane]…` layout *inside* the
decompressed stream — strip markers there too, don't treat the blob as flat planes.
**Plane bit order for display is MSB-first** (bit 7 = leftmost pixel): decoding
LSB-first produces horizontally-mirrored frames — a real output bug if you ship
it, since `cframes` drives the spatial colorized output.

### Serum .cROMc (v7)
`"CROM"` + `uint16 LE` version(7) + `uint32 LE` uncompressed size + zlib-deflated
cereal `PortableBinaryArchive` of `SerumData`. Key V1 fields: `nocolors=4`
(input DMD shades — wrong value → grayscale-fallback too-dark frames),
`nccolors=64` (colorization palette — 0 makes `Serum_Load` return NULL),
`hashcodes` (useIndex=true, write via `setAtIndex`), `cpal` (192 B/frame),
`cframes` (4096 B/frame), `compmasks`/`compmaskID`/`shapecompmode`/`ncompmasks`
for masked triggers. compmaskID default 255 = no mask; shapecompmode default 0.

**Dynamic-region colorization (the live score — REQUIRED for ColorMask/LCM, added
2026-06-24).** A masked frame has two regions: the *static* region (the masked
background, coloured by `cframes`) and the *dynamic* region (score/timers — the
pixels the mask leaves out of the trigger hash) which must track the **live** DMD,
not a frozen frame. `cframes` alone freezes everything → the score sticks at the
authored value (looked like "scores always 0"). libserum V1 (`Colorize_Framev1`)
renders a pixel as `cpal[ dyna4cols[ dynamasks[tk]*nocolors + live_shade ] ]` when
`dynamasks[tk] != 255`. Mirror libvni's `render_color_mask` (output = live low
bits | colorist's high bits): emit per masked frame
- `dynamasks[tk]` = `(cframe[tk] >> 2) & 0xF` where the pixel is **dynamic**
  (compmask==1, i.e. mask-clear), else **255** (static → keep `cframes`);
- `dyna4cols[L*nocolors + s]` = `(L<<2) | s` (constant; re-inserts the live shade
  in the low 2 bits). `MAX_DYNA_4COLS_PER_FRAME=16` covers the 4 high bits.
`frameHasDynamic` / `dynamasks_active` are **derived at load** (active where
`dynamasks != 255`) — don't set them. This also retires the old wrong conclusion
that "frame-0-only is correct for ColorMask by design": the *static* region is
frame-0 (animation continuations don't help it), but the *dynamic* region needs
this dyna machinery to follow the live frame.

## The trigger-hash schemes (reverse-engineered empirically)

PIN2DMD computes its PAL trigger CRC over a **bit-plane**, not the per-pixel frame:

- **Replace / Follow (modes 1, 4):** `CRC32(plane0)` where plane0 = bit 0 of each
  pixel, LSB-packed into 512 bytes. (Some frames key on plane1; try both.)
- **ColorMask / LayeredColorMask (modes 2, 5):** `CRC32(plane0 AND mask)`,
  keep-where-mask-bit-**set**, LSB packing. The mask's set bits are the *static*
  region; the dynamic region (scores, etc.) is excluded so one trigger covers
  every variation. Verified: dominant scheme, 0 mask-assignment ambiguity. Crack
  it on a new game with `discover_masks.py` (it sweeps plane/polarity/packing).

These were found by hashing real captured RAW frames every which way and seeing
which scheme hit the PAL CRC set far above chance.

**Colorization survives CPU/rules mods (capture once, play modded).** On games whose
DMD is rendered by a *separate* controller (Stern Whitestar/SAM, WPC DMD board), the
frame bitmaps come from the display ROM, not the main CPU. A gameplay/rules mod that
only swaps the main CPU ROM (e.g. LOTR `lotrcpua.a00`) leaves the display ROM
(`lotrdspa.a00` + `bios.u8`) byte-identical, so the same screens produce the same
frame CRCs and the colorization triggers still fire. Verify by comparing the display
ROM CRC across zips (`unzip -v`); if it matches factory, you can capture against the
factory ROM and play the modded ROM with full colorization. (New screens the mod adds
won't be coloured until captured, but everything shared with factory is.)

## Why a bridge is mandatory (the core insight)

libserum, at runtime, computes `crc32_fast(frame, fwidth*fheight)` over the
**4096-byte per-pixel** live frame (or `calc_crc32` with a comp mask, still
per-pixel, optionally shape-clamped) and matches it to the stored hashcode. It
**never** hashes a bit-plane. So PIN2DMD's stored plane-CRC is in a different
domain and can't be reproduced from the per-pixel frame — and CRC is one-way, so
you can't invert the PAL CRC back to a frame either.

**Bridge:** use the captured per-pixel frame `F` as the Rosetta stone. Compute
F's plane-CRC (PIN2DMD domain) → look up the colorization in the PAL. Store the
**libserum-domain** hash of that same F as the trigger:
- unmasked: `crc32(F)` over all 4096 per-pixel bytes (mask=255, shape=0).
- masked: `crc32` over F's per-pixel values where the Serum compmask is 0. The
  Serum compmask is the **inverted** PAL mask (0 where the PAL bit is set,
  because libserum hashes where compmask==0) so it hashes exactly the static
  region. Same static content ⇒ same hash ⇒ one trigger fires across all dynamic
  variants.

Partial offline shortcut (correctly oriented VNI): with the MSB orientation fix,
the VNI colorized frame's **plane0 reproduces the PAL trigger CRC for ~62/316
LOTR mappings** (`pac2serum --selftest-vni` → mode1=54, mode2=8). So for those,
VNI structure == original structure. This still does NOT directly yield a
libserum hashcode for 4-shade frames (you'd need the full per-pixel values, i.e.
plane1 too), but it's a strong prior for the silhouette/offline route below and
worth revisiting for animations. (Before the orientation fix this test returned
0 — a stale "VNI can't help" claim is wrong.) The display ROM still can't be
statically scanned for raw planes (`scan_rom_planes.py` → 0; compressed,
68000-decoded — `inspect_display_rom.py` confirms).

## Fallback: silhouette / "shape" triggers (for hard-to-reach frames & animations)

When a frame is hard to capture in gameplay (rare screens, or **animation frames
2..N** that play on PIN2DMD's own timer with no per-frame ROM CRC), there's an
offline option that needs **no replay or brute force**:

libserum's **shape mode** (`shapecompmode=1`) hashes the lit/unlit *silhouette*
(per-pixel value clamped `>1 → 1`), optionally within a comp mask. The silhouette
is **directly readable from the VNI colorized frame** (lit = non-background),
because the colorizer lights exactly the pixels the original lit. So you can emit
a working shape-mode hashcode for *any* mapping straight from PAL+VNI:
- Replace: `crc32_shape` over the whole frame's lit pattern.
- ColorMask/LCM: `crc32_shape` over the **masked** static-region lit pattern
  (whole-frame won't match — the live dynamic region differs from the VNI's).

Tradeoff — **specificity**: silhouettes are coarser than exact frames, so
similar-shaped screens collide and can mis-colorize. Measured on LOTR's 316
mappings: 259 distinct whole-frame silhouettes, 81 frames in collisions; masking
to the static region should reduce that. **Use it as a guarded supplement, not a
replacement:** keep exact replay-derived triggers where you have them, add shape
triggers only for silhouettes that are unique and don't collide with an exact
trigger. (Brute-forcing the original plane0 from colorized color-groups is cheap
— ~2^12/frame, 90.7% of frames consistent — but does NOT yield a usable hashcode,
because reconstructing plane0 leaves plane1 unconstrained and libserum needs full
per-pixel values; the silhouette route sidesteps this.)

## libserum: version pin, API, validation

- **Version pin:** VPX bundles libserum **2.6.0** built from PPUC/libserum (same
  distinctive log strings). `pac2serum` pins PPUC tag v2.6.0 (commit `21b28325`)
  via FetchContent + two patches: `0001` additive `setAtIndex` (writer; cereal layout
  stays byte-for-byte) and `0002` `maxFramesToSkip=20` default. The build also emits
  the runtime `lib/libserum.dylib` (`serum_shared` target) from the same sources — the
  writer (`serum_static`→pac2serum) and the deployed reader stay in lockstep.
- **Uncolorized-frame passthrough:** on a frame with no trigger match, libserum holds
  the last colorized frame and returns `IDENTIFY_NO_FRAME` *unless* `maxFramesToSkip`
  or `ignoreUnknownFramesTimeout` is set — then after the threshold it applies the
  standard grayscale palette and returns 0 (passthrough to the original DMD). Both
  default to **0 upstream** (= freeze forever), and the BGFX `plugin-serum` never sets
  them, so patch `0002` defaults `maxFramesToSkip=20`: ~20 unmatched frames bridges
  brief intra-animation gaps, then a genuinely uncolorized screen shows through. Tune
  in `vendor/patches/0002-passthrough-unknown-frames.patch`. (Note: the
  `ignoreUnknownFramesTimeout` path gets overwritten to 0x2000 when `showStatusMessages`
  triggers — that's why we use the frame-count lever, not the timeout.)
- **API gotcha:** `Serum_ColorizeWithMetadatav2(frame, bool sceneRequested)` —
  output goes into the `Serum_Frame_Struc*` returned by `Serum_Load`, NOT via
  out-params. For testing call the clean 1-arg `Serum_Colorize(frame)` and read
  `struct.palette` (192 B) / `struct.frame` (4096 B). The multi-arg signature in
  old notes is wrong.
- **Validation:** stage `<tmp>/<rom>/<rom>.cROMc`, `Serum_Load(<tmp>, rom, 0)`
  (NULL = ValidateLoadedGeometry failed — check nccolors/nocolors), feed captured
  frames through `Serum_Colorize`, count hits, and compare the returned palette
  to the PAL palette for the matched mapping (byte-exact = full chain correct).

## Replay segfault (operational)

`replay_host.py` (libpinmame) SIGSEGVs on ~14% of runs — intermittent, native,
NOT session-specific (big sessions crash next to identical ones that pass; boot
time doesn't discriminate; a failed session succeeds on retry). Not our code; a
prebuilt-binary bug. Fix: relaunch-retry (≤4×) per session in the capture batch.
Empty/aborted sessions (0 switch events) also crash but run fine on retry.
