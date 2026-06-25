#!/usr/bin/env python3
"""Render captured DMD frames from a replay output dir to a watchable video.

A `replay.py --trace dmd` run writes one raw 8-bit-luminance `.bin` per DMD
frame under `<OutDir>/dmd/NNNNNN.bin`, plus per-frame metadata (including the
simulated-time `t`) in `<OutDir>/dmd.index.jsonl`. The DMD only emits a frame
when its contents change, so frames are spaced irregularly in time.

This tool resamples those frames onto a constant frame rate that plays back in
*real time* (each DMD frame is held until the next one's timestamp), upscales
the dot grid with nearest-neighbour, and burns in a running timecode + source
frame number so you can pause and call out an exact moment (the timecode is the
session/trace `t`, so it lines up with trace.state.jsonl and the switch log).

With `--colorize` it runs each frame through **libserum** (the Serum colorization
the `colorize` skill produces) and renders the colourised RGB output instead of the
grey dot grid — the same pixels VPX/libdmdutil would show at runtime, but headless.
This expects RAW shade-index frames (0-3 per pixel, what `replay.py --trace dmd`
captures for a 4-shade DMD), since that's what libserum's trigger hashes match; it
loads the installed bundle's libserum and the deployed `<altcolor>/<rom>/<rom>.cROMc`.

Encodes to H.264 mp4 via ffmpeg by default; falls back to an animated GIF
(Pillow only) if ffmpeg isn't found.

If the replay dir also has a `sound` trace (audio.index.jsonl +
audio/audio.s16le.raw, produced by `replay.py --trace dmd,sound`), the emulated
game audio is muxed into the mp4, time-aligned to the same `t` clock as the DMD
frames (so the boot phase, which both traces collapse onto t=0, lines up). GIF
output can't carry audio, so it's dropped there with a warning. Use --no-audio
to skip muxing.

Usage:
    python3 render_dmd_video.py <replay-out-dir> [--fps 30] [--scale 6]
        [--out <file>] [--start 0] [--end 9999] [--no-timecode] [--no-audio]
        [--gif]

Defaults: 30 fps, scale 6 (128x32 -> 768x192), out=<replay-dir>/dmd.mp4.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from workbench_env import bootstrap_venv, load_config, load_game_manifest

bootstrap_venv()  # re-exec under the toolkit venv (where Pillow lives) before importing it

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:
    raise SystemExit(
        "Pillow is required. Install it with `pip install pillow` "
        "(the `setup` skill does this for you), then re-run."
    ) from e


# --- libserum (only used in --colorize mode) ---------------------------------

class SerumFrame(ctypes.Structure):
    """Mirror of libserum's `Serum_Frame_Struc` (serum.h). Field order/types must
    match exactly; ctypes applies the same natural alignment as the C compiler.
    Pointers are c_void_p (8 bytes) regardless of pointee type. For a V1
    colorization the colorized output is in `frame` (w*h palette indices 0-63) and
    `palette` (64 RGB triples)."""
    _fields_ = [
        ("frame", ctypes.c_void_p), ("palette", ctypes.c_void_p),
        ("rotations", ctypes.c_void_p), ("frame32", ctypes.c_void_p),
        ("width32", ctypes.c_uint32), ("rotations32", ctypes.c_void_p),
        ("rotationsinframe32", ctypes.c_void_p), ("modifiedelements32", ctypes.c_void_p),
        ("frame64", ctypes.c_void_p), ("width64", ctypes.c_uint32),
        ("rotations64", ctypes.c_void_p), ("rotationsinframe64", ctypes.c_void_p),
        ("modifiedelements64", ctypes.c_void_p), ("SerumVersion", ctypes.c_uint32),
        ("flags", ctypes.c_uint8), ("nocolors", ctypes.c_uint32),
        ("ntriggers", ctypes.c_uint32), ("triggerID", ctypes.c_uint32),
        ("frameID", ctypes.c_uint32), ("rotationtimer", ctypes.c_uint32),
    ]


_LOG_KEEP = None  # the log-callback thunk MUST stay referenced or libserum segfaults


def resolve_libserum(explicit: str | None) -> Path:
    """Find the libserum dylib: --lib if given, else the installed VPX bundle's
    (BGFX preferred, GL fallback). The setup skill swaps our v7-capable build into
    the bundle, keeping its versioned filename — so prefer the real versioned file
    over the symlink/.orig backup."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise SystemExit(f"--lib not found: {p}")
        return p
    vp = os.environ.get("VPINBALL_DIR")
    if not vp:
        raise SystemExit("VPINBALL_DIR not set (run the setup skill) and no --lib given.")
    for app in ("VPinballX_BGFX.app", "VPinballX_GL.app"):
        fw = Path(vp) / app / "Contents" / "Frameworks"
        cands = sorted(fw.glob("libserum*.dylib"))
        real = [c for c in cands if c.is_file() and not c.is_symlink()
                and not c.name.endswith(".orig")]
        if real:
            return real[0]
        if cands:
            return cands[0]
    raise SystemExit(f"No libserum*.dylib under {vp}/*/Contents/Frameworks (run setup, or pass --lib).")


