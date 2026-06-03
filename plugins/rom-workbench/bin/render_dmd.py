#!/usr/bin/env python3
"""Render captured DMD frames from a replay output dir to PNG.

A `replay.py --trace dmd` run writes one raw `.bin` per frame under
`<OutDir>/dmd/NNNNNN.bin` plus metadata in `<OutDir>/dmd.index.jsonl`.
Each frame is 1 byte per pixel of 8-bit luminance — libpinmame quantises
the underlying WPC 2-bit DMD values up to 0/85/170/255-ish for portability.

This helper renders selected frames (or a range) to PNG using Pillow,
upscaling with nearest-neighbour so the dot grid stays crisp.

Usage:
    python3 render_dmd.py <replay-out-dir> [--frames 0,5,10-20] [--scale 4] [--out <dir>]

If --frames is omitted, every frame is rendered.
If --out is omitted, PNGs go to <replay-out-dir>/dmd_png/.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from PIL import Image
except ImportError as e:
    raise SystemExit(
        "Pillow is required for DMD rendering. Install it with "
        "`pip install pillow` (the `setup` skill does this for you), then re-run."
    ) from e


def parse_frames(spec: str) -> list[int]:
    """Parse '0,5,10-20' into [0, 5, 10, 11, ..., 20]."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def read_layout(index_path: Path) -> tuple[int, int]:
    """Read the first frame record from dmd.index.jsonl for width/height."""
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("kind") == "dmd":
                return int(rec["width"]), int(rec["height"])
    raise ValueError(f"No dmd record found in {index_path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("replay_dir", type=Path,
                    help="A replay output directory (containing dmd/ and dmd.index.jsonl)")
    ap.add_argument("--frames", default=None,
                    help="Comma-separated frame IDs / ranges (e.g. '0,5,10-20'). Default: all.")
    ap.add_argument("--scale", type=int, default=4,
                    help="Integer upscale factor (default 4 = 512x128 from 128x32).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir. Default: <replay-dir>/dmd_png/")
    args = ap.parse_args(argv)

    dmd_dir = args.replay_dir / "dmd"
    index   = args.replay_dir / "dmd.index.jsonl"
    if not dmd_dir.is_dir():
        raise SystemExit(f"Not a replay dir (no dmd/ inside): {args.replay_dir}")
    if not index.is_file():
        raise SystemExit(f"Missing index: {index}")

    width, height = read_layout(index)
    expected = width * height
    out_dir = args.out or (args.replay_dir / "dmd_png")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.frames:
        wanted = sorted(set(parse_frames(args.frames)))
    else:
        wanted = sorted(int(p.stem) for p in dmd_dir.glob("*.bin"))

    written = 0
    skipped = 0
    for fid in wanted:
        src = dmd_dir / f"{fid:06d}.bin"
        if not src.exists():
            skipped += 1
            continue
        buf = src.read_bytes()
        if len(buf) != expected:
            print(f"  skip frame {fid}: size {len(buf)} != expected {expected}")
            skipped += 1
            continue
        img = Image.frombytes("L", (width, height), buf)
        if args.scale != 1:
            img = img.resize((width * args.scale, height * args.scale), Image.NEAREST)
        img.save(out_dir / f"{fid:06d}.png")
        written += 1

    print(f"Rendered {written} frame(s) at {width*args.scale}x{height*args.scale} -> {out_dir}")
    if skipped:
        print(f"  ({skipped} requested frame(s) skipped: not present or wrong size)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
