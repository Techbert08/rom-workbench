#!/usr/bin/env python3
"""
Parse PIN2DMD PAL and VNI files and print a summary of their contents.

PAL format (big-endian):
  [0]     version (uint8)
  [1:3]   num_palettes (uint16 BE)
  Palettes:
    [+0:2]  palette_index (uint16 BE)
    [+2:4]  num_colors    (uint16 BE)
    [+4]    palette_type  (uint8): 0=normal, 1=persistent_default, 2=default
    [+5:]   RGB data      (num_colors * 3 bytes)
  Mappings (num_palettes palettes consumed first):
    [+0:2]  num_mappings  (uint16 BE)
    For each mapping:
      [+0:4]  checksum      (uint32 BE) -- CRC32 of ROM frame
      [+4]    switch_mode   (uint8): 0=Palette,1=Replace,2=ColorMask,
                                     3=Event,4=Follow,5=LayeredColorMask,
                                     6=FollowReplace,7=MaskedReplace
      [+5:7]  palette_index (uint16 BE)
      [+7:11] duration_or_offset (uint32 BE)
  Masks (optional, after mappings):
    [+0]    num_masks (uint8)
    For each mask: raw bytes (size = DMD width*height/8 per plane)

VNI format (big-endian, magic "VPIN"):
  [0:4]  magic "VPIN"
  [4:6]  version  (uint16 BE)
  [6:8]  num_animations (uint16 BE)
  If version >= 2: offset table (num_animations * uint32 BE)
  For each animation: (complex, see parse_animation)
"""

import struct
from dataclasses import dataclass, field
from typing import List, Optional

SWITCH_MODES = ["Palette","Replace","ColorMask","Event",
                "Follow","LayeredColorMask","FollowReplace","MaskedReplace"]
PAL_TYPES    = {0:"normal", 1:"persistent_default", 2:"default"}


# ── PAL parser ────────────────────────────────────────────────────────────────

@dataclass
class Palette:
    index: int
    pal_type: int
    colors: List[tuple]  # list of (R,G,B)

@dataclass
class Mapping:
    checksum: int
    switch_mode: int
    palette_index: int
    duration_or_offset: int

def parse_pal(data: bytes):
    off = 0
    version     = data[off]; off += 1
    num_palettes = struct.unpack_from(">H", data, off)[0]; off += 2
    print(f"PAL version={version}, num_palettes={num_palettes}")

    palettes = []
    for _ in range(num_palettes):
        pal_idx    = struct.unpack_from(">H", data, off)[0]; off += 2
        num_colors = struct.unpack_from(">H", data, off)[0]; off += 2
        pal_type   = data[off]; off += 1
        colors = []
        for c in range(num_colors):
            r, g, b = data[off], data[off+1], data[off+2]; off += 3
            colors.append((r, g, b))
        palettes.append(Palette(index=pal_idx, pal_type=pal_type, colors=colors))

    print(f"  Palettes parsed, offset now: {off}")
    print(f"  Bytes remaining: {len(data)-off}")

    num_mappings = struct.unpack_from(">H", data, off)[0]; off += 2
    print(f"  num_mappings={num_mappings}")

    mappings = []
    for _ in range(num_mappings):
        checksum = struct.unpack_from(">I", data, off)[0]; off += 4
        mode     = data[off]; off += 1
        pal_idx  = struct.unpack_from(">H", data, off)[0]; off += 2
        dur_off  = struct.unpack_from(">I", data, off)[0]; off += 4
        mappings.append(Mapping(checksum, mode, pal_idx, dur_off))

    print(f"  Mappings parsed, offset now: {off}")
    print(f"  Bytes remaining: {len(data)-off}")

    # Optional masks
    num_masks = 0
    if off < len(data):
        num_masks = data[off]; off += 1
        print(f"  num_masks={num_masks}")

    return palettes, mappings, num_masks, version


# ── VNI parser ────────────────────────────────────────────────────────────────

@dataclass
class VniFrame:
    delay_ms: int
    hash: Optional[int]
    bit_depth: int
    width: int
    height: int
    pixels: bytes  # flattened indexed-color pixel data

@dataclass
class Animation:
    name: str
    frames: List[VniFrame] = field(default_factory=list)

def reverse_bits(b: int) -> int:
    return int(f'{b:08b}'[::-1], 2)

def decode_planes(planes_data: bytes, num_planes: int, plane_size: int, width: int, height: int) -> bytes:
    """Decode bit-plane encoded frame into indexed pixel array."""
    # Each plane is plane_size bytes; bits are reversed within each byte
    plane_bits = []
    for p in range(num_planes):
        raw = planes_data[p * plane_size : (p+1) * plane_size]
        bits = []
        for byte in raw:
            rb = reverse_bits(byte)
            for bit in range(8):
                bits.append((rb >> (7-bit)) & 1)
        plane_bits.append(bits[:width * height])

    pixels = bytearray(width * height)
    for i in range(width * height):
        val = 0
        for p in range(num_planes):
            val |= plane_bits[p][i] << p
        pixels[i] = val
    return bytes(pixels)

try:
    import heatshrink2
    HAS_HEATSHRINK = True
except ImportError:
    HAS_HEATSHRINK = False
    print("WARNING: heatshrink2 not installed; compressed frames will be skipped")

