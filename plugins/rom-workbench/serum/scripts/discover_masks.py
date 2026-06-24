#!/usr/bin/env python3
"""
discover_masks.py — reverse-engineer how PIN2DMD computes the *masked* trigger
CRC32 for ColorMask / LayeredColorMask mappings, and which of the PAL's masks
each mapping uses.

Background
──────────
The unmasked modes (Replace) hash a single 512-byte LSB-packed bit-PLANE of the
frame (see the project notes / bridge in src/pac2serum.cpp).  The mask-based
modes (ColorMask=2, LayeredColorMask=5 — ~70% of a typical PAL) instead hash the
plane *restricted to a mask region*, so an unmasked hash never matches them.

This tool brute-forces the convention the same way the unmasked scheme was
cracked: for every unique captured RAW (0–3) frame F and every PAL mask M, it
computes a masked-plane CRC32 under each candidate convention and checks it
against the set of mode-2/5 PAL trigger CRCs.  Whatever convention yields many
matches (≫ chance) is the real scheme; the per-match (mask, plane, polarity)
tells us how mappings select a mask.

Usage
─────
    python3 scripts/discover_masks.py --pal output/pin2dmd.pal \
        --frames <dir> [--frames <dir> ...] [--modes 2,5] [--max-frames N]

Each --frames dir is scanned recursively for 4096-byte .bin frames (0–3 RAW).
zlib.crc32 is bit-identical to libserum's crc32_fast.
"""

import argparse
import glob
import struct
import zlib
from collections import Counter, defaultdict

import numpy as np


# ── PAL parsing (big-endian) ───────────────────────────────────────────────────

def parse_pal(path):
    d = open(path, "rb").read()
    be16 = lambda o: struct.unpack_from(">H", d, o)[0]
    be32 = lambda o: struct.unpack_from(">I", d, o)[0]
    off = 1                       # skip version
    npal = be16(off); off += 2
    for _ in range(npal):
        off += 2                  # palette index
        nc = be16(off); off += 2
        off += 1                  # type
        off += nc * 3
    nmaps = be16(off); off += 2
    maps = []
    for _ in range(nmaps):
        crc = be32(off); off += 4
        mode = d[off]; off += 1
        pidx = be16(off); off += 2
        voff = be32(off); off += 4
        maps.append({"crc": crc, "mode": mode, "pal_idx": pidx, "vni_off": voff})
    nmasks = d[off]; off += 1
    masks = [d[off + i * 512: off + i * 512 + 512] for i in range(nmasks)]
    return maps, masks


# ── Frame loading (dedup) ──────────────────────────────────────────────────────

def load_unique_frames(dirs, limit=None):
    seen = {}
    for root in dirs:
        for p in glob.iglob(f"{root}/**/*.bin", recursive=True):
            b = open(p, "rb").read()
            if len(b) != 4096:
                continue
            if b not in seen:
                seen[b] = p
                if limit and len(seen) >= limit:
                    return seen
    return seen


# ── Plane / mask helpers (numpy, LSB bit order to match the unmasked scheme) ────

def planes_of(frame_bytes):
    """Return (plane0, plane1) each as a 512-byte uint8 array, LSB-packed."""
    a = np.frombuffer(frame_bytes, dtype=np.uint8)
    p0 = np.packbits((a & 1).astype(np.uint8), bitorder="little")
    p1 = np.packbits(((a >> 1) & 1).astype(np.uint8), bitorder="little")
    return p0, p1


def crc(arr):
    return zlib.crc32(arr.tobytes()) & 0xFFFFFFFF