def load_serum(lib_path: Path, altcolor: Path, rom: str):
    """dlopen libserum, silence its logging, Serum_Load the rom. Returns (lib, *SerumFrame)."""
    global _LOG_KEEP
    lib = ctypes.CDLL(str(lib_path))
    cb = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)
    _LOG_KEEP = cb(lambda fmt, args, ud: None)
    try:
        lib.Serum_SetLogCallback.argtypes = [cb, ctypes.c_void_p]
        lib.Serum_SetLogCallback.restype = None
        lib.Serum_SetLogCallback(_LOG_KEEP, None)
    except AttributeError:
        pass  # older libserum without the log callback
    lib.Serum_Load.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint8]
    lib.Serum_Load.restype = ctypes.POINTER(SerumFrame)
    pserum = lib.Serum_Load(str(altcolor).encode(), rom.encode(), 0)
    if not pserum:
        raise SystemExit(f"Serum_Load returned NULL — no loadable cROMc at "
                         f"{altcolor}/{rom}/{rom}.cROMc (or libserum can't read its format).")
    lib.Serum_Colorize.argtypes = [ctypes.c_char_p]
    lib.Serum_Colorize.restype = ctypes.c_uint32
    return lib, pserum


def colorize_frame(dmd_dir: Path, fid: int, w: int, h: int, lib, s) -> Image.Image | None:
    """Colorize one RAW (0-3) frame via libserum; return a small (w x h) RGB image.
    Must be called in source-frame order so libserum's sequential state is correct."""
    src = dmd_dir / f"{fid:06d}.bin"
    if not src.exists():
        return None
    data = src.read_bytes()
    if len(data) != w * h:
        return None
    lib.Serum_Colorize(ctypes.create_string_buffer(data, w * h))
    idx_px = ctypes.string_at(s.frame, w * h)       # palette indices 0-63
    pal = ctypes.string_at(s.palette, 192)          # 64 RGB triples
    img = Image.frombytes("P", (w, h), idx_px)
    img.putpalette(pal + bytes(768 - len(pal)))     # pad to 256*3
    return img.convert("RGB")


def read_index(index_path: Path) -> tuple[int, int, list[tuple[int, float]]]:
    """Return (width, height, [(frame, t), ...] sorted by frame)."""
    width = height = None
    frames: list[tuple[int, float]] = []
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("kind") != "dmd":
                continue
            if width is None:
                width, height = int(rec["width"]), int(rec["height"])
            frames.append((int(rec["frame"]), float(rec.get("t", -1.0))))
    if width is None:
        raise SystemExit(f"No dmd frames found in {index_path}")
    frames.sort(key=lambda ft: ft[0])
    return width, height, frames


