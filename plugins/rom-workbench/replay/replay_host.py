"""Headless libpinmame driver.

Drives libpinmame.dll via ctypes through a SetTimeFence-paced loop. Reads the
record-pinball session.jsonl and injects switch deltas at their simulated-time
offsets, then lets libpinmame run the show: it emits events via callbacks
(solenoid changes, DMD frames, OnState) and via the event-driven debugger
(breakpoints, watchpoints, step). The host only samples *state* (lamps, GIs)
that the libpinmame surface cannot deliver as events.

Trace modes:
    state   — solenoid changes (callback), lamp/GI deltas (per-tick batch).
              Lamps are PWM-driven so the "is the lamp on" question is
              state-at-a-moment, not an event — we sample per tick and
              record only changes.
    dmd     — DMD frames via OnDisplayUpdated callback.
    sound   — emulated audio (PCM) via OnAudioAvailable/OnAudioUpdated. Raw
              interleaved s16le samples are streamed to audio/audio.s16le.raw;
              format + per-chunk timing land in audio.index.jsonl. render the
              DMD video with render_dmd_video.py to mux this in.
    dbg     — Breakpoints / watchpoints / single-step via the debugger API.
              Use --break-pc, --watch-r, --watch-w.

Usage:
    python replay_host.py
        --pinmame-dir <dir-with-libpinmame.dll>
        --rom-dir     <dir-with-rom.zip>
        --rom         <rom-name>          (e.g. congo_21)
        --session     <session.jsonl>
        --out         <out-dir>
        [--trace state[,dmd,dbg]]
        [--sim-step  0.001]               (simulated seconds per loop iteration)
        [--max-sec   600]
        [--quiet]

Time keys (`t`) are simulated seconds since PinmameRun returned. The host driver
advances simulation in fixed sim_step chunks via PinmameSetTimeFence; this is
the only deterministic pacing primitive libpinmame exposes.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# libpinmame ctypes bindings
# ---------------------------------------------------------------------------

# Status codes per libpinmame.h.
PINMAME_STATUS_OK = 0

# Callback signatures (per libpinmame.h).
OnStateUpdatedCallback = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)
OnDisplayAvailableCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
)
OnDisplayUpdatedCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
OnSolenoidUpdatedCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
# Audio callbacks (per libpinmame.h). Both return int:
#   OnAudioAvailable -> samplesPerFrame  (called once when the stream starts)
#   OnAudioUpdated   -> samples           (called per emulated frame with PCM)
OnAudioAvailableCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
OnAudioUpdatedCallback = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
)


class PinmameAudioInfo(ctypes.Structure):
    """Layout passed to OnAudioAvailable (per libpinmame.h)."""

    _fields_ = [
        ("format",          ctypes.c_int),     # PINMAME_AUDIO_FORMAT (0=int16, 1=float)
        ("channels",        ctypes.c_int),     # 1 mono / 2 stereo
        ("sampleRate",      ctypes.c_double),  # actual machine sample rate
        ("framesPerSecond", ctypes.c_double),
        ("samplesPerFrame", ctypes.c_int),     # per-channel frames per video frame
        ("bufferSize",      ctypes.c_int),
    ]


class PinmameSolenoidState(ctypes.Structure):
    _fields_ = [("solNo", ctypes.c_int), ("state", ctypes.c_int)]


class PinmameLampState(ctypes.Structure):
    _fields_ = [("lampNo", ctypes.c_int), ("state", ctypes.c_int)]


class PinmameGIState(ctypes.Structure):
    _fields_ = [("giNo", ctypes.c_int), ("state", ctypes.c_int)]

# PinmameConfig struct (per libpinmame.h — must be passed to PinmameSetConfig before PinmameRun).
PINMAME_MAX_PATH = 512
PINMAME_AUDIO_FORMAT_INT16 = 0

class PinmameConfig(ctypes.Structure):
    _fields_ = [
        ("audioFormat",             ctypes.c_int),
        ("sampleRate",              ctypes.c_int),
        ("vpmPath",                 ctypes.c_char * PINMAME_MAX_PATH),
        ("cb_OnStateUpdated",       ctypes.c_void_p),
        ("cb_OnDisplayAvailable",   ctypes.c_void_p),
        ("cb_OnDisplayUpdated",     ctypes.c_void_p),
        ("cb_OnAudioAvailable",     ctypes.c_void_p),
        ("cb_OnAudioUpdated",       ctypes.c_void_p),
        ("cb_OnMechAvailable",      ctypes.c_void_p),
        ("cb_OnMechUpdated",        ctypes.c_void_p),
        ("cb_OnSolenoidUpdated",    ctypes.c_void_p),
        ("cb_OnConsoleDataUpdated", ctypes.c_void_p),
        ("fn_IsKeyPressed",         ctypes.c_void_p),
        ("cb_OnLogMessage",         ctypes.c_void_p),
        ("cb_OnSoundCommand",       ctypes.c_void_p),
    ]

# PINMAME_FILE_TYPE enum (per libpinmame.h):
PINMAME_FILE_TYPE_ROMS = 0
PINMAME_FILE_TYPE_NVRAM = 1
PINMAME_FILE_TYPE_HISCORE = 2
PINMAME_FILE_TYPE_CONFIG = 3


class PinmameDisplayLayout(ctypes.Structure):
    """Layout passed to OnDisplayAvailable and OnDisplayUpdated (per libpinmame.h)."""

    _fields_ = [
        ("type",   ctypes.c_int),
        ("top",    ctypes.c_int),
        ("left",   ctypes.c_int),
        ("length", ctypes.c_int),  # element count for alpha displays; unused for DMD
        ("width",  ctypes.c_int),
        ("height", ctypes.c_int),
        ("depth",  ctypes.c_int),  # bits per pixel
    ]


class PinmameMainCPURegs(ctypes.Structure):
    """Layout MUST match libpinmame.h::PinmameMainCPURegs exactly.

    Added by the patched libpinmame (the switch-recorder branch; prebuilt in
    this skill's bin/).
    """

    _fields_ = [
        ("pc", ctypes.c_uint16),
        ("s",  ctypes.c_uint16),
        ("u",  ctypes.c_uint16),
        ("x",  ctypes.c_uint16),
        ("y",  ctypes.c_uint16),
        ("a",  ctypes.c_uint8),
        ("b",  ctypes.c_uint8),
        ("cc", ctypes.c_uint8),
        ("dp", ctypes.c_uint8),
    ]


# Debugger event-reason codes (must match libpinmame.h::PINMAME_DBG_REASON)
PINMAME_DBG_REASON_NONE       = 0
PINMAME_DBG_REASON_BREAKPOINT = 1
PINMAME_DBG_REASON_WATCHPOINT = 2
PINMAME_DBG_REASON_STEP       = 3
PINMAME_DBG_REASON_DETACHED   = 4


class PinmameDebugEvent(ctypes.Structure):
    """Layout MUST match libpinmame.h::PinmameDebugEvent exactly."""

    _fields_ = [
        ("reason",         ctypes.c_uint32),
        ("regs",           PinmameMainCPURegs),
        ("hit_pc",         ctypes.c_uint32),
        ("watch_addr",     ctypes.c_uint32),
        ("watch_is_write", ctypes.c_uint8),
        ("watch_value",    ctypes.c_uint8),
        ("_pad",           ctypes.c_uint8 * 2),
    ]


def load_libpinmame(pinmame_dir: Path) -> ctypes.CDLL:
    # The asset ships with a version suffix (e.g. libpinmame-3.6.dll). Prefer the
    # canonical name (created by setup.ps1) if it exists; fall back to any match.
    if os.name == "nt":
        patterns = ("libpinmame.dll", "libpinmame*.dll")
    elif sys.platform == "darwin":
        patterns = ("libpinmame.dylib", "libpinmame*.dylib")
    else:
        patterns = ("libpinmame.so", "libpinmame*.so")
    candidate = None
    for pat in patterns:
        matches = sorted(pinmame_dir.glob(pat))
        if matches:
            candidate = matches[0]
            break
    if candidate is None or not candidate.exists():
        raise FileNotFoundError(f"libpinmame not found under {pinmame_dir} (tried {patterns})")

    # On Windows, add the directory to the DLL search path so dependencies resolve.
    if os.name == "nt":
        os.add_dll_directory(str(pinmame_dir))

    lib = ctypes.CDLL(str(candidate))

    lib.PinmameSetConfig.argtypes = [ctypes.POINTER(PinmameConfig)]
    lib.PinmameSetConfig.restype = None

    lib.PinmameSetPath.argtypes = [ctypes.c_int, ctypes.c_char_p]
    lib.PinmameSetPath.restype = None

    lib.PinmameRun.argtypes = [ctypes.c_char_p]
    lib.PinmameRun.restype = ctypes.c_int

    lib.PinmameStop.argtypes = []
    lib.PinmameStop.restype = None

    lib.PinmameReset.argtypes = []
    lib.PinmameReset.restype = ctypes.c_int

    lib.PinmamePause.argtypes = [ctypes.c_int]
    lib.PinmamePause.restype = ctypes.c_int

    lib.PinmameSetSwitch.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.PinmameSetSwitch.restype = None

    lib.PinmameGetSwitch.argtypes = [ctypes.c_int]
    lib.PinmameGetSwitch.restype = ctypes.c_int

    lib.PinmameGetLamp.argtypes = [ctypes.c_int]
    lib.PinmameGetLamp.restype = ctypes.c_int

    lib.PinmameGetSolenoid.argtypes = [ctypes.c_int]
    lib.PinmameGetSolenoid.restype = ctypes.c_int

    lib.PinmameSetTimeFence.argtypes = [ctypes.c_double]
    lib.PinmameSetTimeFence.restype = None

    # Emulation clock (timer_get_time) — from the patched libpinmame. Lets the host
    # close the fence loop: poll until the emulator has actually reached a posted
    # fence before advancing, instead of sleeping a fixed (Windows-unreliable)
    # interval. Optional so an older DLL still loads (we fall back to a sleep).
    if hasattr(lib, "PinmameGetEmulationTime"):
        lib.PinmameGetEmulationTime.argtypes = []
        lib.PinmameGetEmulationTime.restype = ctypes.c_double
    # Offset-correct "has the emulator reached the posted fence?" predicate --
    # the right thing to poll (comparing GetEmulationTime() to the nominal fence
    # is wrong; the emulator targets fence + time_fence_global_offset).
    if hasattr(lib, "PinmameTimeFenceReached"):
        lib.PinmameTimeFenceReached.argtypes = []
        lib.PinmameTimeFenceReached.restype = ctypes.c_int

    # Runtime memory read — added upstream in commit 52b2dfa6
    # ("libpinmame: add support for reading raw memory"). Useful for
    # inspecting RAM at the moment of a debugger breakpoint, when the
    # emulation thread is paused.
    if hasattr(lib, "PinmameReadMainCPUByte"):
        lib.PinmameReadMainCPUByte.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8)]
        lib.PinmameReadMainCPUByte.restype = ctypes.c_int

    # Change-batch APIs used by the state trace (callback-free for lamps/GIs).
    lib.PinmameGetChangedSolenoids.argtypes = [ctypes.POINTER(PinmameSolenoidState)]
    lib.PinmameGetChangedSolenoids.restype = ctypes.c_int
    lib.PinmameGetChangedLamps.argtypes = [ctypes.POINTER(PinmameLampState)]
    lib.PinmameGetChangedLamps.restype = ctypes.c_int
    if hasattr(lib, "PinmameGetChangedGIs"):
        lib.PinmameGetChangedGIs.argtypes = [ctypes.POINTER(PinmameGIState)]
        lib.PinmameGetChangedGIs.restype = ctypes.c_int

    # Event-driven debugger API — added by the patched libpinmame (the
    # switch-recorder branch; prebuilt in this skill's bin/). Drives the
    # --trace dbg path with traditional debugger semantics: breakpoints fire,
    # the emulation thread blocks, host (Python) reads regs/memory, then
    # resumes via Continue or Step.
    if hasattr(lib, "PinmameDebugAttach"):
        lib.PinmameDebugAttach.argtypes = []
        lib.PinmameDebugAttach.restype = None
        lib.PinmameDebugDetach.argtypes = []
        lib.PinmameDebugDetach.restype = None
        lib.PinmameDebugIsAttached.argtypes = []
        lib.PinmameDebugIsAttached.restype = ctypes.c_int

        lib.PinmameDebugAddBreakpoint.argtypes = [ctypes.c_uint32]
        lib.PinmameDebugAddBreakpoint.restype = None
        lib.PinmameDebugRemoveBreakpoint.argtypes = [ctypes.c_uint32]
        lib.PinmameDebugRemoveBreakpoint.restype = None
        lib.PinmameDebugClearBreakpoints.argtypes = []
        lib.PinmameDebugClearBreakpoints.restype = None

        lib.PinmameDebugAddWatchpoint.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_int]
        lib.PinmameDebugAddWatchpoint.restype = None
        lib.PinmameDebugRemoveWatchpoint.argtypes = [ctypes.c_uint32]
        lib.PinmameDebugRemoveWatchpoint.restype = None
        lib.PinmameDebugClearWatchpoints.argtypes = []
        lib.PinmameDebugClearWatchpoints.restype = None

        lib.PinmameDebugWait.argtypes = [ctypes.c_uint32, ctypes.POINTER(PinmameDebugEvent)]
        lib.PinmameDebugWait.restype = ctypes.c_int
        lib.PinmameDebugContinue.argtypes = []
        lib.PinmameDebugContinue.restype = None
        lib.PinmameDebugStep.argtypes = []
        lib.PinmameDebugStep.restype = None

    return lib


# ---------------------------------------------------------------------------
# Session reading
# ---------------------------------------------------------------------------


@dataclass(order=True)
class SwitchEvent:
    t: float
    n: int
    on: bool


def read_session(session_path: Path) -> tuple[dict, list[SwitchEvent]]:
    meta: Optional[dict] = None
    events: list[SwitchEvent] = []
    with session_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if lineno == 1:
                if rec.get("kind") != "meta":
                    raise ValueError(f"{session_path}: first record is not meta")
                meta = rec
                continue
            if rec.get("kind") == "switch":
                events.append(
                    SwitchEvent(t=float(rec["t"]), n=int(rec["n"]), on=bool(rec["on"]))
                )
    if meta is None:
        raise ValueError(f"{session_path}: empty session")
    events.sort()
    return meta, events


# ---------------------------------------------------------------------------
# Trace output writers
# ---------------------------------------------------------------------------


class JsonlWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            self._f.close()


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pinmame-dir", type=Path, required=True)
    ap.add_argument("--rom-dir", type=Path, required=True)
    ap.add_argument("--rom", required=True)
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--nvram-dir", type=Path, default=None,
                    help="Override NVRAM directory (isolates state between runs)")
    ap.add_argument("--trace", default="state",
                    help="comma-separated subset of: state,dmd,sound,dbg")
    ap.add_argument("--tail-sec", type=float, default=1.0,
                    help="Keep emulating this many seconds past the last switch "
                         "event so the game can settle / the DMD can finish its "
                         "transition (e.g. attract->game after a Start press). "
                         "Default 1.0; capped by --max-sec.")
    ap.add_argument("--break-pc", default="",
                    help="Comma-separated PCs to break on (used by --trace dbg). "
                         "e.g. '0xD9A6,0xD9BF,0xD9CD,0xD9DB'.")
    ap.add_argument("--watch-r", default="",
                    help="Comma-separated addresses for read watchpoints (--trace dbg).")
    ap.add_argument("--watch-w", default="",
                    help="Comma-separated addresses for write watchpoints (--trace dbg).")
    ap.add_argument("--dbg-step-after", type=int, default=0,
                    help="When --trace dbg fires, single-step N additional instructions "
                         "and capture each. Useful for inspecting the prologue of a "
                         "routine at a breakpoint. 0 = Continue immediately on each hit.")
    ap.add_argument("--dbg-mem", default="",
                    help="Comma-separated memory windows to dump on every --trace dbg "
                         "hit, each 'ADDR[:LEN]' (LEN default 1), e.g. "
                         "'0x0326:8,0x0011'. Bytes are read via PinmameReadMainCPUByte "
                         "while the CPU is frozen and emitted as hex in the record's "
                         "'mem' map. Use to read a RAM/ROM source byte live at a break.")
    ap.add_argument("--interactive", action="store_true",
                    help="Persistent debugger session: boot to the first breakpoint, "
                         "hold the CPU frozen, and serve regs/mem/dis/step/bp/continue "
                         "commands over a TCP control socket until 'quit'. Requires "
                         "--trace dbg and at least one --break-pc.")
    ap.add_argument("--dbg-port", type=int, default=47655,
                    help="TCP port for the interactive debugger control socket "
                         "(127.0.0.1). Default 47655.")
    ap.add_argument("--playback-inp", type=Path, default=None,
                    help="MAME .inp recording to play back natively via PinMAME's "
                         "VPINMAME_PLAYBACK path. When set, the emulator replays the "
                         "recorded input ports itself; session.jsonl switch events (if "
                         "any) are ignored. This is the faithful way to replay a "
                         "VpRecord/InpOnly session — those store input only in the .inp.")
    ap.add_argument("--sim-step", type=float, default=0.001)
    ap.add_argument("--max-sec", type=float, default=600.0)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--heartbeat-sec", type=float, default=5.0,
                    help="Emit a one-line progress update every N sim-seconds. "
                         "Set to 0 to disable. Ignored under --quiet.")
    args = ap.parse_args(argv)

    traces = set(t.strip() for t in args.trace.split(",") if t.strip())
    args.out.mkdir(parents=True, exist_ok=True)

    def log(*a, **k):
        if args.quiet:
            return
        print(*a, **k, flush=True)

    log(f"[replay_host] loading libpinmame from {args.pinmame_dir}")
    lib = load_libpinmame(args.pinmame_dir)
    log("[replay_host] libpinmame loaded")

    # --- Set up trace writers / callbacks ------------------------------------

    state_writer: Optional[JsonlWriter] = None
    dmd_writer: Optional[JsonlWriter] = None
    dbg_writer: Optional[JsonlWriter] = None
    sound_writer: Optional[JsonlWriter] = None
    audio_raw_file = None  # binary stream of raw interleaved s16le PCM
    dmd_dir: Optional[Path] = None
    saved_callbacks = []  # keep references so they aren't GC'd

    # Shared "current sim time" cell. Updated by the main loop, read by
    # callbacks that fire on the emulation thread (they don't know sim_t
    # themselves). Approximate by sim_step, which is fine for state events.
    last_sim_t = [0.0]

    state_event_counter = [0]  # bumped by state callbacks + the per-tick lamp/GI delta writer
    dmd_frame_counter   = [0]  # bumped by _on_display
    dbg_hit_counter     = [0]  # bumped by the debugger worker thread
    audio_chunk_counter = [0]  # bumped by _on_audio_updated
    audio_sample_total  = [0]  # cumulative per-channel frames written to the raw file

    # libpinmame OnStateUpdated: state==1 means the machine has finished booting
    # (MACHINE_INIT ran, cpu_timeslice is now honoring time fences). We MUST wait
    # for this before posting fences -- PinmameRun returns immediately while the
    # worker is still loading the ROM, and during that window the fence APIs report
    # their not-running short-circuit (TimeFenceReached()->1, GetEmulationTime()->0),
    # so an un-gated loop races to the end in a fraction of a second without pacing.
    # Registered unconditionally (independent of --trace) so the gate always works.
    running_state = [0]

    def _on_state(state: int, _ud) -> None:
        running_state[0] = int(state)
        if state_writer is not None:
            state_writer.write(
                {"t": last_sim_t[0], "kind": "pm_state", "state": int(state)}
            )
            state_event_counter[0] += 1

    cb_state = OnStateUpdatedCallback(_on_state)
    saved_callbacks.append(cb_state)

    # Event-driven debugger trace. Spawns a background thread that
    # blocks on PinmameDebugWait; each breakpoint/watchpoint hit writes
    # a record and (optionally) single-steps for N more instructions.
    dbg_break_pcs:  list[int]              = []
    dbg_watch_r_addrs: list[int]           = []
    dbg_watch_w_addrs: list[int]           = []
    dbg_stop_flag: dict[str, bool]         = {"stop": False}
    dbg_thread: Optional[threading.Thread] = None
    if "dbg" in traces:
        if not hasattr(lib, "PinmameDebugAttach"):
            raise SystemExit(
                "libpinmame doesn't export PinmameDebugAttach — use the prebuilt "
                "DLL in this skill's bin/, or rebuild from the patched PinMAME "
                "source (the switch-recorder branch with the debugger API patch)."
            )
        if not (args.break_pc or args.watch_r or args.watch_w):
            raise SystemExit(
                "--trace includes 'dbg' but no --break-pc / --watch-r / --watch-w supplied"
            )

        def _parse_addrs(spec: str) -> list[int]:
            return sorted({int(t.strip(), 0) for t in spec.split(",") if t.strip()})

        if args.break_pc: dbg_break_pcs     = _parse_addrs(args.break_pc)
        if args.watch_r:  dbg_watch_r_addrs = _parse_addrs(args.watch_r)
        if args.watch_w:  dbg_watch_w_addrs = _parse_addrs(args.watch_w)

        # --dbg-mem: memory windows to sample on each hit. Each window is
        # ('abs', addr, len) for a fixed address, or ('reg', name, off, len)
        # for a register-relative read (the address is resolved live from the
        # frozen CPU's registers — lets you dereference S/U/X/Y/PC to follow
        # stack frames and pointer chains). Spec grammar per comma item:
        #   ADDR[:LEN]            e.g. 0x0326:8
        #   @REG[+/-OFF][:LEN]    e.g. @S:2  @X:16  @U+5:1
        dbg_mem_windows: list[tuple] = []
        _memspec = re.compile(
            r'^@(?P<reg>pc|s|u|x|y)(?P<off>[+-][0-9a-fx]+)?(?::(?P<len>\w+))?$',
            re.I)
        for spec in (t.strip() for t in args.dbg_mem.split(",") if t.strip()):
            m = _memspec.match(spec)
            if m:
                off = int(m.group("off"), 0) if m.group("off") else 0
                ln  = int(m.group("len"), 0) if m.group("len") else 1
                dbg_mem_windows.append(("reg", m.group("reg").lower(), off, ln))
            else:
                a, _, ln = spec.partition(":")
                dbg_mem_windows.append(("abs", int(a, 0), int(ln, 0) if ln else 1))

        dbg_path = args.out / "trace.dbg.jsonl"
        dbg_writer = JsonlWriter(dbg_path)
        dbg_writer.write(
            {
                "v": 1,
                "kind": "trace_meta",
                "trace_kind": "dbg",
                "session_path": str(args.session),
                "rom": args.rom,
                "rom_dir": str(args.rom_dir),
                "start_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "sim_step_seconds": args.sim_step,
                "options": {
                    "break_pc": [f"0x{p:04X}" for p in dbg_break_pcs] or None,
                    "watch_r":  [f"0x{a:04X}" for a in dbg_watch_r_addrs] or None,
                    "watch_w":  [f"0x{a:04X}" for a in dbg_watch_w_addrs] or None,
                    "step_after": args.dbg_step_after,
                    "mem": [
                        (f"@{w[1].upper()}{w[2]:+#x}:{w[3]}" if w[0] == "reg"
                         else f"0x{w[1]:04X}:{w[2]}")
                        for w in dbg_mem_windows
                    ] or None,
                },
            }
        )

    if "state" in traces:
        state_path = args.out / "trace.state.jsonl"
        state_writer = JsonlWriter(state_path)
        state_writer.write(
            {
                "v": 1,
                "kind": "trace_meta",
                "trace_kind": "state",
                "session_path": str(args.session),
                "rom": args.rom,
                "rom_dir": str(args.rom_dir),
                "start_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "sim_step_seconds": args.sim_step,
                "options": {"sols_via": "OnSolenoidUpdated callback",
                            "lamps_gis_via": "GetChangedLamps/GetChangedGIs per tick"},
            }
        )

        # (OnStateUpdated callback _on_state is defined+registered above, so the
        # running-state gate works regardless of which traces are enabled.)

        # OnSolenoidUpdated fires for every solenoid change. The callback
        # receives a single PinmameSolenoidState* (one solenoid per call).
        # We treat it as the authoritative source for solenoid timeline
        # and drop the per-tick polling that used to happen here.
        def _on_solenoid(p_state, _ud) -> None:
            if not p_state:
                return
            s = ctypes.cast(p_state, ctypes.POINTER(PinmameSolenoidState)).contents
            state_writer.write(
                {"t": last_sim_t[0], "kind": "sol",
                 "n": int(s.solNo), "v": int(s.state)}
            )
            state_event_counter[0] += 1

        cb_solenoid = OnSolenoidUpdatedCallback(_on_solenoid)
        saved_callbacks.append(cb_solenoid)

    if "dmd" in traces:
        dmd_dir = args.out / "dmd"
        dmd_dir.mkdir(parents=True, exist_ok=True)
        dmd_writer = JsonlWriter(args.out / "dmd.index.jsonl")
        dmd_writer.write(
            {
                "v": 1,
                "kind": "trace_meta",
                "trace_kind": "dmd",
                "session_path": str(args.session),
                "rom": args.rom,
                "rom_dir": str(args.rom_dir),
                "start_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "options": {},
            }
        )

        frame_counter = dmd_frame_counter  # alias so the existing closure body keeps working

        def _on_display(index: int, p_data, p_layout, _ud) -> None:
            if not p_layout or not p_data:
                return
            layout = ctypes.cast(p_layout, ctypes.POINTER(PinmameDisplayLayout)).contents
            w, h, bpp = int(layout.width), int(layout.height), int(layout.depth)
            if w <= 0 or h <= 0:
                return
            n_bytes = w * h * max(1, bpp // 8) if bpp >= 8 else w * h
            try:
                buf = ctypes.string_at(p_data, n_bytes)
            except Exception:
                return
            sha = hashlib.sha256(buf).hexdigest()
            idx = frame_counter[0]
            frame_counter[0] += 1
            (dmd_dir / f"{idx:06d}.bin").write_bytes(buf)
            dmd_writer.write(
                {
                    "t": last_sim_t[0],
                    "kind": "dmd",
                    "frame": idx,
                    "sha256": sha,
                    "width": w,
                    "height": h,
                    "bits_per_pixel": bpp,
                }
            )

        def _on_display_available(index: int, count: int, p_layout, _ud) -> None:
            pass  # accept the display; libpinmame requires this to activate Updated callbacks

        cb_dmd_avail = OnDisplayAvailableCallback(_on_display_available)
        saved_callbacks.append(cb_dmd_avail)

        cb_dmd = OnDisplayUpdatedCallback(_on_display)
        saved_callbacks.append(cb_dmd)

    if "sound" in traces:
        audio_dir = args.out / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_raw_path = audio_dir / "audio.s16le.raw"
        audio_raw_file = audio_raw_path.open("wb")
        sound_writer = JsonlWriter(args.out / "audio.index.jsonl")
        sound_writer.write(
            {
                "v": 1,
                "kind": "trace_meta",
                "trace_kind": "sound",
                "session_path": str(args.session),
                "rom": args.rom,
                "rom_dir": str(args.rom_dir),
                "start_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "options": {"pcm": "interleaved s16le in audio/audio.s16le.raw"},
            }
        )

        # Filled from OnAudioAvailable (the *actual* machine rate/channels —
        # may differ from the requested config.sampleRate). Mono vs stereo
        # decides bytes-per-sample-frame in the updated callback.
        audio_info = {"sample_rate": 0.0, "channels": 1, "format": 0,
                      "samples_per_frame": 0}
        audio_format_seen = [False]

        def _on_audio_available(p_info, _ud) -> int:
            if not p_info:
                return 0
            info = ctypes.cast(p_info, ctypes.POINTER(PinmameAudioInfo)).contents
            audio_info["sample_rate"]      = float(info.sampleRate)
            audio_info["channels"]         = int(info.channels) or 1
            audio_info["format"]           = int(info.format)
            audio_info["samples_per_frame"] = int(info.samplesPerFrame)
            audio_format_seen[0] = True
            sound_writer.write(
                {
                    "t": last_sim_t[0],
                    "kind": "audio_format",
                    "sample_rate": audio_info["sample_rate"],
                    "channels": audio_info["channels"],
                    "pcm_format": "s16le",
                    "samples_per_frame": audio_info["samples_per_frame"],
                }
            )
            # Convention (see libpinmame test.cpp): return samplesPerFrame.
            return audio_info["samples_per_frame"]

        def _on_audio_updated(p_buffer, samples, _ud) -> int:
            # `samples` is per-channel frames; the int16 buffer holds
            # samples*channels values. We only handle the int16 path
            # (config.audioFormat is fixed to INT16 below).
            if not p_buffer or samples <= 0:
                return samples
            ch = audio_info["channels"] or 1
            try:
                buf = ctypes.string_at(p_buffer, samples * ch * 2)
            except Exception:
                return samples
            audio_raw_file.write(buf)
            off = audio_sample_total[0]
            audio_sample_total[0] += samples
            sound_writer.write(
                {"t": last_sim_t[0], "kind": "audio",
                 "sample_offset": off, "samples": int(samples)}
            )
            audio_chunk_counter[0] += 1
            return samples

        cb_audio_avail = OnAudioAvailableCallback(_on_audio_available)
        saved_callbacks.append(cb_audio_avail)
        cb_audio = OnAudioUpdatedCallback(_on_audio_updated)
        saved_callbacks.append(cb_audio)

    # --- Configure and run the simulator -------------------------------------

    config = PinmameConfig()
    config.audioFormat = PINMAME_AUDIO_FORMAT_INT16
    config.sampleRate  = 44100
    # vpmPath is the *parent* of the pinmame directory; libpinmame appends "\pinmame\" internally.
    vpm_base = str(args.pinmame_dir.parent)

    # Native .inp playback: PinMAME's run_game() opens the file named by the
    # VPINMAME_PLAYBACK env var from the FILETYPE_INPUTLOG search dir, which
    # libpinmame hard-wires to `vpmPath + "inp"` (ComposePath concatenates with
    # NO separator, and there is no PinmameSetPath for INPUTLOG — see the
    # explicit ROMS/NVRAM/CONFIG/HISCORE overrides below for why). So we point
    # vpmPath at <out>/ (trailing sep) → INPUTLOG resolves to <out>/inp, stage
    # the recording there as <rom>.inp, and let the emulator replay the recorded
    # input ports itself. roms/nvram/cfg/hi are overridden after SetConfig, so
    # repointing vpmPath only moves the (unused here) samples/memcard/state
    # defaults; the lone other use is an <vpmPath>/alias.txt probe (harmless).
    if args.playback_inp is not None:
        if not args.playback_inp.is_file():
            raise SystemExit(f"--playback-inp not found: {args.playback_inp}")
        vpm_base = str(args.out) + os.sep
        inp_dir = args.out / "inp"
        inp_dir.mkdir(parents=True, exist_ok=True)
        staged = inp_dir / f"{args.rom}.inp"
        shutil.copy2(args.playback_inp, staged)
        os.environ["VPINMAME_PLAYBACK"] = args.rom
        log(f"[replay_host] native .inp playback: {staged} "
            f"(VPINMAME_PLAYBACK={args.rom})")

    vpm_bytes = vpm_base.encode("utf-8")
    config.vpmPath = vpm_bytes[:PINMAME_MAX_PATH - 1]
    # State callback is always registered (drives the running-state gate);
    # solenoid callback only when the state trace wants it.
    config.cb_OnStateUpdated = ctypes.cast(cb_state, ctypes.c_void_p)
    if state_writer is not None:
        config.cb_OnSolenoidUpdated = ctypes.cast(cb_solenoid, ctypes.c_void_p)
    if dmd_writer is not None:
        config.cb_OnDisplayAvailable = ctypes.cast(cb_dmd_avail, ctypes.c_void_p)
        config.cb_OnDisplayUpdated   = ctypes.cast(cb_dmd,       ctypes.c_void_p)
    if sound_writer is not None:
        config.cb_OnAudioAvailable = ctypes.cast(cb_audio_avail, ctypes.c_void_p)
        config.cb_OnAudioUpdated   = ctypes.cast(cb_audio,       ctypes.c_void_p)
    lib.PinmameSetConfig(ctypes.byref(config))

    # Set ROM path after SetConfig (SetConfig may reset internal state).
    lib.PinmameSetPath(PINMAME_FILE_TYPE_ROMS, str(args.rom_dir).encode("utf-8"))
    if args.nvram_dir is not None:
        args.nvram_dir.mkdir(parents=True, exist_ok=True)
        lib.PinmameSetPath(PINMAME_FILE_TYPE_NVRAM, str(args.nvram_dir).encode("utf-8"))

    # Pin CONFIG/HISCORE into the per-run scratch dir too. Without this,
    # libpinmame derives a default cfg dir by appending "cfg" to vpmPath with no
    # separator (-> a stray "<skill-dir>cfg" leaking MAME .cfg files next to the
    # skill). Routing it under --out keeps every run's artifacts self-contained.
    cfg_dir = args.out / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lib.PinmameSetPath(PINMAME_FILE_TYPE_CONFIG, str(cfg_dir).encode("utf-8"))
    lib.PinmameSetPath(PINMAME_FILE_TYPE_HISCORE, str(cfg_dir).encode("utf-8"))

    if hasattr(lib, "PinmameSetHandleKeyboard"):
        lib.PinmameSetHandleKeyboard.argtypes = [ctypes.c_int]
        lib.PinmameSetHandleKeyboard.restype  = None
        lib.PinmameSetHandleKeyboard(0)

    log(f"[replay_host] PinmameRun({args.rom})")
    status = lib.PinmameRun(args.rom.encode("utf-8"))
    if status != PINMAME_STATUS_OK:
        raise RuntimeError(f"PinmameRun returned status {status}")

    # Register debug breakpoints/watchpoints and start the worker thread.
    # The thread blocks on PinmameDebugWait — when the emulator's m6809
    # dispatch loop hits a breakpoint, it freezes itself and signals the
    # condition variable; the worker wakes up, snapshots, writes JSONL,
    # then calls PinmameDebugContinue (or Step) to release the CPU.
    if dbg_writer is not None:
        lib.PinmameDebugAttach()
        for pc in dbg_break_pcs:
            lib.PinmameDebugAddBreakpoint(ctypes.c_uint32(pc))
        for a in dbg_watch_r_addrs:
            lib.PinmameDebugAddWatchpoint(ctypes.c_uint32(a), 1, 0)
        for a in dbg_watch_w_addrs:
            lib.PinmameDebugAddWatchpoint(ctypes.c_uint32(a), 0, 1)

        # WPC keeps a RAM shadow of the live ROM-bank register at
        # (DP<<8)+0x11 (the system mirrors every WPC_ROM_BANK write there;
        # visible in the decompile as *(in_DP*0x100+0x11)). Reading it at a
        # frozen breakpoint tells us which page is mapped into $4000-$7FFF,
        # which is the ONLY way to resolve a banked PC/pointer to a file
        # offset — the register snapshot alone is ambiguous across pages.
        _have_membyte = hasattr(lib, "PinmameReadMainCPUByte")

        def _read_byte(addr: int):
            if not _have_membyte:
                return None
            out = ctypes.c_uint8(0)
            lib.PinmameReadMainCPUByte(ctypes.c_uint32(addr & 0xFFFF),
                                       ctypes.byref(out))
            return int(out.value)

        def _loc(pc: int, bank) -> str:
            # Canonical address form accepted by wpc-investigate/rom.py.
            if 0x4000 <= pc < 0x8000 and bank is not None:
                return f"${pc:04X}@p{bank:02X}"
            if pc >= 0x8000:
                return f"${pc:04X}"
            return f"${pc:04X}"  # RAM/IO — no ROM page

        def _dbg_worker():
            ev = PinmameDebugEvent()
            reason_name = {
                PINMAME_DBG_REASON_BREAKPOINT: "bp",
                PINMAME_DBG_REASON_WATCHPOINT: "wp",
                PINMAME_DBG_REASON_STEP:       "step",
                PINMAME_DBG_REASON_DETACHED:   "detached",
            }
            steps_remaining = 0
            while not dbg_stop_flag["stop"]:
                # 100ms timeout lets us notice stop_flag cleanly on shutdown.
                ok = lib.PinmameDebugWait(ctypes.c_uint32(100), ctypes.byref(ev))
                if not ok:
                    continue
                if ev.reason == PINMAME_DBG_REASON_DETACHED:
                    break
                rec = {
                    # Stamp the most-recent simulated time (shared cell
                    # updated by the fence loop), same basis as the
                    # state/sol/lamp records — lets break/watch hits be
                    # correlated and filtered by sim time.
                    "t":      last_sim_t[0],
                    "kind":   "dbg",
                    "reason": reason_name.get(int(ev.reason), str(int(ev.reason))),
                    "pc":     f"0x{int(ev.hit_pc):04X}",
                    "a":      int(ev.regs.a),
                    "b":      int(ev.regs.b),
                    "x":      int(ev.regs.x),
                    "y":      int(ev.regs.y),
                    "u":      int(ev.regs.u),
                    "s":      int(ev.regs.s),
                    "cc":     int(ev.regs.cc),
                    "dp":     int(ev.regs.dp),
                }
                if ev.reason == PINMAME_DBG_REASON_WATCHPOINT:
                    rec["watch_addr"] = f"0x{int(ev.watch_addr):04X}"
                    rec["is_write"]   = bool(ev.watch_is_write)
                    rec["value"]      = int(ev.watch_value)

                # Resolve the mapped ROM page so a banked PC becomes a
                # file-locatable address (feed `loc` straight to rom.py).
                pc   = int(ev.hit_pc)
                bank = _read_byte((int(ev.regs.dp) << 8) + 0x11)
                if bank is not None:
                    rec["bank"] = f"0x{bank:02X}"
                rec["loc"] = _loc(pc, bank)

                # Optional memory windows, read live while the CPU is frozen.
                # Register-relative windows ('reg') resolve their base address
                # from this hit's snapshot, so '@S:2' yields the return address
                # and '@X:16' dumps the struct/string X points at.
                if dbg_mem_windows:
                    mem = {}
                    for win in dbg_mem_windows:
                        if win[0] == "reg":
                            _, reg, off, ln = win
                            base = int(getattr(ev.regs, reg)) + off
                            sign = "+" if off >= 0 else "-"
                            key = f"@{reg.upper()}" + (
                                f"{sign}0x{abs(off):X}" if off else "") + f"=0x{base & 0xFFFF:04X}"
                        else:
                            _, base, ln = win
                            key = f"0x{base & 0xFFFF:04X}"
                        bs = [_read_byte(base + i) for i in range(ln)]
                        mem[key] = "".join(
                            "??" if b is None else f"{b:02X}" for b in bs)
                    rec["mem"] = mem
                dbg_writer.write(rec)
                dbg_hit_counter[0] += 1

                # Step-after support: continue with single-step semantics
                # for N more instructions, capturing each, then resume free
                # execution. The user explicitly requested this kind of
                # debugger control rather than approximate polling.
                if ev.reason == PINMAME_DBG_REASON_BREAKPOINT and args.dbg_step_after > 0:
                    steps_remaining = args.dbg_step_after
                if steps_remaining > 0 and ev.reason in (
                    PINMAME_DBG_REASON_BREAKPOINT, PINMAME_DBG_REASON_STEP):
                    steps_remaining -= 1
                    lib.PinmameDebugStep()
                else:
                    lib.PinmameDebugContinue()

        # ---- interactive persistent session --------------------------------
        # Instead of the auto-emit-and-continue policy above, hold the CPU
        # frozen at each break and serve commands over a TCP socket, so the
        # *next* probe is decided from what the *last* one showed — without
        # re-booting from POST. The main fence loop keeps the emulator alive;
        # this control thread owns the Debug* API + the command socket.
        interactive_stop = {"stop": False}

        def _load_disasm():
            """Import the 6809 disassembler from the sibling wpc-investigate
            skill so `dis` decodes the *live* instruction stream (correct bank,
            ground-truth boundaries). Optional: degrades to a hint if absent."""
            try:
                import importlib.util
                rp = (Path(__file__).resolve().parent.parent.parent
                      / "wpc-investigate" / "rom.py")
                spec = importlib.util.spec_from_file_location("wpc_rom", rp)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception as e:  # pragma: no cover - best effort
                log(f"[dbg] live disassembler unavailable: {e}")
                return None

        def _interactive_worker():
            import socket
            ev = PinmameDebugEvent()
            dism = _load_disasm()
            # ReadMainCPUByte does NOT apply the WPC ROM bank to the $4000-$7FFF
            # window (banked reads come back zero), so for that window we read
            # the ROM image at page=bank instead. ROM doesn't self-modify, so a
            # static read at the live PC+bank is faithful. RAM/IO (<$4000) and
            # system ROM ($8000+) read fine live.
            rom_img = None
            rom_first = None
            try:
                if dism is not None:
                    rz = args.rom_dir / f"{args.rom}.zip"
                    rom_img = dism.load_rom(str(rz))
                    rom_first = dism.SIZE_TO_FIRST_PAGE[len(rom_img)]
            except Exception as e:  # pragma: no cover
                log(f"[dbg] ROM image for banked reads unavailable: {e}")

            def read_smart(addr: int, bank):
                addr &= 0xFFFF
                if (0x4000 <= addr < 0x8000 and rom_img is not None
                        and bank is not None and rom_first is not None):
                    off = (bank - rom_first) * 0x4000 + (addr - 0x4000)
                    if 0 <= off < len(rom_img):
                        return rom_img[off]
                return _read_byte(addr)

            bp_set: set[int] = set(dbg_break_pcs)
            wp_set: dict[int, str] = {}
            for a in dbg_watch_r_addrs: wp_set[a] = "r"
            for a in dbg_watch_w_addrs: wp_set[a] = "w"

            def wait_hit() -> bool:
                """Block (with shutdown polling) until the CPU freezes. True if
                a break/step/watch event landed in `ev`; False on detach/stop."""
                while not interactive_stop["stop"]:
                    if lib.PinmameDebugWait(ctypes.c_uint32(200), ctypes.byref(ev)):
                        if ev.reason == PINMAME_DBG_REASON_DETACHED:
                            return False
                        return True
                return False

            def cur():
                pc   = int(ev.regs.pc)
                bank = _read_byte((int(ev.regs.dp) << 8) + 0x11)
                return pc, bank

            def fmt_regs() -> str:
                pc, bank = cur()
                r = ev.regs
                bs = f"{bank:02X}" if bank is not None else "??"
                return (f"loc={_loc(pc, bank)}  bank={bs}\n"
                        f"PC={pc:04X} S={int(r.s):04X} U={int(r.u):04X} "
                        f"X={int(r.x):04X} Y={int(r.y):04X}\n"
                        f"A={int(r.a):02X} B={int(r.b):02X} "
                        f"CC={int(r.cc):02X} DP={int(r.dp):02X}")

            def resolve_addr(tok: str) -> int:
                """Accept 0xNNNN, $NNNN, NNNN(hex), or @REG[+/-off]."""
                tok = tok.strip()
                m = re.match(r'^@(pc|s|u|x|y)([+-][0-9a-fx]+)?$', tok, re.I)
                if m:
                    base = int(getattr(ev.regs, m.group(1).lower()))
                    return (base + (int(m.group(2), 0) if m.group(2) else 0)) & 0xFFFF
                tok = tok.lstrip("$")
                return int(tok, 16 if re.fullmatch(r'[0-9a-fA-F]+', tok) else 0) & 0xFFFF

            def do_mem(addr: int, ln: int, bank) -> str:
                out = []
                for row in range(0, ln, 16):
                    chunk = [read_smart(addr + row + i, bank) for i in range(min(16, ln - row))]
                    hexs = " ".join("??" if b is None else f"{b:02X}" for b in chunk)
                    asc  = "".join(chr(b) if b and 0x20 <= b <= 0x7E else "." for b in chunk)
                    out.append(f"  {addr+row:04X}: {hexs:<47}  |{asc}|")
                return "\n".join(out)

            def do_dis(addr: int, count: int, bank) -> str:
                if dism is None:
                    return ("(live disassembler unavailable; use "
                            "wpc-investigate/rom.py dis '$%04X@p%s')"
                            % (addr, f"{bank:02X}" if bank is not None else "??"))
                # Decode from ROM image for banked/system code (faithful, correct
                # bank); branch targets use logical addr + bank.
                buf = bytes((read_smart(addr + i, bank) or 0) for i in range(count * 4 + 12))
                page = bank if (0x4000 <= addr < 0x8000) else None
                psuf = f"@p{page:02X}" if page is not None else ""
                a, off, lines = addr, 0, []
                for _ in range(count):
                    if off >= len(buf) - 4:
                        break
                    n, mn, operand, tgt = dism.disasm_one(buf, off, a, page)
                    raw = " ".join(f"{b:02X}" for b in buf[off:off + n])
                    ann = ""
                    if tgt is not None:
                        if 0x4000 <= tgt < 0x8000 and page is not None:
                            ann = f"   -> ${tgt:04X}@p{page:02X}"
                        else:
                            ann = f"   -> ${tgt:04X}"
                    lines.append(f"  ${a:04X}{psuf}  {raw:<14}  {mn:<6} {operand}{ann}")
                    a = (a + n) & 0xFFFF
                    off += n
                return "\n".join(lines)

            def handle(line: str) -> str:
                parts = line.split()
                if not parts:
                    return ""
                cmd, args_ = parts[0].lower(), parts[1:]
                pc, bank = cur()
                if cmd in ("regs", "r", "info"):
                    return fmt_regs()
                if cmd == "bank":
                    return f"{bank:02X}" if bank is not None else "??"
                if cmd in ("mem", "m", "x"):
                    addr = resolve_addr(args_[0]); ln = int(args_[1], 0) if len(args_) > 1 else 16
                    return do_mem(addr, ln, bank)
                if cmd in ("dis", "d", "u"):
                    addr = resolve_addr(args_[0]) if args_ else pc
                    count = int(args_[1], 0) if len(args_) > 1 else 8
                    return do_dis(addr, count, bank)
                if cmd in ("step", "s"):
                    n = int(args_[0], 0) if args_ else 1
                    rows = []
                    for _ in range(n):
                        lib.PinmameDebugStep()
                        if not wait_hit():
                            return "(detached during step)"
                        p, bk = cur()
                        rows.append(f"  {_loc(p, bk)}  A={int(ev.regs.a):02X} "
                                    f"B={int(ev.regs.b):02X} X={int(ev.regs.x):04X} "
                                    f"Y={int(ev.regs.y):04X} U={int(ev.regs.u):04X}")
                    return "\n".join(rows)
                if cmd in ("continue", "cont", "c", "g"):
                    tmp = None
                    if args_ and args_[0].lower() in ("until", "to"):
                        tmp = resolve_addr(args_[1])
                        if tmp not in bp_set:
                            lib.PinmameDebugAddBreakpoint(ctypes.c_uint32(tmp))
                    lib.PinmameDebugContinue()
                    if not wait_hit():
                        return "(detached)"
                    if tmp is not None and tmp not in bp_set:
                        lib.PinmameDebugRemoveBreakpoint(ctypes.c_uint32(tmp))
                    return "stopped:\n" + fmt_regs()
                if cmd == "bp":
                    sub = args_[0].lower() if args_ else "list"
                    if sub == "add":
                        a = resolve_addr(args_[1]); bp_set.add(a)
                        lib.PinmameDebugAddBreakpoint(ctypes.c_uint32(a)); return f"bp+ {a:04X}"
                    if sub in ("del", "rm"):
                        a = resolve_addr(args_[1]); bp_set.discard(a)
                        lib.PinmameDebugRemoveBreakpoint(ctypes.c_uint32(a)); return f"bp- {a:04X}"
                    return "bp: " + (", ".join(f"{a:04X}" for a in sorted(bp_set)) or "(none)")
                if cmd == "wp":
                    sub = args_[0].lower() if args_ else "list"
                    if sub == "add":
                        kind = args_[1].lower(); a = resolve_addr(args_[2])
                        lib.PinmameDebugAddWatchpoint(ctypes.c_uint32(a),
                                                      1 if kind == "r" else 0,
                                                      1 if kind == "w" else 0)
                        wp_set[a] = kind; return f"wp+ {kind} {a:04X}"
                    if sub in ("del", "rm"):
                        a = resolve_addr(args_[2] if len(args_) > 2 else args_[1])
                        lib.PinmameDebugRemoveWatchpoint(ctypes.c_uint32(a))
                        wp_set.pop(a, None); return f"wp- {a:04X}"
                    return "wp: " + (", ".join(f"{v}:{a:04X}" for a, v in sorted(wp_set.items())) or "(none)")
                if cmd in ("quit", "q", "detach"):
                    interactive_stop["stop"] = True
                    return "bye"
                if cmd in ("help", "?", "h"):
                    return ("commands: regs | mem <addr> [len] | dis [addr] [n] | "
                            "step [n] | continue [until <pc>] | bp add|del <pc> | "
                            "bp list | wp add r|w <addr> | wp del <addr> | bank | quit\n"
                            "addr forms: 0xNNNN  $NNNN  NNNN(hex)  @X @S+2 @U-1")
                return f"?unknown cmd: {cmd} (try 'help')"

            # Boot to the first breakpoint, then serve commands.
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", args.dbg_port))
            srv.listen(1)
            srv.settimeout(0.5)
            (args.out / "dbg.session.json").write_text(json.dumps(
                {"port": args.dbg_port, "pid": os.getpid(),
                 "break_pc": [f"0x{p:04X}" for p in dbg_break_pcs]}), encoding="utf-8")
            log(f"[dbg] interactive session on 127.0.0.1:{args.dbg_port}; "
                f"booting to first breakpoint...")
            if not wait_hit():
                log("[dbg] detached before first break")
                interactive_stop["stop"] = True
                return
            p, bk = cur()
            log(f"[dbg] paused at {_loc(p, bk)} — drive with dbg.py")
            while not interactive_stop["stop"]:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(5.0)
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    line = data.decode("utf-8", "replace").strip()
                    resp = handle(line)
                    conn.sendall((resp + "\n").encode("utf-8"))
                except Exception as e:  # keep the session alive across client errors
                    try:
                        conn.sendall(f"ERROR: {e}\n".encode("utf-8"))
                    except Exception:
                        pass
                finally:
                    conn.close()
            try:
                srv.close()
            except Exception:
                pass

        if args.interactive:
            dbg_stop_flag = interactive_stop  # main loop watches the same flag
            dbg_thread = threading.Thread(target=_interactive_worker,
                                          name="pm-dbg-interactive", daemon=True)
        else:
            dbg_thread = threading.Thread(target=_dbg_worker,
                                          name="pm-dbg-worker", daemon=True)
        dbg_thread.start()

    # Read session events.
    _meta, events = read_session(args.session)
    log(f"[replay_host] {len(events)} switch events from session")

    # --- SetTimeFence-paced loop --------------------------------------------

    sim_t = 0.0
    step = args.sim_step
    max_t = min(args.max_sec, (events[-1].t + args.tail_sec) if events else args.max_sec)

    # Lamp / GI delta buffers. libpinmame caps these at CORE_MAXLAMPS and
    # CORE_MAXGI internally; allocate generously.
    lamp_buf = (PinmameLampState * 256)()
    gi_buf   = (PinmameGIState   * 32)()

    # Interactive sessions run until the control thread sets the stop flag
    # ('quit'), not until max_t — the CPU spends most of its time frozen at a
    # breakpoint while we issue commands, so sim_t is not a meaningful bound.
    interactive_mode = bool(args.interactive and dbg_writer is not None)
    INTERACTIVE_WALL_CAP = 7200.0  # absolute safety net (s)

    ev_idx = 0
    start_wall = time.perf_counter()

    # Gate: don't start pacing until the emulator worker has actually booted
    # (OnStateChange(1) -> running_state[0]==1). See the running_state comment
    # above for why an un-gated loop finishes in ~0.3s without emulating.
    boot_deadline = start_wall + 30.0
    while running_state[0] != 1:
        if time.perf_counter() > boot_deadline:
            log("[replay_host] WARNING: emulator did not report running within "
                "30s; starting the loop anyway (timing may be wrong)")
            break
        time.sleep(0.005)
    if running_state[0] == 1:
        log(f"[replay_host] emulator running after "
            f"{time.perf_counter() - start_wall:.2f}s; starting fence loop")
    start_wall = time.perf_counter()  # measure pacing wall-time from boot completion

    next_heartbeat_at = (float("inf") if interactive_mode
                         else (args.heartbeat_sec if args.heartbeat_sec > 0 else float("inf")))

    # Closed-loop fence sync: poll the emulator's own clock so sim_t never runs
    # ahead of emulated time (open-loop sleeping is unreliable on Windows). If
    # the DLL is too old to export the clock, fall back to the blind sleep.
    have_emu_clock     = hasattr(lib, "PinmameGetEmulationTime")
    have_fence_reached = hasattr(lib, "PinmameTimeFenceReached")
    max_inject_drift   = [0.0]    # worst |emu_t - recorded_t| at an injection (s)
    fence_stall_warned = [False]
    if not have_fence_reached:
        log("[replay_host] WARNING: DLL has no PinmameTimeFenceReached; "
            "falling back to open-loop sleep pacing (timing may drift)")

    try:
        while (not dbg_stop_flag["stop"]) if interactive_mode else (sim_t < max_t):
            # Heartbeat: one line every N sim-seconds so a long run isn't a black box.
            if sim_t >= next_heartbeat_at:
                wall = time.perf_counter() - start_wall
                log(
                    f"[hb] sim_t={sim_t:6.1f}s  wall={wall:6.1f}s  "
                    f"events={ev_idx}/{len(events)}  "
                    f"state_evts={state_event_counter[0]}  "
                    f"dmd_frames={dmd_frame_counter[0]}  "
                    f"audio_chunks={audio_chunk_counter[0]}  "
                    f"dbg_hits={dbg_hit_counter[0]}"
                )
                next_heartbeat_at += args.heartbeat_sec

            # Apply switch events whose t has elapsed. Log the emulator's real
            # clock at the instant of injection so we can see how closely each
            # switch actually landed (in emulated time) vs when it was recorded
            # -- the drift that an open-loop pacer would silently introduce.
            while ev_idx < len(events) and events[ev_idx].t <= sim_t:
                ev = events[ev_idx]
                lib.PinmameSetSwitch(ev.n, 1 if ev.on else 0)
                if have_emu_clock:
                    emu_t = float(lib.PinmameGetEmulationTime())
                    drift = emu_t - ev.t
                    if abs(drift) > max_inject_drift[0]:
                        max_inject_drift[0] = abs(drift)
                    log(f"[inject] n={ev.n} on={int(ev.on)} rec_t={ev.t:.4f} "
                        f"sim_t={sim_t:.4f} emu_t={emu_t:.4f} "
                        f"drift={drift * 1000:+.1f}ms")
                ev_idx += 1

            # Advance the fence one step, then -- crucially -- wait until the
            # emulator's clock actually reaches it before moving on. This closes
            # the loop: sim_t can never run ahead of emulated time, so the next
            # events are injected at the right emulated moment regardless of OS
            # sleep granularity. (time_fence_wait on the worker side keeps the
            # CPU pinned at the fence, so this terminates as soon as it arrives.)
            fence = sim_t + step
            lib.PinmameSetTimeFence(fence)

            # Publish sim_t for the callbacks that fire on the emulation thread
            # (OnState, OnSolenoid, OnDisplay) -- they have no clock of their own.
            last_sim_t[0] = sim_t

            if have_fence_reached:
                fence_deadline = time.perf_counter() + 2.0
                while not lib.PinmameTimeFenceReached():
                    if time.perf_counter() > fence_deadline:
                        if not fence_stall_warned[0]:
                            log("[replay_host] WARNING: emulator not reaching "
                                "the time fence (stalled/paused?); continuing")
                            fence_stall_warned[0] = True
                        break
                    time.sleep(0)  # yield; the native worker advances, then re-check
            else:
                # No fence-reached predicate: approximate by waiting a short real time.
                time.sleep(max(step * 0.5, 0.001))

            # Lamp/GI deltas. Lamps are PWM-driven and have no event
            # semantic in libpinmame; the change-batch API returns just
            # the outputs whose averaged state crossed a boundary since
            # the last call. Cheap (one ctypes call returning count + N
            # struct entries) and event-shaped (only changes written).
            if state_writer is not None:
                n = int(lib.PinmameGetChangedLamps(lamp_buf))
                for i in range(n):
                    s = lamp_buf[i]
                    state_writer.write(
                        {"t": sim_t, "kind": "lamp",
                         "n": int(s.lampNo), "v": int(s.state)}
                    )
                    state_event_counter[0] += 1
                if hasattr(lib, "PinmameGetChangedGIs"):
                    n = int(lib.PinmameGetChangedGIs(gi_buf))
                    for i in range(n):
                        s = gi_buf[i]
                        state_writer.write(
                            {"t": sim_t, "kind": "gi",
                             "n": int(s.giNo), "v": int(s.state)}
                        )
                        state_event_counter[0] += 1

            # (Solenoids: OnSolenoidUpdated callback — no work here.)
            # (Debugger trace: dedicated worker thread — no work here.)

            sim_t += step

            # Belt-and-braces real-time bound: if for some reason a single sim
            # second has been taking many wall seconds, bail out. In interactive
            # mode wall time is dominated by think-time at frozen breakpoints, so
            # use a flat absolute cap instead.
            wall_cap = INTERACTIVE_WALL_CAP if interactive_mode else args.max_sec * 4 + 60
            if time.perf_counter() - start_wall > wall_cap:
                log("[replay_host] wall-clock bound exceeded; bailing")
                break
    finally:
        # Detach the debugger first — that unblocks any in-flight CPU
        # break AND wakes up the worker thread's PinmameDebugWait. Then
        # join the worker and stop the emulator.
        if dbg_writer is not None and hasattr(lib, "PinmameDebugDetach"):
            dbg_stop_flag["stop"] = True
            try:
                lib.PinmameDebugDetach()
            except Exception:
                pass
            if dbg_thread is not None:
                dbg_thread.join(timeout=2.0)
        log("[replay_host] PinmameStop")
        try:
            lib.PinmameStop()
        except Exception:
            pass
        end_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        if state_writer is not None:
            state_writer.write({"kind": "trace_end", "end_ts": end_ts, "sim_t": sim_t})
            state_writer.close()
        if dmd_writer is not None:
            dmd_writer.write({"kind": "trace_end", "end_ts": end_ts, "sim_t": sim_t})
            dmd_writer.close()
        if sound_writer is not None:
            sound_writer.write({"kind": "trace_end", "end_ts": end_ts, "sim_t": sim_t,
                                "total_samples": audio_sample_total[0],
                                "sample_rate": audio_info["sample_rate"],
                                "channels": audio_info["channels"]})
            sound_writer.close()
        if audio_raw_file is not None:
            try:
                audio_raw_file.close()
            except Exception:
                pass
        if dbg_writer is not None:
            dbg_writer.write({"kind": "trace_end", "end_ts": end_ts, "sim_t": sim_t,
                              "total_hits": dbg_hit_counter[0]})
            dbg_writer.close()

    drift_note = (f" max_inject_drift={max_inject_drift[0] * 1000:.1f}ms"
                  if have_emu_clock else " (open-loop; no drift measurement)")
    log(f"[replay_host] done; sim_t={sim_t:.3f}s "
        f"wall={time.perf_counter()-start_wall:.1f}s{drift_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
