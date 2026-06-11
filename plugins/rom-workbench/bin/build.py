#!/usr/bin/env python3
"""Apply JSON patch specs to a WPC ROM zip and produce a patched ROM zip.

Reads every *.json file in --patch-dir (sorted by name, so prefix numbers
control order), validates the expected bytes at each target location, writes the
new bytes, then updates the WPC checksum so the ROM passes startup verification.

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
writes the corrected delta word at $FFEC. Pass --disable-checksum to instead
write delta=0x00FF, which tells the WPC OS to skip verification entirely (useful
during development when the checksum formula isn't yet confirmed).

ROM zip and outputs follow the working-dir convention:
    source    ./orig/<rom>.zip   (override: --rom-zip; default: first zip in ./orig/)
    patches   ./source/patches   (override: --patch-dir)
    output    ./dist/<rom-stem>_modded.zip   (override: --out-zip)

Usage:
    python3 build.py [--rom congo_21] [--rom-zip <path.zip>] [--patch-dir <dir>]
                    [--out-zip <path.zip>] [--disable-checksum] [--deploy] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

from workbench_env import (
    _C, _c, bootstrap_venv, die, load_config, load_game_manifest, ok, step, warn,
)

IS_WIN = os.name == "nt"


# =============================================================================
# ROM geometry (shared by WPC and Sega/Stern Whitestar — both 6809, 0x4000 banks)
# =============================================================================

# ROM size -> first banked page number. The 6809 banked window is $4000-$7FFF;
# the top $8000-$FFFF is the always-resident system region (2 pages). WPC and
# Whitestar share this geometry; for a 128 KiB Whitestar CPU ROM (e.g. LOTR's
# lotrcpua.a00) the first page is $38 and banked pages run $38-$3D. This table
# and resolve_address() must stay byte-for-byte identical to rom.py so patch
# addresses land where the disassembler says they do.
SIZE_TO_FIRST_PAGE = {
    131072:  0x38,   # 128 KiB
    262144:  0x34,   # 256 KiB
    524288:  0x20,   # 512 KiB
    1048576: 0x00,   # 1 MiB
}
BANK_SIZE = 0x4000
SYS_SIZE = 0x8000
VALID_SIZES = (131072, 262144, 524288, 1048576)


def rom_first_page(rom: bytearray) -> int:
    sz = len(rom)
    if sz not in SIZE_TO_FIRST_PAGE:
        die(f"Unsupported ROM size {sz} bytes (expected 128K/256K/512K/1M).")
    return SIZE_TO_FIRST_PAGE[sz]


def resolve_address(rom: bytearray, spec: str) -> int:
    """Convert an address spec to a file offset.

    Supports: "$NNNN" (sys ROM), "$NNNN@pXX" (banked),
    "0xNNNNN" / plain integer (file offset).
    """
    import re
    spec = spec.strip()

    # Raw file offset: 0xNNNNN or plain number.
    if re.fullmatch(r"0x[0-9a-fA-F]+", spec):
        return int(spec, 16)
    if re.fullmatch(r"\d+", spec):
        return int(spec)

    # Banked: $NNNN@pXX
    m = re.fullmatch(r"\$?([0-9a-fA-F]{4})@p([0-9a-fA-F]{2})", spec)
    if m:
        addr = int(m.group(1), 16)
        page = int(m.group(2), 16)
        first_page = rom_first_page(rom)
        if page < first_page or page > 0x3D:
            die(f"Page 0x{page:02X} out of range (first=0x{first_page:02X}).")
        return (page - first_page) * BANK_SIZE + (addr - BANK_SIZE)

    # System ROM: $NNNN
    m = re.fullmatch(r"\$([0-9a-fA-F]{4})", spec)
    if m:
        addr = int(m.group(1), 16)
        if addr < 0x8000:
            die(f"${m.group(1)} is below $8000; use $NNNN@pXX for banked pages.")
        return (len(rom) - SYS_SIZE) + (addr - 0x8000)

    die(f"Cannot parse address spec '{spec}'. Use $NNNN, $NNNN@pXX, or 0xNNNNN.")


def fmt_hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def parse_hex(hex_str: str) -> bytes:
    return bytes(int(tok, 16) for tok in hex_str.split())


# =============================================================================
# WPC checksum
# =============================================================================
#
# WPC startup walks the ROM byte-by-byte adding into a 16-bit wrapping
# accumulator, EXCEPT the two bytes at $FFEC/$FFED are read as a big-endian
# 16-bit "delta" word and that single value is added (in place of summing the
# two bytes individually). The total must equal the word stored at $FFEE.
#
# So if S = sum-of-all-bytes-with-FFEC-FFED-zeroed and C = the word at $FFEE,
# the delta we need to write is just (C - S) mod 65536. There is no byte-sum
# cap, so any single 16-bit value works and any patch can be balanced.

def update_checksum(rom: bytearray) -> None:
    sys_base = len(rom) - SYS_SIZE
    delta_off = sys_base + (0xFFEC - 0x8000)
    cksum_off = sys_base + (0xFFEE - 0x8000)

    target = (rom[cksum_off] << 8) | rom[cksum_off + 1]

    # Sum-of-all-bytes with the delta bytes treated as zero.
    rom[delta_off] = 0
    rom[delta_off + 1] = 0
    s_excl_delta = sum(rom) & 0xFFFF

    # Delta as a 16-bit big-endian word; no overflow possible after the mod.
    needed = (target - s_excl_delta) & 0xFFFF
    d_hi = (needed >> 8) & 0xFF
    d_lo = needed & 0xFF
    rom[delta_off] = d_hi
    rom[delta_off + 1] = d_lo

    # Verify under the actual model: byte-sum minus the two delta bytes, plus
    # delta as a word.
    verify = sum(rom) & 0xFFFF
    verify = (verify - rom[delta_off] - rom[delta_off + 1]
              + ((rom[delta_off] << 8) | rom[delta_off + 1])) & 0xFFFF
    if verify != target:
        die(f"Checksum verify failed: got 0x{verify:04X}, want 0x{target:04X}.")
    ok(f"Checksum: delta=0x{d_hi:02X}{d_lo:02X}  target=0x{target:04X}  [OK]")


def disable_checksum(rom: bytearray) -> None:
    sys_base = len(rom) - SYS_SIZE
    delta_off = sys_base + (0xFFEC - 0x8000)
    rom[delta_off] = 0x00
    rom[delta_off + 1] = 0xFF
    warn("Checksum disabled (delta=0x00FF). ROM will pass startup without "
         "checksum verification.")


# =============================================================================
# Sega/Stern Whitestar checksum
# =============================================================================
#
# The Whitestar boot self-test ($9F62 in LOTR) banks each page $38-$3F into the
# $4000-$7FFF window and accumulates every byte into an 8-bit register B
# (ADDB ,X+). Pages $3E/$3F alias the fixed $8000-$FFFF region, so the result is
# the 8-bit sum of the ENTIRE CPU ROM. It then:
#     LDX $FFEE ; CMPX #$FFFF ; BEQ pass   ; word $FFEE == $FFFF -> skip the test
#     CMPB #$FF ; BEQ pass                 ; else the 8-bit byte-sum must be $FF
# So: a correct ROM has (sum of all bytes) & 0xFF == 0xFF, and writing $FFFF at
# $FFEE disables the check entirely. (Verified: the factory lotrcpua.a00 sums to
# 0xFF, and $FFEE = 0x84FF -> enforced.) This is unrelated to the WPC delta-word
# scheme above. See lotr notes/21.

WHITESTAR_CKSUM_TARGET = 0xFF  # required value of (sum of all bytes) & 0xFF


def whitestar_update_checksum(rom: bytearray) -> None:
    """Make the 8-bit byte-sum of the ROM equal 0xFF by tweaking one pad byte.

    Absorbs the patch's effect on the sum into a single unused (0xFF) padding
    byte so no real code/data changes. No-op if the sum is already correct."""
    s = sum(rom) & 0xFF
    if s == WHITESTAR_CKSUM_TARGET:
        ok(f"Checksum: 8-bit sum already 0x{s:02X}  [OK, no fixup needed]")
        return
    # Changing a byte currently 0xFF to v shifts the sum by (v - 0xFF); pick v so
    # the total lands on 0xFF:  v = (0xFE - s) & 0xFF.
    v = (0xFE - s) & 0xFF
    # Find a trailing 0xFF padding byte to use as the adjuster. Stay below $FFEC
    # so we never touch the checksum word ($FFEE), the WPC-style delta slot
    # ($FFEC), or the 6809 vectors ($FFF0-$FFFF) — keeps the stored checksum
    # word stable and can't accidentally form the $FFFF "disabled" sentinel.
    sys_base = len(rom) - SYS_SIZE
    vec_off = sys_base + (0xFFEC - 0x8000)
    pad_off = None
    for off in range(min(vec_off, len(rom)) - 1, -1, -1):
        if rom[off] == 0xFF:
            pad_off = off
            break
    if pad_off is None:
        die("Whitestar checksum: no spare 0xFF padding byte found to absorb the "
            "correction. Free a byte or pass --disable-checksum.")
    rom[pad_off] = v
    verify = sum(rom) & 0xFF
    if verify != WHITESTAR_CKSUM_TARGET:
        die(f"Whitestar checksum verify failed: got 0x{verify:02X}, "
            f"want 0x{WHITESTAR_CKSUM_TARGET:02X}.")
    ok(f"Checksum: 8-bit sum -> 0x{verify:02X} via pad byte @ 0x{pad_off:05X} "
       f"(0xFF -> 0x{v:02X})  [OK]")


def whitestar_disable_checksum(rom: bytearray) -> None:
    """Write 0xFFFF at $FFEE so the boot self-test skips ROM verification."""
    sys_base = len(rom) - SYS_SIZE
    off = sys_base + (0xFFEE - 0x8000)
    rom[off] = 0xFF
    rom[off + 1] = 0xFF
    warn("Checksum disabled (word $FFEE=0xFFFF). Whitestar boot will skip ROM "
         "verification. Rebuild without --disable-checksum for distribution.")


# =============================================================================
# Game ROM selection
# =============================================================================

def pick_game_rom(zf: zipfile.ZipFile, names: List[str], sizes: dict) -> Optional[str]:
    """Pick the CPU/game ROM from a (possibly multi-file) ROM zip.

    Mirrors rom.py's load_rom() selection so build.py patches the same file the
    disassembler reads: score by filename (WPC `_g`, Whitestar `cpu`; penalize
    sound/display/bios), then, among the top scorers, prefer the entry whose
    6809 reset vector (last two bytes) points into $8000-$FFFF — the structural
    fingerprint of a real CPU ROM. This is what disambiguates lotrcpua.a00 from
    the 1 MB BSMT sound ROMs in a Whitestar zip (which all share name-score 0)."""
    import re

    def score(name: str) -> int:
        n = name.lower()
        s = 0
        if "_g" in n:                                           s += 10
        if "cpu" in n:                                          s += 12
        if re.match(r"^[a-z]{2,4}s\d", n):                      s -= 10
        if any(x in n for x in ("sound", "snd", "dcs", "voic", "speech")): s -= 8
        if any(x in n for x in ("dsp", "disp", "dmd")):         s -= 8
        if "bios" in n:                                         s -= 20
        return s

    cands = sorted((n for n in names if sizes[n] in VALID_SIZES),
                   key=score, reverse=True)
    if not cands:
        return None
    for n in cands:
        data = zf.read(n)
        if (data[-2] << 8 | data[-1]) >= 0x8000:
            return n
    return cands[0]


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    bootstrap_venv()  # re-exec under the toolkit venv if not already there
    load_config()  # --deploy needs VPINMAME_DIR / PINMAME_DIR from config.env
    ap = argparse.ArgumentParser(
        description="Apply JSON patch specs to a WPC ROM zip and produce a patched zip.")
    ap.add_argument("--rom", default=None,
                    help="Game name; source is ./orig/<rom>.zip. "
                         "Default: first zip in ./orig/.")
    ap.add_argument("--rom-zip", default=None,
                    help="Source ROM zip (overrides --rom / the ./orig/ default).")
    ap.add_argument("--patch-dir", default="source/patches",
                    help="Directory of *.json patch specs. Default: ./source/patches.")
    ap.add_argument("--out-zip", default=None,
                    help="Output zip. Default: ./dist/<rom-stem>_modded.zip.")
    ap.add_argument("--disable-checksum", action="store_true",
                    help="Write delta=0x00FF instead of recomputing the real checksum.")
    ap.add_argument("--deploy", action="store_true",
                    help="Copy the output zip into VP's roms/ dir for immediate testing.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite the output zip if it already exists.")
    args = ap.parse_args()

    # --- Resolve source ROM zip ---------------------------------------------
    if args.rom_zip:
        rom_zip = Path(args.rom_zip)
        if not rom_zip.is_file():
            die(f"ROM zip not found: {rom_zip}")
    elif args.rom:
        rom_zip = Path("orig") / f"{args.rom}.zip"
        if not rom_zip.is_file():
            die(f"ROM zip for '{args.rom}' not found at {rom_zip}. "
                "Put it there, or pass --rom-zip <path>.")
    else:
        zips = sorted(Path("orig").glob("*.zip")) if Path("orig").is_dir() else []
        if not zips:
            die("No ROM zip found in ./orig/. Pass --rom-zip <path> or --rom <name>.")
        rom_zip = zips[0]
    rom_zip = rom_zip.resolve()
    rom_stem = rom_zip.stem

    # --- Resolve output zip --------------------------------------------------
    if args.out_zip:
        out_zip = Path(args.out_zip)
    else:
        out_zip = Path("dist") / f"{rom_stem}_modded.zip"
    if out_zip.exists() and not args.force:
        die(f"Output zip already exists: {out_zip}. Pass --force to overwrite.")
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    patch_dir = Path(args.patch_dir)

    # --- Load game ROM from zip ----------------------------------------------
    step(f"Loading ROM from {rom_zip}")
    with zipfile.ZipFile(rom_zip) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        sizes = {i.filename: i.file_size for i in infos}
        game_name = pick_game_rom(zf, names, sizes)
        if game_name is None:
            die(f"No valid game ROM found in {rom_zip}.")
        rom = bytearray(zf.read(game_name))
        # Read every other entry now so we can repackage after closing the zip.
        others = {n: zf.read(n) for n in names if n != game_name}
    ok(f"Game ROM: {game_name}  {len(rom) // 1024} KiB")

    # Validate page layout.
    rom_first_page(rom)

    # --- Apply patches -------------------------------------------------------
    step(f"Applying patches from {patch_dir}")
    patch_files = sorted(patch_dir.glob("*.json")) if patch_dir.is_dir() else []
    if not patch_files:
        warn(f"No *.json patch files found in {patch_dir}. Output ROM will be "
             "identical to source (checksum/disable only).")
    else:
        ok(f"{len(patch_files)} patch file(s) found.")

    for pf in patch_files:
        spec = json.loads(pf.read_text(encoding="utf-8"))

        if "address" in spec:
            offset_spec = spec["address"]
        elif "offset" in spec:
            offset_spec = spec["offset"]
        else:
            die(f"Patch '{pf.name}' has neither 'address' nor 'offset' field.")

        offset = resolve_address(rom, str(offset_spec))
        new_bytes = parse_hex(spec["new_hex"])
        n = len(new_bytes)

        # Validate old bytes if specified.
        if spec.get("old_hex"):
            expected = parse_hex(spec["old_hex"])
            if len(expected) != n:
                die(f"Patch '{pf.name}': old_hex and new_hex have different lengths.")
            actual = bytes(rom[offset:offset + n])
            if actual != expected:
                die(f"Patch '{pf.name}': byte mismatch at offset 0x{offset:05X}. "
                    f"Expected [{fmt_hex(expected)}] got [{fmt_hex(actual)}].")

        rom[offset:offset + n] = new_bytes

        name = spec.get("name", pf.stem)
        old_hex = spec.get("old_hex", "???")
        ok(f"  {name:<40} @ 0x{offset:05X}  [{old_hex}] -> [{spec['new_hex']}]")

    # --- Update checksum (platform-specific) ---------------------------------
    manifest = load_game_manifest()
    platform = (manifest or {}).get("platform", "wpc").lower()
    step(f"Checksum (platform={platform})")
    if platform == "whitestar":
        if args.disable_checksum:
            whitestar_disable_checksum(rom)
        else:
            whitestar_update_checksum(rom)
    else:
        if args.disable_checksum:
            disable_checksum(rom)
        else:
            update_checksum(rom)

    # --- Repackage zip -------------------------------------------------------
    step(f"Packaging {out_zip}")
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, bytes(rom) if name == game_name else others[name])
    out_zip = out_zip.resolve()
    ok(f"Written: {out_zip} ({round(out_zip.stat().st_size / 1024)} KB)")

    # --- Optionally deploy ---------------------------------------------------
    if args.deploy:
        step("Deploying to VP's roms/ dir")
        # VP loads ROMs by gamename from VPinMAME's roms dir: VPINMAME_DIR on
        # Windows, PINMAME_DIR on macOS.
        env_name = "VPINMAME_DIR" if IS_WIN else "PINMAME_DIR"
        rom_root = os.environ.get(env_name)
        if not rom_root:
            warn(f"{env_name} not set; skipping deploy. Run the setup skill "
                 "(setup-pinball.py) first.")
        else:
            dest = Path(rom_root) / "roms" / f"{rom_stem}_modded.zip"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(out_zip.read_bytes())
            ok(f"Copied to {dest}")
            warn(f"To test: python3 record.py --rom {rom_stem}_modded")

    # --- Summary -------------------------------------------------------------
    print("\n" + _c(_C.GREEN, "Build complete."))
    print(f"  Source:  {rom_zip}")
    print(f"  Patches: {len(patch_files)}")
    print(f"  Output:  {out_zip}")
    print()
    out_nv = out_zip.with_suffix(".nv")
    print(_c(_C.YELLOW, "Next steps:"))
    print(f"  NVRAM:    python3 init_nvram.py --rom-zip {out_zip} --force")
    print(f"  Validate: python3 replay.py --rom <name> --rom-zip {out_zip} "
          f"--session <session> --nvram {out_nv} --trace state,dmd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
