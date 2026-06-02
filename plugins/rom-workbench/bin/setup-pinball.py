#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Cross-platform one-time installer for the rom-workbench (record) toolchain.

A stdlib-only script that installs everything under a per-user data directory:

    macOS    ~/Library/Application Support/rom-workbench/
    Windows  %LOCALAPPDATA%\\rom-workbench\\
    Linux    $XDG_DATA_HOME (or ~/.local/share)/rom-workbench/

with vpinball/, pinmame/ and (Windows) vpinmame/ underneath, plus a cache/.

Run it with either Python or uv:

    python3 setup-pinball.py        # bootstraps uv if missing, then installs
    uv run  setup-pinball.py        # once uv is already available

Steps:
  1. Ensure uv is installed (the day-to-day Python tools run via `uv run`).
  2. Download + install Visual Pinball X.
  3. Deploy the prebuilt patched libpinmame from lib/ into PINMAME_DIR (replay
     loads it via ctypes; it's self-contained, so nothing to download).
     macOS:   also deploy the dylib into the VPX bundle, re-sign, Gatekeeper-check.
     Windows: also install VPinMAME COM, deploy the patched VPinMAME64.dll, and
              register it with regsvr32.
  4. Persist VPINBALL_DIR / PINMAME_DIR / (Windows) VPINMAME_DIR as user env vars.

Re-running is idempotent; pass --force to re-download / rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, NoReturn

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

SCRIPT_DIR = Path(__file__).resolve().parent          # plugins/rom-workbench/bin
PLUGIN_ROOT = SCRIPT_DIR.parent                       # plugins/rom-workbench
LIB_DIR = PLUGIN_ROOT / "lib"
PATCHES_DIR = PLUGIN_ROOT / "pinmame-patches"
REPO_ROOT = PLUGIN_ROOT.parent.parent                 # the rom-workbench checkout root

# --- Pinned releases ---------------------------------------------------------
# Trust-on-first-use SHA-256: an empty pin means the hash is recorded on the
# first download and verified on subsequent runs (see download()).

# Visual Pinball X. The tag and asset basename differ slightly on some releases.
VPX_TAG = "10.8.0-2051-28dd6c3"
VPX_WIN_ASSET = f"Developer.VPinballX-{VPX_TAG}-Release-win-x64.zip"
VPX_MAC_ASSET_TMPL = "VPinballX_GL-10.8.0-2052-5a81d4e-Release-macos-{arch}.zip"
VPX_BASE_URL = f"https://github.com/vpinball/vpinball/releases/download/v{VPX_TAG}"

# VPinMAME COM server (Windows). Supplies bass64.dll and the directory layout;
# the patched VPinMAME64.dll from lib/ overlays the stock one after extraction.
PINMAME_VERSION = "3.6.0-1227-ecd032e"
VPINMAME_WIN_ASSET = f"VPinMAME-{PINMAME_VERSION}-win-x64.zip"
PINMAME_BASE_URL = f"https://github.com/vpinball/pinmame/releases/download/v{PINMAME_VERSION}"

# Base commit the switch-recorder patches apply onto (macOS source-build fallback).
PINMAME_BASE_COMMIT = "3ef424b0a560b08b563a345d1ecd0fa733533eef"

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


def run(cmd, **kw):
    """subprocess.run with check=True by default."""
    kw.setdefault("check", True)
    return subprocess.run(cmd, **kw)


# =============================================================================
# Paths / env
# =============================================================================

def data_root(override: "str | None") -> Path:
    if override:
        return Path(override).expanduser().resolve()
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "rom-workbench"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "rom-workbench"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "rom-workbench"


def cache_dir(root: Path) -> Path:
    d = root / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _broadcast_env_change() -> None:
    """Tell already-running processes the user environment changed (Windows)."""
    try:
        import ctypes
        from ctypes import wintypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        res = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]  # Windows-only
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(res))
    except Exception:
        pass


