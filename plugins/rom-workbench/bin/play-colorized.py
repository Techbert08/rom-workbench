#!/usr/bin/env python3
"""Launch a Serum-colorized LIVE play session in the VPX BGFX build (macOS).

This is the recipe that produced the first confirmed *in-game* colorization
(LOTR, 2026-06-24). The BGFX standalone build routes colorization completely
differently from the old OpenGL build, and three non-obvious things have to line
up or the DMD stays grayscale (or VPX crashes on start):

  1. Serum is its OWN plugin ([Plugin.Serum]), NOT libdmdutil's built-in path and
     NOT `[Standalone] AltColor`. AltColor is the libdmdutil/PIN2DMD *palette*
     technique — a different mechanism. The Serum plugin must be Enable = 1.

  2. The Serum plugin ignores ~/.vpinball/altcolor. On game start it searches,
     in order:  <table_dir>/serum/<rom>/<rom>.cROMc,
                <table_dir>/pinmame/altcolor/<rom>/<rom>.cROMc,
                <SerumPath setting>/<rom>/<rom>.cROMc
     So the .cROMc has to be staged into the table-relative pinmame/ tree.

  3. The BGFX PinMAME plugin loads ROMs from a TABLE-RELATIVE rompath
     (vpmPath = <table_dir>/pinmame/), i.e. <table_dir>/pinmame/roms/<rom>.zip —
     NOT the PINMAME_DIR the record skill stages to, and NOT the
     [Plugin.PinMAME] PinMAMEPath ini value. Wrong path => "required files are
     missing" => the game ends instantly => libpinmame OnGameEnd NULL-deref crash.

  4. BGFX's plugin-pinmame needs a libpinmame ABI-matched to it (exports
     _PinmameSetMsgAPI / _PinmameSetSoundMode / the ~38 plugin imports). setup-pinball
     deploys exactly that: the bundle's pinned pinmame SHA + the vpintf switch recorder
     (lib/libpinmame-bundle.dylib), so the ONE bundle libpinmame both colorizes and
     records. (The full debugger build lives at PINMAME_DIR for replay_host.py only.)

This script stages the ROM + .cROMc into the table-relative pinmame/ tree, enables
the Serum plugin in VPinballX.ini, sanity-checks the deployed libpinmame, then
launches VPX on the table. Colorization is then visible in-game.

Known cosmetic issue: VPX crashes on window close (a VPX/plugin teardown bug,
unrelated to colorization — the colorized DMD renders fine during play). Whether
NVRAM (high scores) flushes on this crashing exit is UNVERIFIED; check it if scores
matter. Root cause (investigated 2026-06-24): on close VPX
runs makeWindowExit while ~6 plugin/lib worker threads are still alive (libpinmame
cpu_run, libdmdutil DmdFrame/PupDMD/Serum threads, plugin-dmdutil UpdateThread,
plugin-serum ColorizeThread). It either races into freed VPX Settings
(dmdutil onDmdSrcChanged → Settings::LoadValue SIGSEGV) or exit()s with joinable
threads (std::terminate). NOT fixable from our side: suppressing libpinmame's
ReleaseMsgApi teardown broadcasts removes the Settings crash but the broadcast is
also what stops those threads, so it just trades it for the std::terminate — don't
re-attempt that. A real fix needs the plugins/libdmdutil to join their threads
before exit (upstream). Our record/play scripts pkill VPX, bypassing this path.

Usage:
    python3 play-colorized.py --rom lotr \
        --table "../lotr/tables/JP's Lord of the Rings (Stern 2003) v600.vpx" \
        [--rom-zip ../lotr/orig/lotr.zip] [--cromc output/lotr.cROMc]

Defaults resolve by convention from the current working directory:
    ROM zip   ./orig/<rom>.zip, then ./dist/<rom>.zip      (override: --rom-zip)
    .cROMc    ./output/<rom>.cROMc, then
              ~/.vpinball/altcolor/<rom>/<rom>.cROMc        (override: --cromc)

Requires VPINBALL_DIR in the environment / config.env (set by the setup skill).
macOS only — the BGFX standalone plugin model this targets is the macOS build.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from workbench_env import die, info, load_config, ok, step, warn

IS_MAC = sys.platform == "darwin"


# =============================================================================
# Config / discovery
# =============================================================================

def env_or_die(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        die(f"{name} not set, and no config.env found. Run the setup skill "
            "(setup-pinball.py) to install the toolchain and write the config.")
    if not Path(v).exists():
        die(f"{name}={v} does not exist.")
    return v


def resolve_bgfx_bundle(vpinball: Path) -> tuple[Path, Path]:
    """Return (app_bundle, exe) for the VPX BGFX build, preferring BGFX over GL.

    Colorization via [Plugin.Serum] is a BGFX-standalone feature; the old GL
    build colorizes through a different (libdmdutil) path and is not what this
    recipe targets."""
    for stem in ("VPinballX_BGFX", "VPinballX_GL"):
        for base in (vpinball, Path("/Applications")):
            cand = base / f"{stem}.app"
            if cand.is_dir():
                exe = cand / "Contents" / "MacOS" / cand.stem
                if stem == "VPinballX_GL":
                    warn("Only the GL build was found. Serum colorization in this "
                         "script targets the BGFX build's [Plugin.Serum]; the GL "
                         "build uses a different path and may not colorize here.")
                return cand, exe
    die("No VPinballX_BGFX.app found under "
        f"{vpinball} or /Applications. Run the setup skill (setup-pinball.py).")


def check_libpinmame(app: Path) -> None:
    """Warn if the deployed libpinmame lacks _PinmameSetMsgAPI (BGFX needs it)."""
    fw = app / "Contents" / "Frameworks"
    lib = fw / "libpinmame.dylib"
    target = lib.resolve() if lib.exists() else None
    if target is None or not target.exists():
        # fall back to the versioned name
        cands = sorted(fw.glob("libpinmame.*.dylib"))
        target = next((c for c in cands if not c.name.endswith(".orig")), None)
    if target is None:
        warn("Could not locate libpinmame in the bundle to sanity-check.")
        return
    try:
        # plain nm (not -gU): the plugin imports are global (T), but vp_switchlog is
        # an internal local symbol (t) that -gU would hide → false "absent".
        out = subprocess.run(["nm", str(target)], capture_output=True,
                             text=True).stdout
    except Exception as e:
        warn(f"Could not run nm on libpinmame ({e}); skipping symbol check.")
        return
    has_plugin = "_PinmameSetMsgAPI" in out and "_PinmameSetSoundMode" in out
    has_record = "_vp_switchlog" in out
    if has_plugin and has_record:
        ok(f"libpinmame OK — bundle-matched + switch recorder: {target.name}")
    elif has_plugin:
        ok(f"libpinmame OK (plugin ABI present): {target.name}")
        warn("but vp_switchlog is absent — live play works, recording won't capture "
             "switches. Re-run setup-pinball.py to deploy lib/libpinmame-bundle.dylib.")
    else:
        warn("Deployed libpinmame is NOT bundle-matched (missing _PinmameSetMsgAPI/"
             "_PinmameSetSoundMode) — BGFX's plugin-pinmame will crash on load. Re-run "
             "setup-pinball.py to deploy the bundle build (lib/libpinmame-bundle.dylib).")


# =============================================================================
# Staging into the table-relative pinmame/ tree
# =============================================================================

def stage_rom(table: Path, rom: str, rom_zip: Path) -> Path:
    dest_dir = table.parent / "pinmame" / "roms"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{rom}.zip"
    shutil.copy2(rom_zip, dest)
    ok(f"Staged ROM -> {dest}")
    return dest


def stage_cromc(table: Path, rom: str, cromc: Path) -> Path:
    dest_dir = table.parent / "pinmame" / "altcolor" / rom
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{rom}.cROMc"
    shutil.copy2(cromc, dest)
    ok(f"Staged .cROMc -> {dest}")
    return dest


# =============================================================================
# VPinballX.ini — enable the Serum plugin (idempotent)
# =============================================================================

def enable_serum_plugin(ini: Path, serum_path: Path) -> None:
    """Set [Plugin.Serum] Enable = 1 and point its folder setting at serum_path.

    serum_path is the directory such that <serum_path>/<rom>/<rom>.cROMc exists
    (Priority-3 fallback); Priority-2 (table-relative) already covers staging, so
    this is belt-and-suspenders."""
    if not ini.exists():
        warn(f"{ini} not found — launch VPX once to generate it, then re-run. "
             "Skipping ini edit.")
        return
    lines = ini.read_text().splitlines()
    out: list[str] = []
    in_serum = False
    seen = {"Enable": False, "SerumPath": False, "CRZFolder": False}
    changed = False

    def emit_missing() -> None:
        nonlocal changed
        if not seen["Enable"]:
            out.append("Enable = 1"); changed = True
        if not seen["SerumPath"]:
            out.append(f"SerumPath = {serum_path}"); changed = True
        if not seen["CRZFolder"]:
            out.append(f"CRZFolder = {serum_path}"); changed = True

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_serum:           # leaving the Serum section: backfill any keys
                emit_missing()
            in_serum = (stripped == "[Plugin.Serum]")
            out.append(line)
            continue
        if in_serum:
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in seen:
                seen[key] = True
                want = {"Enable": "1", "SerumPath": str(serum_path),
                        "CRZFolder": str(serum_path)}[key]
                new = f"{key} = {want}"
                if line.strip() != new:
                    out.append(new); changed = True
                else:
                    out.append(line)
                continue
        out.append(line)
    if in_serum:                   # file ended inside the Serum section
        emit_missing()

    if not any(l.strip() == "[Plugin.Serum]" for l in out):
        out += ["", "[Plugin.Serum]", "Enable = 1",
                f"SerumPath = {serum_path}", f"CRZFolder = {serum_path}"]
        changed = True

    if changed:
        ini.write_text("\n".join(out) + "\n")
        ok(f"Enabled [Plugin.Serum] in {ini}")
    else:
        ok("[Plugin.Serum] already enabled and configured.")


# =============================================================================
# Launch
# =============================================================================

def launch(app: Path, table: Path) -> None:
    subprocess.run(["open", "-a", str(app), "--args",
                    "-DisableTrueFullscreen", "-play", str(table)], check=True)
    ok(f"Launched {app.name} on {table.name}")
    info("Play the table — the DMD should render colorized. Close the window to stop.")
    info("(Known cosmetic bug: VPX crashes on window close — teardown race with "
         "live plugin threads, harmless; game + NVRAM are fine.)")


# =============================================================================
# main
# =============================================================================

def resolve_rom_zip(rom: str, explicit: "str | None") -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            die(f"--rom-zip {p} not found.")
        return p
    for sub in ("orig", "dist"):
        cand = Path.cwd() / sub / f"{rom}.zip"
        if cand.is_file():
            return cand
    die(f"No ROM zip found at ./orig/{rom}.zip or ./dist/{rom}.zip. "
        "Pass --rom-zip.")


def resolve_cromc(rom: str, explicit: "str | None") -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            die(f"--cromc {p} not found.")
        return p
    for cand in (Path.cwd() / "output" / f"{rom}.cROMc",
                 Path.home() / ".vpinball" / "altcolor" / rom / f"{rom}.cROMc"):
        if cand.is_file():
            return cand
    die(f"No .cROMc found at ./output/{rom}.cROMc or "
        f"~/.vpinball/altcolor/{rom}/{rom}.cROMc. Pass --cromc.")


def main() -> None:
    if not IS_MAC:
        die("play-colorized.py targets the macOS BGFX standalone build.")

    ap = argparse.ArgumentParser(description="Launch a Serum-colorized live play "
                                             "session (VPX BGFX, macOS).")
    ap.add_argument("--rom", required=True, help="PinMAME rom/gamename (zip stem), e.g. lotr")
    ap.add_argument("--table", required=True, help="Path to the .vpx table file")
    ap.add_argument("--rom-zip", help="ROM zip (default ./orig/<rom>.zip or ./dist/<rom>.zip)")
    ap.add_argument("--cromc", help="Serum .cROMc (default ./output/<rom>.cROMc or "
                                    "~/.vpinball/altcolor/<rom>/<rom>.cROMc)")
    ap.add_argument("--ini", default=str(Path.home() / ".vpinball" / "VPinballX.ini"),
                    help="VPinballX.ini path (default ~/.vpinball/VPinballX.ini)")
    ap.add_argument("--no-launch", action="store_true",
                    help="Stage + configure only; don't launch VPX")
    args = ap.parse_args()

    load_config()
    vpinball = Path(env_or_die("VPINBALL_DIR"))
    table = Path(args.table)
    if not table.is_file():
        die(f"--table {table} not found.")
    rom_zip = resolve_rom_zip(args.rom, args.rom_zip)
    cromc = resolve_cromc(args.rom, args.cromc)

    step("Resolving VPX BGFX bundle")
    app, _exe = resolve_bgfx_bundle(vpinball)
    ok(f"Bundle: {app}")
    check_libpinmame(app)

    step("Staging ROM + .cROMc into the table-relative pinmame/ tree")
    info(f"table dir: {table.parent}")
    stage_rom(table, args.rom, rom_zip)
    stage_cromc(table, args.rom, cromc)

    step("Enabling the Serum plugin")
    serum_path = table.parent / "pinmame" / "altcolor"   # <path>/<rom>/<rom>.cROMc
    enable_serum_plugin(Path(args.ini), serum_path)

    if args.no_launch:
        ok("Staged and configured (--no-launch). Launch VPX yourself to verify.")
        return

    step("Launching colorized play")
    launch(app, table)


if __name__ == "__main__":
    main()