def read_audio_index(replay_dir: Path) -> dict | None:
    """Read a `sound` trace if present. Returns a dict with the raw-PCM path,
    sample_rate, channels, and the [(t, sample_offset), ...] chunk timeline
    (used to map a video `t` onto a sample position via the shared sim clock),
    or None if no usable audio trace exists."""
    idx = replay_dir / "audio.index.jsonl"
    raw = replay_dir / "audio" / "audio.s16le.raw"
    if not idx.is_file() or not raw.is_file() or raw.stat().st_size == 0:
        return None
    sample_rate = 0.0
    channels = 0
    total_samples = None
    chunks: list[tuple[float, int]] = []
    with idx.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "audio_format":
                sample_rate = float(rec.get("sample_rate") or 0.0)
                channels = int(rec.get("channels") or 0)
            elif kind == "audio":
                chunks.append((float(rec["t"]), int(rec["sample_offset"])))
            elif kind == "trace_end":
                total_samples = int(rec.get("total_samples") or 0) or None
                if rec.get("sample_rate"):
                    sample_rate = float(rec["sample_rate"])
                if rec.get("channels"):
                    channels = int(rec["channels"])
    if sample_rate <= 0 or channels <= 0 or not chunks:
        return None
    chunks.sort(key=lambda c: c[1])  # by sample_offset (monotonic == arrival order)
    if total_samples is None:
        total_samples = chunks[-1][1]
    return {
        "raw": raw, "sample_rate": sample_rate, "channels": channels,
        "chunks": chunks, "total_samples": total_samples,
    }