def _set_env_rcfiles(name: str, value: str) -> None:
    """Persist `export NAME="VALUE"` into the user's shell rc files (POSIX)."""
    line = f'export {name}="{value}"'
    for rc in (Path.home() / ".zshenv", Path.home() / ".bash_profile"):
        existing = rc.read_text().splitlines() if rc.exists() else []
        kept = [ln for ln in existing if not ln.startswith(f"export {name}=")]
        kept.append(line)
        rc.write_text("\n".join(kept) + "\n")


def set_user_env(name: str, value: str) -> None:
    """Persist a user-scope env var and mirror it into the current process."""
    value = str(value)
    if IS_WIN:
        import winreg  # Windows-only stdlib module
        wr: Any = winreg
        with wr.OpenKey(wr.HKEY_CURRENT_USER, "Environment", 0,
                        wr.KEY_SET_VALUE) as key:
            wr.SetValueEx(key, name, 0, wr.REG_SZ, value)
        _broadcast_env_change()
    else:
        _set_env_rcfiles(name, value)
    os.environ[name] = value
    ok(f"{name} = {value}")


# =============================================================================
# Download (cache + trust-on-first-use SHA-256)
# =============================================================================

def _http_get(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "rom-workbench-setup"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        last = -1
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if total and sys.stdout.isatty():
                pct = read * 100 // total
                if pct != last:
                    print(f"\r    {pct:3d}%  {read >> 20} / {total >> 20} MiB",
                          end="", flush=True)
                    last = pct
    if total and sys.stdout.isatty():
        print()
    tmp.replace(dest)


def download(root: Path, url: str, filename: str,
             expected_sha: str = "", force: bool = False) -> "Path | None":
    cache = cache_dir(root)
    dest = cache / filename
    sidecar = cache / (filename + ".sha256")

    if force and dest.exists():
        dest.unlink()

    if dest.exists():
        if expected_sha:
            if sha256(dest).lower() == expected_sha.lower():
                ok(f"Checksum OK (cached {filename}).")
                return dest
            warn("Cached checksum mismatch; re-downloading.")
            dest.unlink()
        elif sidecar.exists():
            want = sidecar.read_text().strip()
            if sha256(dest).lower() == want.lower():
                ok(f"Cached {filename} matches recorded hash {want[:12]}…")
                return dest
            warn(f"Cached {filename} diverged from recorded hash; re-downloading.")
            dest.unlink()
        else:
            h = sha256(dest)
            sidecar.write_text(h)
            ok(f"Cached {filename} present; recorded hash {h[:12]}…")
            return dest

    step(f"Downloading {url}")
    try:
        _http_get(url, dest)
    except Exception as e:                       # noqa: BLE001 — surface as a clean message
        warn(f"Download failed: {e}")
        return None
    ok(f"Downloaded to {dest}")

    if expected_sha:
        if sha256(dest).lower() != expected_sha.lower():
            die(f"SHA-256 mismatch for {dest}. Expected {expected_sha}, got {sha256(dest)}.")
        sidecar.write_text(expected_sha.lower())
        ok("Checksum OK.")
    else:
        h = sha256(dest)
        sidecar.write_text(h)
        warn(f"No SHA-256 pinned for {filename}. Trust-on-first-use hash recorded: {h}")
    return dest


def extract_zip(zip_path: Path, dest: Path, strip: bool = False) -> None:
    """Extract a .zip into dest. With strip=True, a single top-level directory is
    flattened so dest holds its *contents*. Uses the system `unzip` on POSIX to
    preserve permissions/symlinks (matters for .app bundles)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if IS_WIN or shutil.which("unzip") is None:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    else:
        run(["unzip", "-q", "-o", str(zip_path), "-d", str(dest)])
    if strip:
        entries = list(dest.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            for child in inner.iterdir():
                shutil.move(str(child), str(dest / child.name))
            inner.rmdir()


# =============================================================================
# Step 1: uv
# =============================================================================

def _user_local_bin() -> Path:
    return (Path(os.environ["USERPROFILE"]) / ".local" / "bin") if IS_WIN \
        else (Path.home() / ".local" / "bin")


def ensure_uv() -> None:
    step("Checking uv")
    if shutil.which("uv"):
        ver = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        ok(f"{ver.stdout.strip() or 'uv'} at {shutil.which('uv')}")
        return

    warn("uv not found — installing from https://astral.sh/uv ...")
    if IS_WIN:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "ByPass", "-Command",
             "irm https://astral.sh/uv/install.ps1 | iex"])
    else:
        if shutil.which("curl"):
            run("curl -LsSf https://astral.sh/uv/install.sh | sh", shell=True)
        elif shutil.which("wget"):
            run("wget -qO- https://astral.sh/uv/install.sh | sh", shell=True)
        else:
            die("Neither curl nor wget is available to install uv. Install it manually "
                "(https://docs.astral.sh/uv/getting-started/installation/) and re-run.")

    # The installer drops uv under ~/.local/bin; surface it for this process.
    bindir = _user_local_bin()
    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
    if not shutil.which("uv"):
        die(f"uv installed but not on PATH. Open a new shell (or add {bindir} to PATH) "
            "and re-run.")
    ok(f"uv installed: {subprocess.run(['uv', '--version'], capture_output=True, text=True).stdout.strip()}")


# =============================================================================
# Visual Pinball X
# =============================================================================

def _find_app(root: Path, name: str) -> "Path | None":
    if (root / name).is_dir():
        return root / name
    for p in root.rglob(name):
        if p.is_dir():
            return p
    return None


def install_vpx(root: Path, force: bool) -> "Path | None":
    """Returns VPINBALL_DIR (the directory that holds the VPX executable/app)."""
    vpx_dir = root / "vpinball"

    if IS_WIN:
        exe = vpx_dir / "VPinballX64.exe"
        step(f"Visual Pinball X at {vpx_dir}")
        if exe.exists() and not force:
            ok("VPinballX64.exe present; skipping.")
            return vpx_dir
        zip_path = download(root, f"{VPX_BASE_URL}/{VPX_WIN_ASSET}", VPX_WIN_ASSET, force=force)
        if not zip_path:
            warn("VPX download failed; replay does not require VPX. Skipping.")
            return None
        extract_zip(zip_path, vpx_dir, strip=True)
        if not exe.exists():
            found = next(iter(vpx_dir.rglob("VPinballX64.exe")), None)
            if not found:
                die(f"VPX extraction did not produce VPinballX64.exe under {vpx_dir}.")
            for child in found.parent.iterdir():
                shutil.move(str(child), str(vpx_dir / child.name))
        ok("Visual Pinball X installed.")
        return vpx_dir

    # macOS
    arch = platform.machine()  # arm64 / x86_64
    app = vpx_dir / "VPinballX_GL.app"
    exe = app / "Contents" / "MacOS" / "VPinballX_GL"
    step(f"Visual Pinball X at {vpx_dir}")
    if exe.exists() and not force:
        ok("VPinballX_GL.app present; skipping.")
        return vpx_dir

    asset = VPX_MAC_ASSET_TMPL.format(arch=arch)
    zip_path = download(root, f"{VPX_BASE_URL}/{asset}", asset, force=force)
    if not zip_path:
        warn(f"VPX macOS download failed — this release may lack a macos-{arch} asset.")
        warn("Skipping VPX install; replay.py does not require VPX.")
        return None

    vpx_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        extract_zip(zip_path, tmp)
        dmg = next(iter(tmp.rglob("*.dmg")), None)
        src_app = None
        if dmg:
            info(f"Mounting {dmg.name} ...")
            with tempfile.TemporaryDirectory() as mp_dir:
                run(["hdiutil", "attach", str(dmg), "-mountpoint", mp_dir,
                     "-nobrowse", "-quiet"])
                try:
                    src_app = _find_app(Path(mp_dir), "VPinballX_GL.app")
                    if src_app:
                        if app.exists():
                            shutil.rmtree(app)
                        run(["cp", "-a", str(src_app), str(vpx_dir) + "/"])
                        src_app = app
                finally:
                    run(["hdiutil", "detach", mp_dir, "-quiet"], check=False)
        else:
            found = _find_app(tmp, "VPinballX_GL.app")
            if found:
                if app.exists():
                    shutil.rmtree(app)
                run(["cp", "-a", str(found), str(vpx_dir) + "/"])
                src_app = app

    if not exe.exists():
        warn("Extraction did not produce VPinballX_GL.app — check the asset layout.")
        return None
    exe.chmod(0o755)
    ok(f"Visual Pinball X installed at {vpx_dir}")
    return vpx_dir


# =============================================================================
# macOS: libpinmame.dylib
# =============================================================================

def _macho_arch(path: Path) -> str:
    out = subprocess.run(["file", str(path)], capture_output=True, text=True).stdout
    for a in ("arm64", "x86_64"):
        if a in out:
            return a
    return ""


def install_libpinmame_macos(root: Path, pinmame_src: Path, force: bool) -> Path:
    step("Installing patched libpinmame.dylib")
    pinmame_dir = root / "pinmame"
    pinmame_dir.mkdir(parents=True, exist_ok=True)
    target = pinmame_dir / "libpinmame.dylib"
    prebuilt = LIB_DIR / "libpinmame.dylib"
    arch = platform.machine()

    if target.exists() and not force:
        ok(f"libpinmame.dylib present at {target}; skipping.")
        return pinmame_dir

    if prebuilt.exists() and not force:
        if _macho_arch(prebuilt) == arch:
            shutil.copy2(prebuilt, target)
            ok(f"Deployed from lib/libpinmame.dylib (pre-built {arch}).")
            return pinmame_dir
        warn(f"lib/libpinmame.dylib is {_macho_arch(prebuilt) or 'unknown arch'}, "
             f"machine is {arch} — building from source.")

    _build_libpinmame_macos(pinmame_src, target, prebuilt, arch)
    return pinmame_dir


def _require_build_tools() -> None:
    for tool, hint in (("cmake", "brew install cmake"),
                       ("git", "brew install git"),
                       ("clang", "xcode-select --install")):
        if not shutil.which(tool):
            die(f"'{tool}' not found (needed to build libpinmame). Install: {hint}")
    ok(f"cmake: {subprocess.run(['cmake', '--version'], capture_output=True, text=True).stdout.splitlines()[0]}")


def _build_libpinmame_macos(pinmame_src: Path, target: Path,
                            prebuilt: Path, arch: str) -> None:
    _require_build_tools()
    if not pinmame_src.is_dir():
        die(f"PinMAME source not found at {pinmame_src}.\n"
            f"    Clone it: git clone https://github.com/vpinball/pinmame.git {pinmame_src}")

    header = pinmame_src / "src" / "libpinmame" / "libpinmame.h"
    if not header.exists() or "PinmameDebugAttach" not in header.read_text(errors="ignore"):
        info(f"Applying switch-recorder patches to {pinmame_src} ...")
        branch = subprocess.run(["git", "-C", str(pinmame_src), "rev-parse",
                                 "--verify", "switch-recorder"],
                                capture_output=True, text=True)
        if branch.returncode != 0:
            if subprocess.run(["git", "-C", str(pinmame_src), "cat-file", "-e",
                               f"{PINMAME_BASE_COMMIT}^{{commit}}"]).returncode != 0:
                die(f"Base commit {PINMAME_BASE_COMMIT} not in {pinmame_src}. "
                    f"Fetch: git -C {pinmame_src} fetch origin")
            run(["git", "-C", str(pinmame_src), "checkout", "-b", "switch-recorder",
                 PINMAME_BASE_COMMIT])
        else:
            run(["git", "-C", str(pinmame_src), "checkout", "switch-recorder"])
        patches = sorted(PATCHES_DIR.glob("000*-*.patch"))
        run(["git", "-C", str(pinmame_src), "am", "--3way", *map(str, patches)])
        ok("Patches applied.")

    # cmake -S needs a CMakeLists.txt at the source root; patch 1 ships
    # CMakeLists_libpinmame.txt — copy it temporarily.
    cmake_src = pinmame_src / "CMakeLists_libpinmame.txt"
    cmake_wrapper = pinmame_src / "CMakeLists.txt"
    cleanup = False
    if not cmake_wrapper.exists():
        if not cmake_src.exists():
            die(f"CMakeLists_libpinmame.txt missing from {pinmame_src} — did the patches apply?")
        shutil.copy2(cmake_src, cmake_wrapper)
        cleanup = True

    build_dir = pinmame_src / f"build_macos_{arch}"
    info(f"cmake configure (PLATFORM=macos ARCH={arch}) ...")
    run(["cmake", "-S", str(pinmame_src), "-B", str(build_dir),
         "-DPLATFORM=macos", f"-DARCH={arch}",
         "-DBUILD_SHARED=ON", "-DBUILD_STATIC=OFF",
         "-DCMAKE_BUILD_TYPE=Release", "-Wno-dev"],
        stdout=subprocess.DEVNULL)
    nproc = str(os.cpu_count() or 4)
    info(f"cmake build ({nproc} jobs) ...")
    run(["cmake", "--build", str(build_dir), "--target", "pinmame_shared", "-j", nproc])
    if cleanup:
        cmake_wrapper.unlink(missing_ok=True)

    built = next((p for p in build_dir.glob("libpinmame*.dylib") if not p.is_symlink()), None)
    if not built:
        die(f"libpinmame*.dylib not found under {build_dir} after build.")
    syms = subprocess.run(["nm", "-gU", str(built)], capture_output=True, text=True).stdout
    if "_PinmameDebugAttach" not in syms:
        die(f"PinmameDebugAttach missing from {built} — patches may not have applied.")
    shutil.copy2(built, target)
    ok(f"Installed to {target}")
    shutil.copy2(built, prebuilt)
    ok("Updated lib/libpinmame.dylib from source build.")


def deploy_dylib_to_bundle(vpx_dir: "Path | None", dylib: Path) -> None:
    """Replace VPX's bundled libpinmame with the patched build and re-sign."""
    step("Deploying libpinmame into Visual Pinball bundle")
    search_roots = []
    if vpx_dir:
        search_roots.append(vpx_dir / "VPinballX_GL.app")
    search_roots.append(Path("/Applications/VPinballX_GL.app"))

    found = []
    for r in search_roots:
        if r.exists():
            found += [p for p in r.rglob("libpinmame*.dylib")
                      if p.is_file() and not p.name.endswith(".orig")]
    found = sorted(set(found))
    if not found:
        warn("VPX bundled libpinmame not found — skipping VPX bundle deploy.")
        warn("(replay.py only needs PINMAME_DIR; this only matters for using VPX directly.)")
        return

    for dst in found:
        backup = dst.with_suffix(dst.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(dst, backup)
            ok(f"Backed up original to {backup.name}")
        shutil.copy2(dylib, dst)
        ok(f"Patched libpinmame deployed to {dst}")
        _resign_bundle(dst)


def _resign_bundle(dylib_path: Path) -> None:
    # .../VPinballX_GL.app/Contents/Frameworks/libpinmame.dylib -> the .app
    app = dylib_path.parent.parent.parent
    if not app.name.endswith(".app"):
        return
    frameworks = dylib_path.parent
    step(f"Re-signing VPX bundle (ad-hoc): {app.name}")
    errors = 0
    for f in list(frameworks.glob("*.dylib")) + list(frameworks.glob("*.so")):
        if subprocess.run(["codesign", "--force", "--sign", "-", str(f)],
                          capture_output=True).returncode != 0:
            warn(f"Could not sign {f.name}")
            errors += 1
    exe = app / "Contents" / "MacOS" / "VPinballX_GL"
    if subprocess.run(["codesign", "--force", "--sign", "-", str(exe)],
                      capture_output=True).returncode != 0:
        warn("Could not sign VPinballX_GL executable.")
    if subprocess.run(["codesign", "--force", "--sign", "-", str(app)],
                      capture_output=True).returncode == 0:
        ok(f"Bundle re-signed: {app}")
    else:
        warn(f"Bundle re-sign failed — VPX may show 'damaged'. Try: "
             f"codesign --force --deep --sign - '{app}'")
    if errors:
        warn(f"{errors} framework(s) could not be signed (likely pre-existing).")


def gatekeeper_check(vpx_dir: "Path | None") -> None:
    if not vpx_dir:
        return
    exe = vpx_dir / "VPinballX_GL.app" / "Contents" / "MacOS" / "VPinballX_GL"
    if not exe.exists():
        return
    step("Testing VPX launch (Gatekeeper check)")

    def trial() -> bool:
        try:
            p = subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except Exception:
            return False
        try:
            p.wait(timeout=2)
            return False  # exited immediately — blocked
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
            return True

    if trial():
        ok("VPX launches successfully — Gatekeeper is happy.")
        return
    warn("Gatekeeper blocked VPX. Opening System Settings...")
    print("\n    macOS blocked the app because we replaced a dylib inside the")
    print("    notarised bundle. Allow it once:\n")
    print("      System Settings → Privacy & Security → Security")
    print("      → find 'VPinballX_GL was blocked' and click 'Open Anyway'\n")
    subprocess.run(["open",
                    "x-apple.systempreferences:com.apple.preference.security?General"],
                   check=False)
    input("    Press Enter here once you have clicked 'Open Anyway'... ")
    print()
    if trial():
        ok("VPX launches successfully after authorization.")
    else:
        warn("VPX is still being blocked. You may need: sudo spctl --master-disable")
        warn("confirm in System Settings, then re-run this script.")


# =============================================================================
# Windows: patched libpinmame (from lib/), VPinMAME COM
# =============================================================================

def install_pinmame_windows(root: Path, force: bool) -> Path:
    """Stage the prebuilt patched libpinmame into PINMAME_DIR.

    replay.py loads libpinmame.dll from here via ctypes. The library is
    self-contained (only the MSVC++ runtime + system DLLs — no bass64, no data
    files), so there's nothing to download; we deploy lib/libpinmame.dll
    directly, mirroring the macOS path."""
    pinmame_dir = root / "pinmame"
    step(f"Patched libpinmame at {pinmame_dir}")
    patched = LIB_DIR / "libpinmame.dll"
    if not patched.exists():
        die("lib/libpinmame.dll not found; replay needs it "
            "(rebuild per pinmame-patches/README.md).")
    pinmame_dir.mkdir(parents=True, exist_ok=True)
    target = pinmame_dir / "libpinmame.dll"
    if target.exists() and not force:
        ok(f"libpinmame.dll present at {target}; skipping.")
    else:
        shutil.copy2(patched, target)
        ok(f"Deployed lib/libpinmame.dll -> {target}")
    return pinmame_dir


def install_vpinmame_windows(root: Path, force: bool) -> Path:
    vpm_dir = root / "vpinmame"
    step(f"VPinMAME COM at {vpm_dir}")
    dll64 = vpm_dir / "VPinMAME64.dll"
    dll32 = vpm_dir / "VPinMAME.dll"
    if (dll64.exists() or dll32.exists()) and not force:
        ok("VPinMAME DLL present; skipping extraction.")
    else:
        z = download(root, f"{PINMAME_BASE_URL}/{VPINMAME_WIN_ASSET}", VPINMAME_WIN_ASSET, force=force)
        if not z:
            die("VPinMAME COM download failed.")
        extract_zip(z, vpm_dir, strip=True)
        if not (dll64.exists() or dll32.exists()):
            die(f"VPinMAME extraction did not produce VPinMAME(64).dll under {vpm_dir}.")
        ok("VPinMAME extracted.")

    # Deploy the patched VPinMAME64.dll (the VPINMAME_SWITCHLOG recorder).
    patched = LIB_DIR / "VPinMAME64.dll"
    if not patched.exists():
        warn("lib\\VPinMAME64.dll not found; record.py VpRecord needs it.")
    elif not dll64.exists():
        warn(f"VPinMAME64.dll not installed at {dll64}; cannot deploy patch.")
    else:
        backup = dll64.with_suffix(dll64.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(dll64, backup)
            ok(f"Backed up original to {backup.name}")
        shutil.copy2(patched, dll64)
        ok(f"Patched VPinMAME64.dll deployed to {dll64}")

    _regsvr32(dll64 if dll64.exists() else dll32, force)
    return vpm_dir


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]  # Windows-only
    except Exception:
        return False


def _vpm_registered() -> bool:
    try:
        import winreg  # Windows-only stdlib module
        wr: Any = winreg
        with wr.OpenKey(wr.HKEY_CLASSES_ROOT, "VPinMAME.Controller"):
            return True
    except OSError:
        return False


def _regsvr32(dll: Path, force: bool) -> None:
    if _vpm_registered() and not force:
        ok("VPinMAME.Controller already COM-registered.")
        return
    step("Registering VPinMAME.Controller via regsvr32")
    if not _is_admin():
        warn("regsvr32 needs Administrator. Skipping registration.")
        warn("Run this once from an elevated PowerShell, then re-run setup:")
        print(_c(_C.YELLOW, f"      Start-Process regsvr32 -Verb RunAs -ArgumentList '\"{dll}\"'"))
        return
    rs = str(Path(os.environ["WINDIR"]) / "system32" / "regsvr32.exe")
    proc = subprocess.run([rs, "/s", str(dll)])
    if proc.returncode != 0:
        die(f"regsvr32 failed (exit {proc.returncode}) on {dll}.")
    if not _vpm_registered():
        die("regsvr32 reported success but HKCR\\VPinMAME.Controller is missing.")
    ok("VPinMAME.Controller registered.")


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-time cross-platform installer for the rom-workbench toolchain.")
    ap.add_argument("--force", action="store_true",
                    help="Re-download / rebuild everything even if already present.")
    ap.add_argument("--install-root", default=None,
                    help="Override the per-user install root (default: platform app-data dir).")
    ap.add_argument("--pinmame-src", default=None,
                    help="macOS source-build fallback: path to a pinmame clone "
                         "(default: ../pinmame next to the repo root).")
    args = ap.parse_args()

    if not (IS_WIN or IS_MAC or IS_LINUX):
        die(f"Unsupported platform: {sys.platform}")

    root = data_root(args.install_root)
    root.mkdir(parents=True, exist_ok=True)
    pinmame_src = Path(args.pinmame_src).expanduser().resolve() if args.pinmame_src \
        else (REPO_ROOT.parent / "pinmame")

    ensure_uv()

    vpx_dir = install_vpx(root, args.force)
    if vpx_dir:
        set_user_env("VPINBALL_DIR", str(vpx_dir))

    if IS_WIN:
        pinmame_dir = install_pinmame_windows(root, args.force)
        set_user_env("PINMAME_DIR", str(pinmame_dir))
        vpm_dir = install_vpinmame_windows(root, args.force)
        set_user_env("VPINMAME_DIR", str(vpm_dir))
    else:
        pinmame_dir = install_libpinmame_macos(root, pinmame_src, args.force) if IS_MAC \
            else (root / "pinmame")
        if IS_MAC:
            set_user_env("PINMAME_DIR", str(pinmame_dir))
            deploy_dylib_to_bundle(vpx_dir, pinmame_dir / "libpinmame.dylib")
            gatekeeper_check(vpx_dir)

    # --- Summary -------------------------------------------------------------
    print("\n" + _c(_C.GREEN, "Toolchain setup complete."))
    print(f"  install root  = {root}")
    if vpx_dir:
        print(f"  VPINBALL_DIR  = {vpx_dir}")
    if not IS_WIN and IS_MAC:
        print(f"  PINMAME_DIR   = {root / 'pinmame'}")
    if IS_WIN:
        print(f"  PINMAME_DIR   = {root / 'pinmame'}")
        print(f"  VPINMAME_DIR  = {root / 'vpinmame'}")
    print(f"  uv            = {shutil.which('uv')}")
    print()
    if IS_WIN:
        print("Env vars set at user scope. Open a new terminal to pick them up.")
    else:
        print("Env vars written to ~/.zshenv and ~/.bash_profile.")
        print("Restart your shell or run:  source ~/.zshenv")
    print("Next: from a game working dir (ROM at ./orig/<rom>.zip, table at")
    print("./tables/<rom>.vpx), record a session, e.g.")
    print("  uv run record.py --rom congo_21")
    return 0


if __name__ == "__main__":
    sys.exit(main())
