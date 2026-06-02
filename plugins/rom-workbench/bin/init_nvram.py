#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Produce a freshly-reset NVRAM snapshot for a ROM zip.

Boots the given ROM zip into libpinmame from blank NVRAM, lets it run for
--duration-sec seconds (so any startup checksum-mismatch / factory-reset
cycle completes), then snapshots the resulting <rom>.nv file to --out.

The output is a reusable input to replay.py's --nvram parameter: it
removes the per-replay warm-up cost and makes a run's starting state
explicit and reproducible.

A ROM zip that changes the WPC checksum word ($FFEE) will trigger a
factory reset on first boot because the existing NVRAM stored a checksum
from the previous ROM version. Some games then sit on a "FACTORY RESET —
PRESS ENTER" prompt waiting for an acknowledgment. --ack-input accepts a
session.jsonl with the required switch press (recorded via record.py
against the same ROM); that input is replayed during the warm-up.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Produce a freshly-reset NVRAM snapshot for a ROM zip."
    )
    ap.add_argument("--rom-zip", type=Path, required=True,
                    help="ROM zip to initialise NVRAM for.")
    ap.add_argument("--rom", default="",
                    help="PinMAME ROM identifier (e.g. congo_21). "
                         "Default: zip stem with trailing _modded/_mod stripped.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output NVRAM path. Default: <dir-of-rom-zip>/<zip-stem>.nv.")
    ap.add_argument("--ack-input", type=Path, default=None,
                    help="Optional session.jsonl whose switch events should be "
                         "replayed during warm-up to dismiss a reset prompt.")
    ap.add_argument("--duration-sec", type=float, default=90.0,
                    help="Wall-clock cap on the warm-up. Default 90.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite --out if it already exists.")
    return ap.parse_args()


def env_var(name: str) -> str:
    v = os.environ.get(name) or ""
    if not v:
        raise SystemExit(
            f"{name} not set. Run the setup skill (setup-pinball.py) first."
        )
    return v


def main() -> int:
    args = parse_args()

    if not args.rom_zip.is_file():
        raise SystemExit(f"ROM zip not found: {args.rom_zip}")
    rom_zip = args.rom_zip.resolve()
    zip_stem = rom_zip.stem

    rom = args.rom or re.sub(r"_(modded|mod)$", "", zip_stem)

    out = args.out
    if out is None:
        out = rom_zip.parent / f"{zip_stem}.nv"
    if out.exists() and not args.force:
        raise SystemExit(f"--out exists: {out} (pass --force to overwrite).")
    out.parent.mkdir(parents=True, exist_ok=True)

    pinmame_dir = env_var("PINMAME_DIR")

    print(f"==> init-nvram: rom={rom} zip={rom_zip} out={out}")

    scratch = Path(tempfile.gettempdir()) / f"rp-initnv-{uuid.uuid4().hex}"
    roms_dir   = scratch / "roms"
    nvram_dir  = scratch / "nvram"
    out_dir    = scratch / "out"
    for d in (roms_dir, nvram_dir, out_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(rom_zip, roms_dir / f"{rom}.zip")
        print(f"    Staged ROM at {roms_dir}\\{rom}.zip")

        # Session: use --ack-input if provided, else synthesise a meta-only stub.
        if args.ack_input:
            if not args.ack_input.is_file():
                raise SystemExit(f"--ack-input not found: {args.ack_input}")
            session_path = args.ack_input.resolve()
            print(f"    Using ack input session: {session_path}")
        else:
            session_path = scratch / "stub.session.jsonl"
            meta = {"v": 1, "kind": "meta", "rom": rom, "source": "init-nvram-stub"}
            session_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
            print("    Synthesised stub session (no input events)")

        print(f"==> Booting {rom} from blank NVRAM for {args.duration_sec}s")
        host_script = Path(__file__).parent / "replay_host.py"
        cmd = [
            sys.executable, str(host_script),
            "--pinmame-dir", pinmame_dir,
            "--rom-dir",     str(roms_dir),
            "--rom",         rom,
            "--session",     str(session_path),
            "--out",         str(out_dir),
            "--nvram-dir",   str(nvram_dir),
            "--trace",       "state",
            "--max-sec",     f"{args.duration_sec:.1f}",
            "--quiet",
        ]
        rc = subprocess.call(cmd)
        if rc != 0:
            raise SystemExit(f"replay_host.py exited {rc}")

        produced_nv = nvram_dir / f"{rom}.nv"
        if not produced_nv.is_file():
            raise SystemExit(
                f"Warm-up did not produce {produced_nv}. The ROM may not have "
                f"written NVRAM in {args.duration_sec}s; try a longer "
                f"--duration-sec or provide --ack-input."
            )

        shutil.copy2(produced_nv, out)
        print(f"    Snapshot written: {out} ({out.stat().st_size} bytes)")
    finally:
        if scratch.exists():
            try:
                shutil.rmtree(scratch)
            except OSError as e:
                print(f"    [warn] could not remove scratch {scratch}: {e}",
                      file=sys.stderr)

    print()
    print("init-nvram complete.")
    print(f"  ROM:      {rom} ({rom_zip})")
    print(f"  Snapshot: {out}")
    print()
    print("Use with replay.py:")
    print(f"  uv run <skill-dir>\\replay.py --rom {rom} --rom-zip '{rom_zip}' \\")
    print(f"      --session <session-dir> --nvram '{out}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
