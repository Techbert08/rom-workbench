#!/usr/bin/env python3
"""
scan_rom_planes.py — probe a Stern/WPC display ROM for DMD frames whose bit-plane
CRC32 matches a PIN2DMD PAL trigger, WITHOUT replaying the game.

Rationale
─────────
PIN2DMD's Replace-mode trigger CRC is the CRC32 of a single 512-byte LSB-packed
bit-plane (see scripts/discover_masks.py / the bridge).  If the display ROM
stores frames *uncompressed* in that same plane layout, then a sliding 512-byte
window over the ROM will hash to PAL trigger CRCs directly — giving trigger
frames for screens we may never reach in gameplay.

This tool slides a window over the ROM at every byte offset (and, optionally,
bit-reversed) and reports any window whose CRC32 hits a PAL trigger CRC.  A
nonzero hit count means raw extraction is viable; zero means the frames are
compressed (RLE/LZ) and a format-specific decoder is needed first.

Usage
─────
    python3 scripts/scan_rom_planes.py --pal output/pin2dmd.pal \
        --rom ../lotr/orig/lotrdspa.a00 [--window 512] [--modes 1]
"""

import argparse
import struct
import zlib


def parse_pal_crcs(path):
    d = open(path, "rb").read()
    be16 = lambda o: struct.unpack_from(">H", d, o)[0]
    be32 = lambda o: struct.unpack_from(">I", d, o)[0]
    off = 1
    npal = be16(off); off += 2
    for _ in range(npal):
        off += 2
        nc = be16(off); off += 2
        off += 1 + nc * 3
    nmaps = be16(off); off += 2
    crcs = {}
    for _ in range(nmaps):
        crc = be32(off); off += 4
        mode = d[off]; off += 1
        off += 2 + 4
        crcs.setdefault(crc, mode)
    return crcs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pal", required=True)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--window", type=int, default=512,
                    help="bytes per candidate plane (512 for 128x32)")
    ap.add_argument("--modes", default="",
                    help="restrict to PAL modes (comma list); default all")
    ap.add_argument("--step", type=int, default=1, help="offset stride")
    args = ap.parse_args()

    pal = parse_pal_crcs(args.pal)
    if args.modes:
        want = {int(x) for x in args.modes.split(",")}
        pal = {c: m for c, m in pal.items() if m in want}
    rom = open(args.rom, "rb").read()
    print(f"PAL trigger CRCs: {len(pal)} | ROM: {len(rom)} bytes "
          f"({args.rom})")

    W = args.window
    # Precompute a bit-reversal table for the 'reversed bit order' variant.
    rev = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))

    hits = []
    for off in range(0, len(rom) - W + 1, args.step):
        win = rom[off:off + W]
        c = zlib.crc32(win) & 0xFFFFFFFF
        if c in pal:
            hits.append((off, c, pal[c], "lsb"))
            continue
        cr = zlib.crc32(win.translate(rev)) & 0xFFFFFFFF
        if cr in pal:
            hits.append((off, cr, pal[cr], "bitrev"))

    print(f"\n{len(hits)} window(s) matched a PAL trigger CRC")
    for off, c, mode, order in hits[:40]:
        print(f"  off=0x{off:06x} crc={c:08x} mode={mode} bitorder={order}")
    if not hits:
        print("  → frames are not stored as raw uncompressed planes "
              "(likely RLE/LZ compressed; needs a format decoder).")


if __name__ == "__main__":
    main()
