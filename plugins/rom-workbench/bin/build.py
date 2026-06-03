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

from workbench_env import _C, _c, die, load_config, ok, step, warn

IS_WIN = os.name == "nt"


# =============================================================================
# WPC ROM geometry
# =============================================================================

# WPC ROM size -> first page number (matches WPCLoader.java).
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
# Game ROM selection
# =============================================================================

def pick_game_rom(names: List[str], sizes: dict) -> Optional[str]:
    """Of the entries with a valid ROM size, pick the most game-ROM-like by name."""
    import re

    def score(name: str) -> int:
        n = name.lower()
        s = 0
        if re.search(r"_g", n):                s += 10
        if re.search(r"^[a-z]{2,4}s\d", n):    s -= 10
        if re.search(r"sound|snd|dcs", n):     s -= 5
        return s

    candidates = [n for n in names if sizes[n] in VALID_SIZES]
    if not candidates:
        return None
    return max(candidates, key=score)


# =============================================================================
# Main
# =============================================================================

def main() -> int:
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
        game_name = pick_game_rom(names, sizes)
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

    # --- Update checksum -----------------------------------------------------
    step("Checksum")
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
