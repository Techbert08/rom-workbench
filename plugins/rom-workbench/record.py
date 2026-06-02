#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Record a WPC gameplay session in Visual Pinball + VPinMAME (macOS + Windows).

Launches Visual Pinball on the configured .vpx table with VPINMAME_SWITCHLOG set
so the patched VPinMAME DLL logs every externally-driven switch *edge* to a JSONL
file; folds that stream into sessions/<utc>/session.jsonl in the format replay.py
consumes.

VP drives the playfield through the VPinMAME COM Controller, which funnels through
`vp_putSwitch`; the patched DLL logs each edge there stamped with the emulation
clock. replay.py / replay_host.py later inject those edges via PinmameSetSwitch —
the same swMatrix plane VP drove — so gameplay reproduces faithfully.

Stop recording by closing the Visual Pinball window (or Ctrl-C here); --max-sec
is a safety cap.

ROM zip and table are resolved from the working directory by convention:
    ROM zip   ./orig/<rom>.zip, then ./dist/<rom>.zip   (override: --rom-zip)
    table     ./tables/<rom>.vpx                         (override: --table)
The ROM zip is staged into VPinMAME's roms/ directory just before launch — VP
loads ROMs from there by gamename, so it has to live there at record time.

Usage:
    uv run record.py [--rom congo_21] [--rom-zip <path.zip>] [--table <path.vpx>]
                     [--out-dir <dir>] [--max-sec 600]

Requires VPINBALL_DIR and (macOS) PINMAME_DIR / (Windows) VPINMAME_DIR in the
environment — set by pinball-setup/setup-pinball.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# =============================================================================
# Console output
# =============================================================================

class _C:
    CYAN = "\033[0;36m"; GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"
    RED = "\033[0;31m"; GRAY = "\033[0;90m"; RESET = "\033[0m"


def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)  # VT processing
        except Exception:
            return False
    return True


_COLOR = _enable_ansi()


def _c(code: str, msg: str) -> str:
    return f"{code}{msg}{_C.RESET}" if _COLOR else msg


def step(msg: str) -> None: print("\n" + _c(_C.CYAN, f"==> {msg}"))
def ok(msg: str) -> None:   print("    " + _c(_C.GREEN, "ok: ") + msg)
def warn(msg: str) -> None: print("    " + _c(_C.YELLOW, "warn: ") + msg)
def info(msg: str) -> None: print("    " + _c(_C.GRAY, msg))


