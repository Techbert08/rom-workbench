#!/usr/bin/env python3
"""
inspect_display_rom.py — structural reconnaissance of a Stern/Sega Whitestar
DMD display ROM, to judge whether DMD frames can be statically extracted.

It reports three things, each a go/no-go signal for raw extraction:
  1. Entropy map — uniformly high entropy (~7 bits/byte, low zero%) ⇒ the frame
     data is COMPRESSED (RLE/LZ); flat low-entropy regions ⇒ raw bitmaps.
  2. Pointer table — Whitestar indexes records with 3-byte big-endian offsets at
     the ROM start.  Detected by a run of increasing in-range values.
  3. Bank hint — if many table entries exceed the file size, the records are
     addressed in a banked/mapped space wider than this single chip, so offsets
     must be resolved through the memory map before decoding.

Verdict logic: raw extraction is viable only if entropy is low AND pointers are
in-range.  Otherwise you need the game's decompressor — fastest obtained by
decompiling the DMD-draw routine from the main CPU ROM (rom-workbench `ghidra`
skill) — or just render frames in-emulator (replay).

Usage:
    python3 scripts/inspect_display_rom.py --rom ../lotr/orig/cpu/lotrdspa.a00
"""

import argparse
import collections
import math


def entropy(b):
    if not b:
        return 0.0
    c = collections.Counter(b)
    n = len(b)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rom", required=True)
    ap.add_argument("--block", type=int, default=16384, help="entropy block size")
    args = ap.parse_args()
    rom = open(args.rom, "rb").read()
    n = len(rom)
    print(f"ROM: {args.rom}  size={n} (0x{n:x})")

    # 1) entropy map
    ents, zeros = [], []
    for i in range(0, n, args.block):
        blk = rom[i:i + args.block]
        ents.append(entropy(blk))
        zeros.append(blk.count(0) / len(blk))
    avg_ent = sum(ents) / len(ents)
    min_ent = min(ents)
    print(f"\nEntropy: avg={avg_ent:.2f} min={min_ent:.2f} bits/byte; "
          f"max zero%={100*max(zeros):.1f}")
    compressed = min_ent > 5.0
    print(f"  → {'COMPRESSED (no flat bitmap regions)' if compressed else 'has low-entropy regions (possible raw bitmaps)'}")

    # 2) pointer table: 3-byte BE entries from offset 0
    be24 = lambda o: (rom[o] << 16) | (rom[o + 1] << 8) | rom[o + 2]
    entries = []
    o = 0
    while o + 3 <= n:
        v = be24(o)
        if v == 0 and o > 0:
            o += 3
            continue
        entries.append(v)
        o += 3
        if len(entries) >= 4096:
            break
    in_range = sum(1 for v in entries if v < n)
    over = sum(1 for v in entries if v >= n)
    print(f"\n3-byte BE table (first {len(entries)} entries): "
          f"{in_range} in-range, {over} exceed file size")
    if over:
        ov = [v for v in entries if v >= n]
        print(f"  → BANKED layout: {over} entries point past 0x{n:x} "
              f"(e.g. 0x{ov[0]:06x}); offsets need memory-map resolution")

    # 3) verdict
    print("\n=== VERDICT ===")
    if not compressed and over == 0:
        print("  Raw static extraction looks VIABLE — try scan_rom_planes.py / "
              "template matching.")
    else:
        reasons = []
        if compressed:
            reasons.append("frames are compressed")
        if over:
            reasons.append("records use a banked address space")
        print(f"  Raw extraction NOT directly viable ({'; '.join(reasons)}).")
        print("  Need the game's DMD codec: decompile the draw routine in the "
              "main CPU ROM (rom-workbench `ghidra`), or render frames in-emulator.")


if __name__ == "__main__":
    main()