def parse_vni(data: bytes, max_anims: int = None):
    off = 0
    magic = data[0:4]; off += 4
    assert magic == b"VPIN", f"Bad VNI magic: {magic!r}"
    version    = struct.unpack_from(">H", data, off)[0]; off += 2
    num_anims  = struct.unpack_from(">H", data, off)[0]; off += 2
    print(f"\nVNI version={version}, num_animations={num_anims}")

    offsets = None
    if version >= 2:
        offsets = [struct.unpack_from(">I", data, off + i*4)[0] for i in range(num_anims)]
        off += num_anims * 4

    if max_anims is not None:
        num_anims = min(num_anims, max_anims)

    animations = []
    for anim_idx in range(num_anims):
        if offsets:
            off = offsets[anim_idx]

        name_len = struct.unpack_from(">H", data, off)[0]; off += 2
        name = data[off:off+name_len].decode("utf-8", errors="replace"); off += name_len

        # Legacy fields (deprecated but present)
        off += 2 + 2 + 2 + 1 + 1 + 2 + 2 + 2 + 1 + 1  # cycles,hold,clkfrom,clksm,clkfr,clkox,clkoy,refr,type,fsk

        num_frames = struct.unpack_from(">H", data, off)[0]; off += 2

        # Version-gated fields
        num_colors = 0
        if version >= 2:
            off += 2  # reserved
            num_colors = struct.unpack_from(">H", data, off)[0]; off += 2
            off += num_colors * 3  # embedded palette (ignored; we use PAL file)

        if version >= 3:
            off += 1  # edit mode

        width, height = 128, 32
        if version >= 4:
            width  = struct.unpack_from(">H", data, off)[0]; off += 2
            height = struct.unpack_from(">H", data, off)[0]; off += 2

        if version >= 5:
            num_masks_v = struct.unpack_from(">H", data, off)[0]; off += 2
            for _ in range(num_masks_v):
                off += 1  # locked flag
                mask_size = struct.unpack_from(">H", data, off)[0]; off += 2
                off += mask_size

        start_frame = 0
        if version >= 6:
            compiled_flag = data[off]; off += 1
            compiled_size = struct.unpack_from(">H", data, off)[0]; off += 2
            off += compiled_size
            start_frame = struct.unpack_from(">I", data, off)[0]; off += 4

        anim = Animation(name=name)
        plane_size = (width * height) // 8  # bytes per plane

        for _ in range(num_frames):
            raw_plane_size = struct.unpack_from(">h", data, off)[0]; off += 2  # may be negative
            delay_ms       = struct.unpack_from(">H", data, off)[0]; off += 2

            frame_hash = None
            if version >= 4:
                frame_hash = struct.unpack_from(">I", data, off)[0]; off += 4

            bit_depth  = data[off]; off += 1
            compressed = False
            if version >= 3:
                compressed = bool(data[off]); off += 1

            if compressed:
                comp_size = struct.unpack_from(">I", data, off)[0]; off += 4
                comp_data = data[off:off+comp_size]; off += comp_size
                if HAS_HEATSHRINK:
                    planes_raw = heatshrink2.decompress(comp_data, window_sz2=10, lookahead_sz2=5)
                else:
                    anim.frames.append(VniFrame(delay_ms, frame_hash, bit_depth, width, height, b""))
                    continue
            else:
                planes_raw_list = []
                for _ in range(bit_depth):
                    plane_marker = data[off]; off += 1
                    plane_bytes  = bytearray(data[off:off+plane_size]); off += plane_size
                    planes_raw_list.append(bytes(plane_bytes))
                planes_raw = b"".join(planes_raw_list)

            pixels = decode_planes(planes_raw, bit_depth, plane_size, width, height)
            anim.frames.append(VniFrame(delay_ms, frame_hash, bit_depth, width, height, pixels))

        animations.append(anim)
        print(f"  [{anim_idx:3d}] '{name}': {num_frames} frames, {width}x{height}, depth={bit_depth if num_frames else '?'}")

    return animations


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    with open("pin2dmd.pal", "rb") as f:
        pal_data = f.read()
    palettes, mappings, num_masks, pal_version = parse_pal(pal_data)

    print(f"\n  First 5 palettes:")
    for p in palettes[:5]:
        print(f"    pal[{p.index}] type={PAL_TYPES.get(p.pal_type,'?')} colors={len(p.colors)} first={(p.colors[0] if p.colors else 'n/a')}")

    print(f"\n  First 10 mappings:")
    for m in mappings[:10]:
        mode_name = SWITCH_MODES[m.switch_mode] if m.switch_mode < len(SWITCH_MODES) else f"mode{m.switch_mode}"
        print(f"    crc32=0x{m.checksum:08x} mode={mode_name:20s} pal={m.palette_index} dur/off={m.duration_or_offset}")

    print(f"\n  Summary: {len(palettes)} palettes, {len(mappings)} mappings, {num_masks} masks")

    print("\n" + "=" * 60)
    with open("pin2dmd.vni", "rb") as f:
        vni_data = f.read()
    animations = parse_vni(vni_data, max_anims=None)

    total_frames = sum(len(a.frames) for a in animations)
    print(f"\nVNI Summary: {len(animations)} animations, {total_frames} total frames")