def die(msg: str) -> NoReturn:
    print("    " + _c(_C.RED, "error: ") + msg, file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Helpers
# =============================================================================

def env_or_die(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        die(f"{name} not set. Run pinball-setup/setup-pinball.py and open a new shell.")
    if not Path(v).exists():
        die(f"{name}={v} does not exist.")
    return v


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# Visual Pinball launch (platform-specific) — returns True if it timed out
# =============================================================================

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def launch_and_wait(vpx_app: Path, vpx_exe: Path, table: Path,
                    workdir: Path, max_sec: int) -> bool:
    """Launch VPX on the resolved table and block until it exits or max_sec.
    VPINMAME_SWITCHLOG must already be set in os.environ so the child inherits it.
    Returns True if we had to stop it on the safety timeout."""
    start = time.monotonic()

    if IS_WIN:
        proc = subprocess.Popen([str(vpx_exe), "-play", str(table)], cwd=str(workdir))
        while proc.poll() is None:
            if time.monotonic() - start >= max_sec:
                warn("MaxSeconds reached; closing Visual Pinball.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return True
            time.sleep(0.5)
        return False

    # macOS: must launch via `open -a` so LaunchServices registers the window
    # (a direct exec leaves the SDL window without focus). `open` returns at
    # once, so we find the real pid with pgrep, then poll it.
    subprocess.run(["open", "-a", str(vpx_app), "--args",
                    "-DisableTrueFullscreen", "-play", str(table)], check=True)
    pid = None
    for _ in range(10):
        out = subprocess.run(["pgrep", "-f", "VPinballX_GL"],
                             capture_output=True, text=True).stdout.split()
        if out:
            pid = int(out[0])
            break
        time.sleep(1)
    if pid is None:
        die("VPX failed to launch (no process found after 10s).")
    ok(f"VPX running (pid {pid})")
    while _alive(pid):
        if time.monotonic() - start >= max_sec:
            warn(f"MaxSeconds ({max_sec}) reached; sending SIGTERM to VPX.")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            return True
        time.sleep(1)
    return False


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record a WPC gameplay session in Visual Pinball + VPinMAME.")
    ap.add_argument("--rom", default="congo_21",
                    help="VPM gamename VPinMAME loads. Default: congo_21.")
    ap.add_argument("--rom-zip", default=None,
                    help="ROM zip. Default: ./orig/<rom>.zip, then ./dist/<rom>.zip.")
    ap.add_argument("--table", default=None,
                    help="VPX table to play. Default: ./tables/<rom>.vpx.")
    ap.add_argument("--out-dir", default=None,
                    help="Output directory. Default: ./sessions/<UTC>.")
    ap.add_argument("--max-sec", type=int, default=600,
                    help="Safety stop; recording auto-terminates after this many seconds.")
    args = ap.parse_args()

    if not (IS_WIN or IS_MAC):
        die(f"Unsupported platform: {sys.platform} (record needs macOS or Windows).")

    # --- Resolve ROM zip + table from the working dir by convention ----------
    if args.rom_zip:
        rom_zip = Path(args.rom_zip)
        if not rom_zip.is_file():
            die(f"ROM zip not found: {rom_zip}")
    else:
        rom_zip = next((p for p in (Path("orig") / f"{args.rom}.zip",
                                    Path("dist") / f"{args.rom}.zip") if p.is_file()), None)
        if rom_zip is None:
            die(f"ROM zip for '{args.rom}' not found. Put it at ./orig/{args.rom}.zip "
                f"(or ./dist/{args.rom}.zip), or pass --rom-zip <path>.")
    rom_zip = rom_zip.resolve()

    table = Path(args.table) if args.table else Path("tables") / f"{args.rom}.vpx"
    if not table.is_file():
        die(f"Table for '{args.rom}' not found at {table}. Put it at "
            f"./tables/{args.rom}.vpx, or pass --table <path>.")
    table = table.resolve()

    # --- Env / paths ---------------------------------------------------------
    vpinball = Path(env_or_die("VPINBALL_DIR"))
    # VP loads ROMs by gamename from VPinMAME's roms dir: VPINMAME_DIR on Windows,
    # PINMAME_DIR on macOS.
    rom_root = Path(env_or_die("VPINMAME_DIR" if IS_WIN else "PINMAME_DIR"))

    # Resolve the VPX executable.
    if IS_WIN:
        vpx_app = vpinball
        vpx_exe = vpinball / "VPinballX64.exe"
        if not vpx_exe.is_file():
            die(f"VPinballX64.exe not found at {vpx_exe}.")
    else:
        vpx_app = vpinball / "VPinballX_GL.app"
        if not vpx_app.is_dir():
            vpx_app = Path("/Applications/VPinballX_GL.app")
        vpx_exe = vpx_app / "Contents" / "MacOS" / "VPinballX_GL"
        if not vpx_exe.is_file():
            die(f"VPinballX_GL not found at {vpx_exe}. Run pinball-setup/setup-pinball.py, "
                "or drag VPinballX_GL.app into VPINBALL_DIR.")

    # Stage the ROM into VP's roms dir just before launch (VPinMAME loads it by
    # gamename from there). Overwrites any stale copy from a previous run.
    roms_dir = rom_root / "roms"
    roms_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rom_zip, roms_dir / f"{args.rom}.zip")
    ok(f"Staged ROM -> {roms_dir / f'{args.rom}.zip'}")

    # --- Output dir ----------------------------------------------------------
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / "sessions" / utc_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()
    session_path = out_dir / "session.jsonl"
    meta_path = out_dir / "session.meta.json"
    switch_log = out_dir / "switchlog.jsonl"

    step(f"Output: {out_dir}")
    info(f"ROM:   {args.rom} ({rom_zip})")
    info(f"Table: {table}")

    rom_sha = sha256(rom_zip)
    table_sha = sha256(table)

    # --- Write the meta line (first line of session.jsonl) -------------------
    meta = {
        "v": 1, "kind": "meta",
        "rom": args.rom,
        "rom_zip_sha256": rom_sha,
        "table_path": str(table),
        "table_sha256": table_sha,
        "mode": "VpRecord",
        "start_ts": utc_iso(),
        "end_ts": None,
        "host": platform.node(),
        "pinmame_version": None,
        "vpm_version": None,
        "comment": f"record.py VpRecord session ({'windows' if IS_WIN else 'macos'})",
        "session_jsonl": "session.jsonl",
    }
    session_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

    # --- Launch Visual Pinball ----------------------------------------------
    step("Launching Visual Pinball (VpRecord mode)")
    warn("Play, then close the Visual Pinball window to stop recording.")

    switch_log.unlink(missing_ok=True)
    # The patched DLL writes the replayable switch stream where VPINMAME_SWITCHLOG
    # points. Export it (and, on macOS, clear VPX's texture cache, which can
    # deadlock at 50% on a repeat load of the same table).
    os.environ["VPINMAME_SWITCHLOG"] = str(switch_log)
    if IS_MAC:
        cache = Path.home() / ".vpinball" / "Cache" / table.stem
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            ok(f"Cleared texture cache for {table.stem}")
    try:
        timed_out = launch_and_wait(vpx_app, vpx_exe, table, vpinball, args.max_sec)
    finally:
        os.environ.pop("VPINMAME_SWITCHLOG", None)

    # --- Fold the captured switch stream into session.jsonl ------------------
    sw_count = 0
    if switch_log.is_file():
        lines = [ln for ln in switch_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            with session_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            sw_count = len(lines)
            ok(f"Switch stream: {sw_count} events folded into session.jsonl")
        else:
            warn("switchlog.jsonl is empty — no switches were captured (did you actually play?).")
    else:
        warn("No switchlog.jsonl produced — the deployed patched DLL may predate the switch recorder,")
        warn("or VPX didn't start the ROM. Verify setup-pinball.py deployed the patched VPinMAME/libpinmame.")

    # --- Write session.meta.json --------------------------------------------
    meta_path.write_text(json.dumps({
        "rom": args.rom,
        "rom_zip_sha256": rom_sha,
        "mode": "VpRecord",
        "table_path": str(table),
        "table_sha256": table_sha,
        "session_jsonl": "session.jsonl",
        "labels": [],
        "notes": "",
    }, indent=2), encoding="utf-8")

    # --- Summary -------------------------------------------------------------
    print("\n" + _c(_C.GREEN, "Recording complete (VpRecord)."))
    print(f"  Session:  {session_path}")
    print(f"  Switches: {sw_count}")
    if timed_out:
        warn(f"Session timed out after {args.max_sec}s.")
    print()
    print("Next: uv run replay.py --rom "
          f"{args.rom} --rom-zip <zip> --session {out_dir} --nvram <nv> --trace state,dmd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