def sample_at_time(audio: dict, t: float) -> int:
    """Map a video-timeline `t` (s) to a per-channel sample position in the raw
    PCM, using the chunk timeline. Within a chunk we interpolate at the sample
    rate; before the first chunk -> 0; after the last -> total_samples. Because
    boot-phase chunks are all stamped t=0 (same as boot DMD frames), querying
    t=0 lands at the end of the boot audio — matching how the DMD resampler
    collapses boot onto a single starting frame."""
    chunks = audio["chunks"]
    sr = audio["sample_rate"]
    # Largest index whose chunk-t <= t (chunks are sorted by offset, and t is
    # non-decreasing with offset, so a linear/bisect scan on t is valid).
    lo, hi = 0, len(chunks)
    while lo < hi:
        mid = (lo + hi) // 2
        if chunks[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if i < 0:
        return 0
    ct, coff = chunks[i]
    pos = coff + int(round(max(0.0, t - ct) * sr))
    return max(0, min(pos, audio["total_samples"]))


def load_frame(dmd_dir: Path, fid: int, w: int, h: int) -> Image.Image | None:
    src = dmd_dir / f"{fid:06d}.bin"
    if not src.exists():
        return None
    buf = src.read_bytes()
    if len(buf) != w * h:
        return None
    return Image.frombytes("L", (w, h), buf)


def _font(px: int):
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        # Older Pillow: load_default() has no size arg.
        return ImageFont.load_default()


def compose(img: Image.Image, scale: int, t: float, fid: int,
            timecode: bool) -> Image.Image:
    """Upscale a DMD frame and optionally append a timecode strip below it.
    Preserves the input mode — "L" for grayscale, "RGB" for colorized frames."""
    mode = "RGB" if img.mode == "RGB" else "L"
    bg = (0, 0, 0) if mode == "RGB" else 0
    fg = (255, 255, 255) if mode == "RGB" else 255
    big = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    if not timecode:
        return big.convert(mode)
    strip_h = max(12, 4 * scale)
    if strip_h % 2:
        strip_h += 1
    canvas = Image.new(mode, (big.width, big.height + strip_h), bg)
    canvas.paste(big, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((4, big.height + 2), f"t={t:7.2f}s   f={fid:05d}",
              fill=fg, font=_font(max(11, strip_h - 6)))
    return canvas


def resample(frames: list[tuple[int, float]], fps: int,
             start: float, end: float) -> list[tuple[int, float]]:
    """Build [(source_frame, output_time), ...] at a constant fps, holding each
    DMD frame until the next one's timestamp."""
    timed = [(fid, t) for fid, t in frames if t >= 0.0]
    if not timed:
        # No usable timestamps: fall back to one output frame per source frame.
        return [(fid, i / fps) for i, (fid, _t) in enumerate(frames)]
    t0 = max(start, timed[0][1])
    t_end = min(end, timed[-1][1])
    if t_end <= t0:
        t_end = t0 + (len(timed) / fps)
    out: list[tuple[int, float]] = []
    idx = 0
    n_ticks = int((t_end - t0) * fps) + 1
    for k in range(n_ticks):
        now = t0 + k / fps
        while idx + 1 < len(timed) and timed[idx + 1][1] <= now:
            idx += 1
        out.append((timed[idx][0], now))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("replay_dir", type=Path,
                    help="A replay output dir (containing dmd/ and dmd.index.jsonl).")
    ap.add_argument("--fps", type=int, default=30, help="Output frame rate. Default 30.")
    ap.add_argument("--scale", type=int, default=6,
                    help="Integer upscale factor. Default 6 (128x32 -> 768x192).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output file. Default <replay-dir>/dmd.mp4 (or .gif with --gif).")
    ap.add_argument("--start", type=float, default=0.0, help="Clip start time (s).")
    ap.add_argument("--end", type=float, default=float("inf"), help="Clip end time (s).")
    ap.add_argument("--no-timecode", action="store_true",
                    help="Don't burn in the timecode/frame strip.")
    ap.add_argument("--no-audio", action="store_true",
                    help="Don't mux the `sound` trace audio even if present.")
    ap.add_argument("--gif", action="store_true",
                    help="Force animated GIF output instead of mp4.")
    ap.add_argument("--colorize", action="store_true",
                    help="Colorize each frame through libserum (RGB output). Expects RAW "
                         "0-3 shade-index frames and a deployed <altcolor>/<rom>/<rom>.cROMc.")
    ap.add_argument("--rom", default=None,
                    help="ROM name for --colorize (default: from ./game.json if present).")
    ap.add_argument("--altcolor", type=Path,
                    default=Path("~/.vpinball/altcolor").expanduser(),
                    help="AltColor root holding <rom>/<rom>.cROMc (default: ~/.vpinball/altcolor).")
    ap.add_argument("--lib", default=None,
                    help="libserum dylib path for --colorize (default: auto-detect VPX bundle).")
    args = ap.parse_args(argv)

    dmd_dir = args.replay_dir / "dmd"
    index = args.replay_dir / "dmd.index.jsonl"
    if not dmd_dir.is_dir() or not index.is_file():
        raise SystemExit(f"Not a replay dir with DMD frames: {args.replay_dir}")

    w, h, frames = read_index(index)
    timecode = not args.no_timecode
    schedule = resample(frames, args.fps, args.start, args.end)
    if not schedule:
        raise SystemExit("Nothing to render in the requested range.")

    # --colorize: load libserum + the rom's cROMc, then colorize every source frame
    # in fid order (so libserum's sequential state is correct) into an RGB cache.
    color_cache: dict[int, Image.Image] = {}
    if args.colorize:
        load_config()  # recover VPINBALL_DIR from config.env if not in the env
        rom = args.rom or (load_game_manifest() or {}).get("rom")
        if not rom:
            raise SystemExit("--colorize needs --rom (or a ./game.json with \"rom\").")
        lib_path = resolve_libserum(args.lib)
        lib, pserum = load_serum(lib_path, args.altcolor, rom)
        s = pserum.contents
        print(f"colorize: {lib_path.name}  rom={rom}  "
              f"(SerumVersion={s.SerumVersion}, nocolors={s.nocolors})")
        hits = 0
        for fid, _t in frames:  # frames is sorted by fid == temporal order
            img = colorize_frame(dmd_dir, fid, w, h, lib, s)
            if img is not None:
                color_cache[fid] = img
                if s.frameID not in (0xFFFFFFFF, 0xFFFFFFFE):
                    hits += 1
        print(f"colorized {len(color_cache)} frames ({hits} matched a Serum trigger)")

    use_gif = args.gif or shutil.which("ffmpeg") is None
    out = args.out or (args.replay_dir / ("dmd.gif" if use_gif else "dmd.mp4"))
    out.parent.mkdir(parents=True, exist_ok=True)

    audio = None if args.no_audio else read_audio_index(args.replay_dir)
    if audio and use_gif:
        print("Note: GIF output can't carry audio; the `sound` trace is dropped "
              "(omit --gif / install ffmpeg for an mp4 with audio).")
        audio = None

    # Cache the small per-source-frame images (many output ticks reuse one source
    # frame). Colorized frames are precomputed above; grayscale ones load lazily.
    cache: dict[int, Image.Image] = {}

    def composed(fid: int, t: float) -> Image.Image:
        if args.colorize:
            base = color_cache.get(fid) or Image.new("RGB", (w, h), (0, 0, 0))
        else:
            if fid not in cache:
                cache[fid] = load_frame(dmd_dir, fid, w, h) or Image.new("L", (w, h), 0)
            base = cache[fid]
        return compose(base, args.scale, t, fid, timecode)

    if use_gif:
        imgs = [composed(fid, t) for fid, t in schedule]
        imgs[0].save(out, save_all=True, append_images=imgs[1:],
                     duration=int(1000 / args.fps), loop=0, optimize=True)
        print(f"Wrote GIF: {out}  ({len(imgs)} frames @ {args.fps}fps)")
        return 0

    # mp4 via ffmpeg: pipe raw frames (gray8, or rgb24 when colorized) at the
    # composed dimensions.
    sample = composed(schedule[0][0], schedule[0][1])
    vw, vh = sample.size
    pix_fmt = "rgb24" if args.colorize else "gray"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pixel_format", pix_fmt,
        "-video_size", f"{vw}x{vh}", "-framerate", str(args.fps),
        "-i", "-",
    ]

    # Mux the emulated audio, sliced to the same [t0, t_end] window the video
    # covers. The raw file is one continuous interleaved s16le stream; we seek
    # to the sample position matching the first rendered frame's `t` and read
    # only the spanned duration. (-ss/-t before -i operate on the raw input.)
    audio_note = ""
    if audio:
        t0, t_end = schedule[0][1], schedule[-1][1]
        s0 = sample_at_time(audio, t0)
        s1 = sample_at_time(audio, t_end)
        sr, ch = audio["sample_rate"], audio["channels"]
        a_start = s0 / sr
        a_dur = max(0.0, (s1 - s0) / sr)
        cmd += [
            "-ss", f"{a_start:.6f}",
            "-t", f"{a_dur:.6f}",
            "-f", "s16le", "-ar", str(int(round(sr))), "-ac", str(ch),
            "-i", str(audio["raw"]),
            "-map", "0:v", "-map", "1:a",
        ]
        audio_note = f", audio {ch}ch @ {int(round(sr))}Hz"

    cmd += [
        "-vf", "format=yuv420p", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "18",
    ]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [str(out)]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for fid, t in schedule:
            proc.stdin.write(composed(fid, t).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass  # ffmpeg died; surfaced via the return code below
    rc = proc.wait()
    if rc != 0:
        raise SystemExit(f"ffmpeg exited {rc}")
    dur = schedule[-1][1] - schedule[0][1]
    print(f"Wrote mp4: {out}  ({len(schedule)} frames, {dur:.1f}s @ "
          f"{args.fps}fps, {vw}x{vh}{audio_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
