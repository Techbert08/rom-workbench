#!/usr/bin/env python3
"""Shared config-file plumbing for the rom-workbench entrypoints.

setup-pinball.py persists the resolved install directories (VPINBALL_DIR,
PINMAME_DIR, and on Windows VPINMAME_DIR) as user-scope environment variables.
That only helps shells started *after* setup ran and re-read them; a fresh shell
— or the same shell that launched setup — often never sees them, so the other
entrypoints would die with "VPINBALL_DIR not set."

To make the toolkit self-contained, setup also drops those same values into a
small KEY=VALUE config file, and every entrypoint calls load_config() once at
startup to recover them. The file lives at a fixed, platform-default location
(see config_path) regardless of where the toolchain itself was installed, so any
entrypoint can locate it deterministically without needing an environment
variable to bootstrap.

load_config() never clobbers a variable that is already in the environment, so an
explicit shell export still wins (standard .env-loader convention) and the
legacy user-scope env vars continue to work unchanged.

The heavy artifacts (Visual Pinball, PinMAME, the cache) and a Python virtual
environment live under the plugin's persistent data directory, exposed to skill
invocations as ${CLAUDE_PLUGIN_DATA} and surviving plugin updates. That path is
not present in the ambient shell environment a tool is launched from, so setup
records it into the same config file; bootstrap_venv() reads it back and re-execs
the current process under the venv interpreter (see below) so every tool runs
with the toolkit's third-party dependency (Pillow) available, on Windows or POSIX
alike — the only OS-specific bit is Scripts\\python.exe vs bin/python.

Stdlib-only; importable as a sibling module because each entrypoint runs as
`python <tool>.py`, which puts this directory first on sys.path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

CONFIG_NAME = "config.env"

# The plugin's persistent data dir, recorded into config.env by setup and read
# back to locate the venv + installed toolchain. Skills substitute the live value
# into setup's invocation; other tools recover it from the config file.
PLUGIN_DATA_KEY = "CLAUDE_PLUGIN_DATA"

# Set on the child after bootstrap_venv() re-execs, so the re-run is a no-op and
# we never loop.
_VENV_GUARD = "ROM_WORKBENCH_IN_VENV"


# =============================================================================
# Console output (shared by every entrypoint)
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

# The variables setup records and the entrypoints consume.
MANAGED_KEYS = ("VPINBALL_DIR", "PINMAME_DIR", "VPINMAME_DIR")


def default_data_root() -> Path:
    """Platform per-user data dir for rom-workbench (ignores --install-root).

    The config file always lives under this directory, even when the toolchain
    itself was installed beneath a custom root, so any entrypoint can find it
    without consulting the environment first."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "rom-workbench"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "rom-workbench"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "rom-workbench"


def config_path() -> Path:
    return default_data_root() / CONFIG_NAME


def write_config(values: "dict[str, str]") -> Path:
    """Persist VAR=VALUE lines to the config file, replacing it wholesale."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(
        "# Written by setup-pinball.py; loaded by the rom-workbench entrypoints.\n"
        "# Edit the install or re-run setup rather than hand-editing these paths.\n"
        + body,
        encoding="utf-8",
    )
    return path


def load_config() -> None:
    """Populate os.environ from the config file for any key not already set.

    A value already present in the environment (an explicit shell export, or a
    user-scope var setup persisted) takes precedence — the file is a fallback,
    not an override. Missing/unreadable file is a no-op."""
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _config_value(key: str) -> "str | None":
    """Read a single KEY's value straight from the config file.

    Used during bootstrap, before load_config() has populated os.environ — it
    must not depend on, or mutate, the environment."""
    try:
        text = config_path().read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        if sep and k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


# =============================================================================
# Virtual environment (lives in the plugin's persistent data dir)
# =============================================================================

def plugin_data_root() -> "Path | None":
    """The plugin's persistent data dir (${CLAUDE_PLUGIN_DATA}), or None.

    Prefer the live environment value (present when a skill substituted it into
    the invocation, e.g. for setup) and fall back to the path setup recorded into
    config.env (the channel every other tool uses, since the variable is not in
    the ambient shell environment)."""
    val = os.environ.get(PLUGIN_DATA_KEY) or _config_value(PLUGIN_DATA_KEY)
    return Path(val).expanduser() if val else None


def venv_dir(data_root: Path) -> Path:
    return data_root / "venv"


def venv_python(data_root: Path) -> Path:
    """Path to the venv's interpreter — the one OS-specific branch in the venv
    story. Launching this binary *is* "activating" the venv: it reads the
    adjacent pyvenv.cfg and points site-packages at the venv, no PATH games."""
    vd = venv_dir(data_root)
    return vd / "Scripts" / "python.exe" if IS_WIN else vd / "bin" / "python"


def bootstrap_venv() -> None:
    """Re-exec the current tool under the toolchain venv interpreter, if needed.

    A no-op when (a) we are already running under it (guard set, or sys.executable
    already is it) or (b) no venv exists yet — so setup, which creates the venv on
    a bare interpreter, runs unimpeded, and stdlib-only tools still work before any
    install. Otherwise re-run the same argv under the venv python and propagate its
    exit code. We use subprocess rather than os.execv because on Windows os.execv
    only emulates exec (spawn + exit), which lets the launching shell return before
    the child finishes and interleaves output; waiting on a child is reliable on
    both platforms."""
    if os.environ.get(_VENV_GUARD):
        return
    root = plugin_data_root()
    if root is None:
        return
    py = venv_python(root)
    if not py.exists():
        return
    try:
        if py.resolve() == Path(sys.executable).resolve():
            os.environ[_VENV_GUARD] = "1"
            return
    except OSError:
        pass
    env = dict(os.environ)
    env[_VENV_GUARD] = "1"
    proc = subprocess.run([str(py), *sys.argv], env=env)
    sys.exit(proc.returncode)


# =============================================================================
# Windows elevation (UAC)
# =============================================================================

def is_admin() -> bool:
    """True if the current process holds an Administrator token (Windows)."""
    if not IS_WIN:
        return os.geteuid() == 0  # type: ignore[attr-defined]  # POSIX-only
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]  # Windows-only
    except Exception:
        return False


def run_elevated(exe: str, params: str) -> "int | None":
    """Launch `exe params` elevated via the UAC "runas" verb and wait for it.

    Returns the child's exit code, or None if elevation could not be started
    (e.g. the user declined the consent dialog). Windows-only; ShellExecuteEx is
    the documented way to trigger a UAC prompt, since a plain CreateProcess
    cannot elevate."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = SW_HIDE

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]  # Windows-only
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    if not shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        return None  # consent declined or launch failed

    kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(sei.hProcess)
    return int(code.value)