# ── Main sweep ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pal", required=True)
    ap.add_argument("--frames", action="append", required=True,
                    help="dir of captured .bin frames (repeatable, recursive)")
    ap.add_argument("--modes", default="2,5",
                    help="comma-separated PAL modes to target (default 2,5)")
    ap.add_argument("--max-frames", type=int, default=None)
    args = ap.parse_args()

    target_modes = {int(x) for x in args.modes.split(",")}
    maps, masks = parse_pal(args.pal)
    target_crcs = {m["crc"]: m for m in maps if m["mode"] in target_modes}
    print(f"PAL: {len(maps)} mappings, {len(masks)} masks; "
          f"targeting modes {sorted(target_modes)} → {len(target_crcs)} CRCs")

    frames = load_unique_frames(args.frames, args.max_frames)
    print(f"Loaded {len(frames)} unique frames")

    # Precompute mask arrays in both polarities, plus a bit-reversed variant in
    # case the mask is packed MSB-first relative to the plane.
    mask_arrs = []
    for mb in masks:
        m = np.frombuffer(mb, dtype=np.uint8)
        # bit-reverse within each byte (MSB<->LSB) as an alternate packing guess
        m_rev = np.frombuffer(
            np.packbits(np.unpackbits(m, bitorder="little"), bitorder="big").tobytes(),
            dtype=np.uint8)
        mask_arrs.append((m, m_rev))

    # convention key → Counter of distinct matched target CRCs
    # association: (mask_index, plane, polarity, packing) → set of matched CRCs
    conv_hits = defaultdict(set)
    assoc = defaultdict(set)          # matched crc → set of (mask,plane,pol,pack)

    for fb in frames:
        p0, p1 = planes_of(fb)
        for plane_idx, P in ((0, p0), (1, p1)):
            for mi, (m, m_rev) in enumerate(mask_arrs):
                for pack, mm in (("lsb", m), ("rev", m_rev)):
                    # keep-where-set (P & M) and keep-where-clear (P & ~M)
                    for pol, K in (("set", mm), ("clr", np.bitwise_not(mm))):
                        c = crc(P & K)
                        if c in target_crcs:
                            key = (f"plane{plane_idx}", pol, pack)
                            conv_hits[key].add(c)
                            assoc[c].add((mi, plane_idx, pol, pack))

    print("\n=== per-(plane,polarity,packing) hit counts ===")
    if not conv_hits:
        print("  NONE — masked-plane convention not found in this set")
        return
    for key, crcs in sorted(conv_hits.items(), key=lambda kv: -len(kv[1])):
        print(f"  {key}: {len(crcs)} CRCs")

    # Deployable scheme = a (polarity, packing); the converter tries BOTH planes
    # per frame, so union plane0/plane1 for each (pol, pack).
    fam = defaultdict(set)
    for (_plane, pol_s, pack_s), crcs in conv_hits.items():
        fam[(pol_s, pack_s)] |= crcs
    print("\n=== deployable scheme (union over plane0+plane1) ===")
    for key, crcs in sorted(fam.items(), key=lambda kv: -len(kv[1])):
        print(f"  polarity={key[0]} packing={key[1]}: {len(crcs)}/{len(target_crcs)} CRCs")

    best_pol, best_pack = max(fam, key=lambda k: len(fam[k]))
    print(f"\n=== mask usage for winning scheme (pol={best_pol}, pack={best_pack}) ===")
    mask_use = Counter()
    mode_use = Counter()
    ambiguous = 0
    for c, opts in assoc.items():
        good = sorted({mi for (mi, _pl, pol, pack) in opts
                       if pol == best_pol and pack == best_pack})
        if not good:
            continue
        if len(good) > 1:
            ambiguous += 1
        mask_use[good[0]] += 1
        mode_use[target_crcs[c]["mode"]] += 1
    print("  matched CRCs per mask index:", dict(sorted(mask_use.items())))
    print("  matched CRCs per mode:", dict(mode_use))
    print(f"  total distinct target CRCs matched: {len(fam[(best_pol, best_pack)])}"
          f"/{len(target_crcs)}")
    print(f"  CRCs matched by >1 mask (ambiguous): {ambiguous}")


if __name__ == "__main__":
    main()
