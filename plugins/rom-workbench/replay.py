#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Headlessly replay a session against a single ROM, with selectable traces.

Single-sided runner: one ROM, one input session, one NVRAM snapshot in;
one trace directory out. To compare two runs, do two single-sided runs
and feed their output dirs to replay/diff_traces.py.

Trace features (composable via comma-separated --trace):
  state     — solenoids via OnSolenoidUpdated callback, lamps/GIs via
              per-tick delta batch APIs (PinmameGetChangedLamps/GIs).
              Default.
  dmd       — DMD frames via libpinmame OnDisplayUpdated callback.
  sound     — emulated audio (PCM) via the OnAudio* callbacks; raw s16le
              samples + a timing index. Mux into the DMD video with
              replay/render_dmd_video.py. Auto-added whenever 'dmd' is
              requested (so the video has audio); suppress with --no-sound.
  dbg       — event-driven debugger via the libpinmame Debug* API. Set
              --break-pc and/or --watch-r/--watch-w; the emulation
              thread blocks the moment a breakpoint or memory access
              fires, a worker thread captures regs to trace.dbg.jsonl,
              then resumes the CPU via Continue (or Step, with
              --dbg-step-after N). No polling, no missed hits.

Usage:
    uv run replay.py --rom congo_21 --rom-zip ./dist/congo_21_modded.zip \\
        --session ./sessions/<utc> --nvram ./orig/congo_21.nv \\
        --trace state,dbg --break-pc 0xD9A6 --dbg-step-after 80
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path


VALID_TRACES = ("state", "dmd", "sound", "dbg")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Headlessly replay a session against a single ROM."
    )
    ap.add_argument("--rom", required=True,
                    help="PinMAME ROM identifier (e.g. congo_21).")
    ap.add_argument("--rom-zip", type=Path, required=True,
                    help="ROM zip to run against (factory or modded).")
    ap.add_argument("--session", type=Path, required=True,
                    help="Session directory containing session.jsonl.")
    ap.add_argument("--nvram", type=Path, required=True,
                    help="NVRAM snapshot to seed the run (from init-nvram.py).")
    ap.add_argument("--trace", default="state",
                    help=f"Comma-separated subset of {VALID_TRACES}. Default: state. "
                         "Requesting 'dmd' auto-adds 'sound' so the rendered video has "
                         "audio; pass --no-sound to capture DMD frames only.")
    ap.add_argument("--no-sound", action="store_true",
                    help="Don't auto-add the 'sound' trace alongside 'dmd'. (Sound is "
                         "still captured if you list it explicitly in --trace.)")
    ap.add_argument("--break-pc", default="",
                    help="Comma-separated PCs to break on (dbg trace).")
    ap.add_argument("--watch-r", default="",
                    help="Comma-separated addresses for read watchpoints (dbg trace).")
    ap.add_argument("--watch-w", default="",
                    help="Comma-separated addresses for write watchpoints (dbg trace).")
    ap.add_argument("--dbg-step-after", type=int, default=0,
                    help="Single-step N instructions after each breakpoint hit.")
    ap.add_argument("--dbg-mem", default="",
                    help="Memory windows to dump on each dbg hit: comma-separated "
                         "'ADDR[:LEN]' or '@REG[+/-OFF][:LEN]' (REG in pc,s,u,x,y). "
                         "e.g. '@S:2,@X:16,0x0011'. Resolves the mapped ROM bank too.")
    ap.add_argument("--interactive", action="store_true",
                    help="Persistent GDB-like debugger session: boot to the first "
                         "--break-pc, hold the CPU frozen, and serve commands over a "
                         "TCP socket (drive with dbg.py) until 'quit'. Implies --trace dbg.")
    ap.add_argument("--dbg-port", type=int, default=47655,
                    help="TCP port for the interactive control socket. Default 47655.")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output dir. Default: <session>/replays/<rom-zip-stem>/<utc>/.")
    ap.add_argument("--sim-step", type=float, default=0.001,
                    help="Simulated seconds per loop iteration. Default 0.001.")
    ap.add_argument("--max-sec", type=float, default=600.0,
                    help="Cap on replay duration. Default 600.")
    ap.add_argument("--tail-sec", type=float, default=1.0,
                    help="Seconds to keep emulating past the last switch event so "
                         "the game settles / the DMD finishes its transition. "
                         "Default 1.0.")
    ap.add_argument("--overwrite", action="store_true",
                    help="Allow --out-dir to exist.")
    return ap.parse_args()


def env_var(name: str) -> str:
    v = os.environ.get(name) or ""
    if not v:
        import sys as _sys
        setup = "setup-pinball.sh" if _sys.platform == "darwin" else "setup-pinball.ps1"
        raise SystemExit(
            f"{name} not set. Run pinball-setup/{setup} first."
        )
    return v


def main() -> int:
    args = parse_args()

    if not args.session.is_dir():
        raise SystemExit(f"Session must be a directory: {args.session}")
    session_jsonl = args.session / "session.jsonl"
    if not session_jsonl.is_file():
        raise SystemExit(f"Missing {session_jsonl}")

    if not args.rom_zip.is_file():
        raise SystemExit(f"ROM zip not found: {args.rom_zip}")
    rom_zip = args.rom_zip.resolve()
    zip_stem = rom_zip.stem

    if not args.nvram.is_file():
        raise SystemExit(
            f"NVRAM snapshot not found: {args.nvram}. Generate one with init-nvram.py."
        )
    nvram = args.nvram.resolve()

    pinmame_dir = env_var("PINMAME_DIR")

    traces = [t.strip().lower() for t in args.trace.split(",") if t.strip()]
    if args.interactive and "dbg" not in traces:
        traces.append("dbg")  # interactive needs the Debug* API path
    # Video (the dmd trace) defaults to carrying audio: render_dmd_video.py muxes
    # the sound trace automatically, so capture it alongside dmd unless opted out.
    if "dmd" in traces and "sound" not in traces and not args.no_sound:
        traces.append("sound")
    for t in traces:
        if t not in VALID_TRACES:
            raise SystemExit(
                f"Unknown trace kind: {t}. Choose from {','.join(VALID_TRACES)}."
            )

    if args.interactive and not args.break_pc:
        raise SystemExit(
            "--interactive needs at least one --break-pc to pause at "
            "(e.g. --break-pc 0x8DB3 to stop at the RESET vector)."
        )
    if "dbg" in traces and not (args.break_pc or args.watch_r or args.watch_w):
        raise SystemExit(
            "dbg trace requested but none of --break-pc / --watch-r / --watch-w supplied."
        )

    out_dir = args.out_dir
    if out_dir is None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = args.session.resolve() / "replays" / zip_stem / stamp
    if out_dir.exists() and not args.overwrite:
        raise SystemExit(f"--out-dir exists: {out_dir} (pass --overwrite to allow).")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()

    print(f"==> OutDir: {out_dir}")
    print(f"    ROM:    {args.rom}  ({rom_zip})")
    print(f"    NVRAM:  {nvram}")

    # Stage ROM zip + seed NVRAM.
    roms_dir  = out_dir / "roms"
    nvram_dir = out_dir / "nvram"
    roms_dir.mkdir(parents=True, exist_ok=True)
    nvram_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rom_zip, roms_dir / f"{args.rom}.zip")
    shutil.copy2(nvram, nvram_dir / f"{args.rom}.nv")

    # Build the replay_host.py command.
    host_script = Path(__file__).parent / "replay" / "replay_host.py"
    cmd: list[str] = [
        sys.executable, str(host_script),
        "--pinmame-dir", pinmame_dir,
        "--rom-dir",     str(roms_dir),
        "--rom",         args.rom,
        "--session",     str(session_jsonl),
        "--out",         str(out_dir),
        "--nvram-dir",   str(nvram_dir),
        "--trace",       ",".join(traces),
        "--sim-step",    f"{args.sim_step:.4f}",
        "--max-sec",     f"{args.max_sec:.1f}",
        "--tail-sec",    f"{args.tail_sec:.1f}",
    ]
    if "dbg" in traces:
        if args.break_pc:      cmd += ["--break-pc", args.break_pc]
        if args.watch_r:       cmd += ["--watch-r",  args.watch_r]
        if args.watch_w:       cmd += ["--watch-w",  args.watch_w]
        if args.dbg_step_after > 0:
            cmd += ["--dbg-step-after", str(args.dbg_step_after)]
        if args.dbg_mem:       cmd += ["--dbg-mem", args.dbg_mem]
    if args.interactive:
        cmd += ["--interactive", "--dbg-port", str(args.dbg_port)]

    if args.interactive:
        print(f"==> replay_host: INTERACTIVE session, control port {args.dbg_port}")
        print(f"    Drive it with:  uv run {Path(__file__).parent / 'dbg.py'} "
              f"--port {args.dbg_port} <command>")
    else:
        print(f"==> replay_host: trace={','.join(traces)} max={args.max_sec}s")
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"replay_host.py exited {rc}")

    print()
    print("Replay complete.")
    print(f"  OutDir: {out_dir}")
    print()
    print("To compare against another run:")
    diff_script = Path(__file__).parent / "replay" / "diff_traces.py"
    print(f"  uv run {diff_script} \\")
    print(f"      --a <other-outdir> --b {out_dir} --out <diff-outdir>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
