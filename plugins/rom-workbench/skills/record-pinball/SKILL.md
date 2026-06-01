---
name: record-pinball
description: Capture a Williams pinball gameplay session in Visual Pinball + VPinMAME, then replay it headlessly against a single ROM (factory or modded) from an explicit NVRAM snapshot, with selectable trace features — state events (lamps/solenoids/GIs), DMD frames, emulated game audio (PCM, muxable into the DMD video), and an event-driven CPU debugger (breakpoints, watchpoints, single-step) for the wpc-investigate skill. Use to record gameplay, produce NVRAM snapshots, or replay a session against a ROM to inspect (or diff) the produced traces.
---

# record-pinball

Records and replays Williams pinball gameplay sessions. The replay path is
**event-driven** — libpinmame and our patched debugger emit callbacks, and
`replay.py` writes them out. The `Dbg` trace is the in-process CPU
debugger that `wpc-investigate` uses; it lives here because the replay
infrastructure (sessions + NVRAM snapshots) is the substrate it runs on.

1. **Record a session** that reaches an in-game state of interest (multiball
   start, attract mode, a scoring shot…).
2. **Init NVRAM** once per ROM zip so replays start from explicit state.
3. **Replay headlessly** against a single ROM. Pick traces: `State`, `Dmd`,
   `Sound`, and/or `Dbg` (with `--break-pc`/`--watch-r`/`--watch-w`).
4. **Compare two runs** (optional) by diffing two replay output dirs.

## When to invoke

- "record a Congo session", "capture gameplay"
- "replay this session", "replay against the modded ROM"
- "validate this mod" / "diff the factory vs modded trace"
- "make an NVRAM snapshot for this ROM" / "init nvram"
- "set a breakpoint on $D9A6 and tell me what A is" (Dbg trace; see also `wpc-investigate`)

For first-time machine setup or per-game ROM/table registration, use the
**`pinball-setup`** skill instead. For static analysis and the investigation workflow that *uses* the `Dbg` trace,
use **`wpc-investigate`**.

## Quickstart

Assumes you've already run `pinball-setup/setup-pinball.py` and
`pinball-setup\add-rom.ps1` for your game. From a PowerShell 7 prompt
(any working directory):

```powershell
# Record a session. Looks up the table for -Rom from config.json. Press Ctrl-C to stop.
& '${CLAUDE_PLUGIN_ROOT}/record.ps1' -Rom congo_21

# One-time per ROM zip: produce a freshly-reset NVRAM snapshot so replays
# don't pay the boot-time factory-reset cost and start from explicit state.
uv run ${CLAUDE_PLUGIN_ROOT}/init_nvram.py --rom-zip .\orig\congo_21.zip         # -> .\orig\congo_21.nv
uv run ${CLAUDE_PLUGIN_ROOT}/init_nvram.py --rom-zip .\dist\congo_21_modded.zip  # -> .\dist\congo_21_modded.nv

# Replay headlessly against a single ROM with just the state-event timeline.
uv run ${CLAUDE_PLUGIN_ROOT}/replay.py --rom congo_21 --rom-zip .\orig\congo_21.zip `
    --session .\sessions\<utc> --nvram .\orig\congo_21.nv --trace state

# Investigate a code path with the in-process CPU debugger.
uv run ${CLAUDE_PLUGIN_ROOT}/replay.py --rom congo_21 --rom-zip .\dist\congo_21_modded.zip `
    --session .\sessions\<utc> --nvram .\orig\congo_21.nv `
    --trace state,dbg --break-pc 0xD9A6 --dbg-step-after 80

# Validate a mod: run the same session against the modded ROM, then (optionally)
# diff the two trace dirs.
uv run ${CLAUDE_PLUGIN_ROOT}/replay.py --rom congo_21 --rom-zip .\dist\congo_21_modded.zip `
    --session .\sessions\<utc> --nvram .\dist\congo_21_modded.nv --trace state,dmd
uv run ${CLAUDE_PLUGIN_ROOT}/replay/diff_traces.py `
    --a .\sessions\<utc>\replays\congo_21\<utc> `
    --b .\sessions\<utc>\replays\congo_21_modded\<utc> `
    --out .\sessions\<utc>\replays\diff
```

Sessions land at `.\sessions\<utc>\` relative to the current working
directory — they live next to whatever repo you ran from.

## Prerequisites

This skill expects `pinball-setup` to have run successfully — that's where
the toolchain, env vars (`PINMAME_DIR`, `VPINBALL_DIR`, `VPINMAME_DIR`),
patched DLLs, and per-game ROM/table registration come from. Specifically
needed:

- `PINMAME_DIR` env var pointing at the PinMAME install with our patched
  `libpinmame.dll` deployed.
- The ROM zip registered via `pinball-setup\add-rom.ps1`.
- `uv` on PATH (installed by `pinball-setup`). Every Python tool here runs via
  `uv run`, which provisions a matching interpreter and each script's declared
  dependencies (e.g. Pillow for DMD rendering) into an ephemeral environment —
  no manual `pip install` is ever needed.

If a script complains "PINMAME_DIR not set" or similar, point at
`pinball-setup`.

## Recording: what `record.ps1` does

```powershell
& '${CLAUDE_PLUGIN_ROOT}/record.ps1' `
    [-Rom congo_21] `                      # default: congo_21
    [-Table '<path-to-vpx>'] `             # default: from config.json
    [-OutDir '<dir>'] `                    # default: .\sessions\<UTC>
    [-MaxSeconds 600]                      # safety stop
```

Sessions are written to `.\sessions\<UTC>\` relative to the current working directory.
`record.ps1` is the Windows recorder; `record.sh` is its macOS counterpart, and both
produce an identical `session.jsonl`.

Launches Visual Pinball with the full table. The patched `VPinMAME64.dll` (deployed by `setup-pinball.py` from `bin\VPinMAME64.dll`) runs inside VP's process and captures the replayable switch stream via one env var set in the child:

- `VPINMAME_SWITCHLOG=<session>\switchlog.jsonl` — VP drives the playfield through the COM `Controller.Switch`/`put_Switches` path, which funnels through `vp_putSwitch`; the patched DLL logs every externally-driven switch *edge* there as a JSONL `"switch"` record stamped with the **emulation clock** (`timer_get_time`). When VP closes, `record.ps1` folds these into `session.jsonl` as `kind:"switch"` records (after the meta line). This is what `replay.py`/`replay_host.py` inject via `PinmameSetSwitch` — the same `swMatrix` plane VP drove, so gameplay reproduces faithfully.

**Stop recording** by closing the VP window or hitting Ctrl-C in the recording terminal. `-MaxSeconds` is a safety cap.

## NVRAM init: `init_nvram.py`

```powershell
uv run ${CLAUDE_PLUGIN_ROOT}/init_nvram.py `
    --rom-zip '<path-to-rom-zip>' `       # required
    [--rom <name>] `                      # default: zip stem with _modded/_mod stripped
    [--out '<path>'] `                    # default: <dir-of-rom-zip>\<zip-stem>.nv
    [--ack-input '<session.jsonl>'] `     # optional: switches to dismiss a reset prompt
    [--duration-sec 90] `                 # wall-clock cap on the warm-up
    [--force]                             # overwrite --out
```

A ROM zip that changes the WPC checksum word (`$FFEE`) triggers a factory reset on first boot because the existing NVRAM stored a checksum from the previous ROM version. `init_nvram.py` boots the ROM from blank NVRAM, lets the factory-reset cycle settle (default 90 s), and snapshots the resulting `<rom>.nv` to `--out`. The output is the input to `replay.py --nvram`. Some games sit on a "FACTORY RESET — PRESS ENTER" prompt waiting for input; in that case record a tiny session that issues the required button press and pass it as `--ack-input`.

The snapshot is keyed by ROM zip, so factory and modded ROMs get distinct cached NVRAMs (`orig\congo_21.nv` and `dist\congo_21_modded.nv` respectively). Re-run after rebuilding a modded ROM whose checksum word changed.

## Replay: `replay.py`

```powershell
uv run ${CLAUDE_PLUGIN_ROOT}/replay.py `
    --rom <name> `                            # required, e.g. congo_21
    --rom-zip '<path-to-rom-zip>' `           # required: factory or modded zip
    --session '<sessions/...>' `              # required: dir with session.jsonl
    --nvram '<path-to-nv>' `                  # required: from init_nvram.py
    [--trace state,dmd,sound,dbg] `           # composable subset; default: state
    [--break-pc '0xD9A6,0xD9BF'] `            # PCs to break on (dbg trace)
    [--watch-r '0x03F5'] `                    # addresses for read watchpoints
    [--watch-w '0x0401,0x03ED'] `             # addresses for write watchpoints
    [--dbg-step-after 50] `                   # single-step N instructions after each break
    [--dbg-mem '@S:2,@X:16,0x0011'] `         # dump memory at each dbg hit (see below)
    [--out-dir '<dir>'] `                     # default: <session>\replays\<zip-stem>\<utc>
    [--sim-step 0.001] `                      # simulated seconds per loop step
    [--max-sec <seconds>] `                   # default 600
    [--tail-sec 1.0] `                        # keep emulating N s past the last switch event
    [--overwrite]
```

`--tail-sec` (default 1.0) keeps the emulator running a beat past the final
switch event so the game settles and the DMD finishes its transition — without
it, a replay can cut off mid-animation right after the last input.

Single-sided: one ROM, one session, one NVRAM in; one trace directory out. The `<zip-stem>` segment in the default `-OutDir` keeps factory and modded runs side by side under the same session without clashing.

#### Resolving banked code and dereferencing pointers (`--dbg-mem`)

A register snapshot alone can't locate banked code: a PC in `$4000–$7FFF`
is ambiguous until you know which ROM page is mapped. Every `dbg` hit now also
carries:

- **`bank`** — the live ROM page, read from the WPC bank shadow at
  `(DP<<8)+0x11`. Combined with the PC it yields **`loc`** (e.g. `$42C6@p39`),
  which you paste straight into `wpc-investigate/rom.py dump`.
- **`mem`** — optional windows requested with `--dbg-mem`, read via
  `PinmameReadMainCPUByte` while the CPU is frozen. Each comma item is either
  a fixed address `0xADDR[:LEN]` or a **register-relative** read
  `@REG[+/-OFF][:LEN]` (REG ∈ `pc,s,u,x,y`):
  - `@S:2` → the return address on top of stack → **who called this routine**.
  - `@X:16` → dump the struct/string a pointer register points at.

Gotcha: memory reads go through the *current* bank, so `@X` on a banked
pointer only returns real bytes when that pointer's page is the one mapped at
the break (e.g. read a page-3C string only while bank=0x3C).

This is the reflex for *"what page is this / who called this / what does this
pointer point at"* — it's what cracked the Congo POST version-display path
(producer page `0x39`, renderer `$404F@p39`, output buffer `$0326`).

### Architecture: PinMAME drives, the host listens

The whole replay is **event-driven**. PinMAME runs the simulation on its own
thread; libpinmame fires callbacks (and our debugger fires events) when
"interesting" things happen, and `replay_host.py` writes them out. The host
does not poll the emulator for state. The only per-tick work is:

- Inject any queued switch deltas (`SetSwitch`).
- Advance `SetTimeFence(sim_t + step)`, **then block until the emulator's clock
  actually reaches that fence** (closed-loop — see "Determinism / pacing model" below).
- Drain the lamp/GI **change-batch** APIs (`GetChangedLamps`/`GetChangedGIs`)
  — these are not "polls" in the trap sense: lamps are PWM-driven so they
  have no event semantic, but the batch API returns only outputs whose
  averaged state crossed a boundary, so we still write strictly change
  records.

Everything else comes from libpinmame and the debugger via callbacks:

| Trace   | Source                                                                              | Output                                  |
|---------|-------------------------------------------------------------------------------------|-----------------------------------------|
| `State` | `OnStateUpdated` + `OnSolenoidUpdated` callbacks; lamp/GI deltas per tick           | `trace.state.jsonl`                     |
| `Dmd`   | `OnDisplayUpdated` callback                                                          | `dmd/<frame>.bin` + `dmd.index.jsonl`   |
| `Sound` | `OnAudioAvailable` + `OnAudioUpdated` callbacks (emulated game audio, PCM); auto-added with `Dmd` (use `--no-sound` to skip) | `audio/audio.s16le.raw` + `audio.index.jsonl` |
| `Dbg`   | `PinmameDebugWait` blocks on a condvar; CPU thread freezes itself at each breakpoint/watchpoint hit and signals | `trace.dbg.jsonl` |

The `Dbg` trace is the central observability primitive for CPU and memory
analysis. It supersedes the old `MemPoll` (read-byte polling) and `RegPoll`
(register-snapshot polling) trace modes, which are gone — both were
"sample at arbitrary tick boundaries" designs that missed events between
samples and captured intermediate-PC noise instead of the entry points
you actually asked about.

### Using `Dbg`: breakpoints, watchpoints, single-step

`Dbg` exposes traditional debugger semantics via four switches:

- `--break-pc '<pc>[,<pc>...]'` — break before each instruction at any of the
  listed PCs. Each hit captures a `PinmameMainCPURegs` snapshot at the
  *exact* PC (before the instruction executes), with PC, S, U, X, Y, A,
  B, CC, DP. Use this to inspect routine entries (e.g. dispatcher
  prologues) or to discover what data drives a known format-string call.

- `--watch-r '<addr>[,<addr>...]'` / `--watch-w '<addr>[,<addr>...]'` —
  break on every read / write to the listed addresses (m6809's 64 KB
  CPU address space). The event records the address, whether it was a
  read or write, the byte value at the access, plus the full register
  file. Use to find what code wakes up a specific RAM byte or memory-
  mapped I/O port.

- `--dbg-step-after N` — after a `--break-pc` hit, single-step N additional
  instructions, capturing each. This is the cheap way to inspect what
  happens immediately after a known entry point (the next ~50
  instructions of a routine, for instance) without setting a wall of
  breakpoints.

The emulation thread genuinely **stops** at each hit and waits for the
host (`replay_host.py`'s debugger worker thread) to call Continue or
Step. No events are missed even if many breakpoints fire in the same
microsecond — they queue up across thread synchronisation, with the
emulation thread blocked between hits.

A small Python-level convenience: the worker thread reads
`PinmameReadMainCPUByte(addr)` to inspect RAM at the moment of a break.
That's safe because the CPU is paused; it's emphatically not safe to
call on a free-running CPU (which is why `PinmameGetMainCPURegs`,
which encouraged exactly that misuse, has been removed).

**Determinism / pacing model.** Time keys (`t`) in session.jsonl and the trace files are **simulated seconds** (emulation clock), not CPU cycles. The driver advances simulation in fixed `--sim-step` chunks, injects pending switch deltas, drains lamp/GI deltas, and bumps the fence forward. Callbacks fired on the emulation thread between fences stamp `t` from a shared last-seen-`sim_t` cell, accurate to within `--sim-step`.

Two non-obvious correctness requirements (both fixed 2026-05-30 — an earlier open-loop version raced to the end in ~0.3s and injected every switch at emu-time 0, so nothing happened):

- **Wait for the worker to boot before pacing.** `PinmameRun` *spawns* the emulation thread and returns immediately; the machine then loads the ROM and runs `MACHINE_INIT` (which fires `OnStateChange(1)`) before `cpu_timeslice` honors time fences. The host registers the `OnStateUpdated` callback unconditionally and **blocks until state==1** before the fence loop. Post fences during the boot window and the fence APIs hit their not-running short-circuit and the loop never paces.
- **Closed-loop fence sync.** After `PinmameSetTimeFence(fence)` the host polls `PinmameTimeFenceReached()` until the emulator's clock reaches the fence, instead of sleeping a fixed wall-time (unreliable: Windows `time.sleep` granularity is ~15 ms). `PinmameTimeFenceReached()` uses the *exact* offset-corrected expression the emulator tests internally (the worker targets `fence + time_fence_global_offset`, offset = −step from the first fence), so it's the right predicate — comparing `PinmameGetEmulationTime()` to the nominal fence is wrong. `cpu_timeslice` publishes the clock into a cross-thread-readable global every timeslice because `timer_get_time()` is only valid on the emulation thread. The per-injection drift log (`[inject] … drift=±N.Nms`) and the final `max_inject_drift` confirm sync (validated sub-ms; the start button landing at emu_t=12.05 s launches the ball).

**Progress on stdout.** `replay_host.py` emits a heartbeat line every `--heartbeat-sec` (default 5) sim-seconds with `sim_t`, wall time, events consumed, state-event count, and DMD-frame count — so a long replay isn't a black box. `init_nvram.py` invokes the host with `--quiet` since its warm-up is fire-and-forget; `replay.py` does not, so the heartbeat is visible by default.

### Persistent interactive session (`--interactive` + `dbg.py`)

The breakpoint/watchpoint switches above define a *policy up front*, run once
from boot, and leave you grepping `trace.dbg.jsonl` afterward — five re-boots to
walk one routine. For iterative reverse-engineering use the **persistent
session** instead: it boots once, holds the m6809 CPU **frozen** at a
breakpoint, and serves commands over a localhost TCP socket so each probe is
decided from what the last one showed — no re-boot, state survives between
commands.

```powershell
# Launch in the background; it stays alive serving the control socket.
uv run ${CLAUDE_PLUGIN_ROOT}/replay.py --rom congo_21 `
    --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
    --nvram .\dist\congo_21_modded.nv `
    --interactive --break-pc 0x4037 [--dbg-port 47655]
# Wait for "[dbg] paused at <loc>" in its output, then drive it with dbg.py:
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py regs
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py dis @pc 12
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py mem @u 24
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py step 20
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py continue until 0x4067
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py wp add w 0x1670
uv run ${CLAUDE_PLUGIN_ROOT}/dbg.py quit          # stops the emulator, ends the session
```

Commands: `regs | mem <addr> [len] | dis [addr] [n] | step [n] |
continue [until <pc>] | bp add|del <pc> | bp list | wp add r|w <addr> |
wp del <addr> | bank | quit`. Address forms accepted anywhere an `<addr>`/`<pc>`
is expected: `0xNNNN`, `$NNNN`, `NNNN` (hex), or register-relative `@X`, `@S+2`,
`@U-1` (resolved from the frozen registers).

- `--interactive` implies `--trace dbg` and requires at least one `--break-pc`
  to pause at (e.g. `--break-pc 0x8DB3` for the RESET vector).
- The host writes `<out>/dbg.session.json` with the port + pid; `dbg.py`
  auto-discovers the port from the most recent one, so `--port` is optional.
- `dis` decodes the **live** instruction stream via the sibling
  `wpc-investigate/rom.py` disassembler. Two caveats: (1) banked `$4000-$7FFF`
  bytes are read from the ROM image at `page=bank` because
  `PinmameReadMainCPUByte` doesn't apply the WPC bank there; (2) `dis <addr>`
  uses the *current* bank, so to decode a different page use static
  `rom.py dis '$addr@pPAGE'`.

Implementation: `replay_host.py --interactive` swaps the auto-continue worker for
a socket-served control thread that owns the `Debug*` API; the main fence loop
keeps the emulator alive while the CPU is frozen between commands.

## Two-run comparison: `replay/diff_traces.py`

Two single-sided runs can be compared after the fact:

```powershell
uv run ${CLAUDE_PLUGIN_ROOT}/replay/diff_traces.py `
    --a '<run-A-OutDir>' --b '<run-B-OutDir>' `
    --out '<diff-OutDir>'
```

It produces `diff.json` (machine-readable) and `diff.html` (browser-readable) with:

- `State`: per `(kind, n)` channel, compares the value sequence; lists divergent channels.
- `Dmd`: per-frame SHA-256 comparison.

Diffs align by **event index**, not by simulated-time `t`, so different sample cadence doesn't trigger spurious divergence.

**This is an investigative tool, not the validation primitive.** Two runs that start from different NVRAM snapshots (factory vs the modded snapshot from `init_nvram.py`) can legitimately disagree on attract-mode lamps, audit counters, default high scores, etc. — none of which are caused by the patch. Prefer targeted, patch-specific assertions on the single-sided trace (e.g. "DMD row Y at frame N contains `M0DTEST`") and reach for the diff when something single-sided already looks off.

## Output locations

Sessions land at `.\sessions\<UTC>\` relative to the working directory.

```
sessions/<utc>/
├── session.jsonl              # canonical input timeline (kind:"meta" + kind:"switch")
├── session.meta.json          # convenience summary (ROM/table sha256, mode, etc.) — see "Tagging" below
├── switchlog.jsonl            # raw switch-edge log from the patched DLL (folded into session.jsonl)
└── replays/<rom-zip-stem>/<utc>/   # written by replay.py; one dir per ROM zip
    ├── roms/<rom>.zip         # the ROM zip used (copied from --rom-zip)
    ├── nvram/<rom>.nv         # the seeded NVRAM (copied from --nvram)
    ├── trace.state.jsonl      # if state
    ├── trace.dbg.jsonl        # if dbg
    ├── dmd/000000.bin ...     # if dmd
    ├── dmd.index.jsonl        # if dmd
    ├── audio/audio.s16le.raw  # if sound  (raw interleaved s16le PCM)
    └── audio.index.jsonl      # if sound  (format + per-chunk timing)
```

### Inspecting DMD frames

`replay.py --trace dmd` writes one 8-bit-luminance raw `.bin` per frame under
`<OutDir>/dmd/NNNNNN.bin` (1 byte per pixel; libpinmame upsamples the WPC
2-bit DMD to 8 bits for portability) plus metadata in `<OutDir>/dmd.index.jsonl`.

To eyeball them, use `replay/render_dmd.py` (Pillow required):

```powershell
# All frames -> <replay-OutDir>/dmd_png/
uv run ${CLAUDE_PLUGIN_ROOT}/replay/render_dmd.py <replay-OutDir>

# Just frames 0, 5, 10..20 at 4x upscale
uv run ${CLAUDE_PLUGIN_ROOT}/replay/render_dmd.py <replay-OutDir> --frames 0,5,10-20 --scale 4

# Custom output dir
uv run ${CLAUDE_PLUGIN_ROOT}/replay/render_dmd.py <replay-OutDir> --out my_pngs
```

Default scale is 4x (so the standard 128x32 DMD becomes 512x128) using
nearest-neighbour, which keeps the dot grid crisp for reading text.

For a **watchable movie** (resampled to real-time playback, with a burned-in
timecode that matches the trace/switch-log `t`), use `replay/render_dmd_video.py`
(Pillow required; encodes H.264 mp4 via ffmpeg, falls back to GIF):

```powershell
uv run ${CLAUDE_PLUGIN_ROOT}/replay/render_dmd_video.py <replay-OutDir> [--fps 30] [--scale 6]
# -> <replay-OutDir>/dmd.mp4
```

The emulated **game audio** is muxed into the mp4 automatically — the `dmd`
trace auto-captures `sound` (unless you replayed with `--no-sound`), and the
audio is time-aligned to the same `t` clock as the DMD frames (the boot phase,
which both traces collapse onto `t=0`, lines up). Pass `--no-audio` to the
renderer to skip it. GIF output can't carry audio, so it's dropped there.
The standalone PCM lives at `<OutDir>/audio/audio.s16le.raw` (mono/stereo and
sample rate per the `audio_format` record in `audio.index.jsonl`); play it
directly with e.g. `ffplay -f s16le -ar <rate> -ac <ch> audio/audio.s16le.raw`.

The DMD only emits a frame when its contents change, so frames are irregularly
spaced; this tool holds each frame until the next one's timestamp so playback is
real-time. The timecode lets you pause and call out an exact moment that lines
up with `trace.state.jsonl` and the switch log — the fastest way to eyeball
"did the recorded inputs actually start the game".

### Tagging sessions

`session.meta.json` is meant to be extended after recording with two
human-curated fields so sessions can be picked by what they exercise without
replaying them:

- `labels`: array of short kebab-case strings naming what the session hits
  (e.g. `["travicom-mode"]`, `["multiball-start", "extra-ball"]`).
- `notes`: free-form one-line description of what happens in the recording.

```json
{
  "rom": "congo_21",
  "rom_zip_sha256": "ac6800c4...",
  "mode": "VpRecord",
  ...,
  "labels": ["travicom-mode"],
  "notes": "Hit Travicom mode near the end of the recording."
}
```

Add these immediately after recording — sessions are expensive to recapture.
When picking a session to drive a replay (e.g. to
validate a patch on a particular code path), grep `sessions/*/session.meta.json`
for the relevant label first.

## Pipeline

For a typical mod-validation workflow:

1. **Identify the moment of interest.** Decide what gameplay event the mod targets (e.g. "left ramp 10 times in a row triggers extra ball"). Record a session in VpRecord mode (the default) that reaches and exercises this event.
2. **Pin down the code path on the factory ROM.** Once per ROM zip: `init_nvram.py --rom-zip .\orig\<rom>.zip` to make `<rom>.nv`. Then `replay.py --rom <rom> --rom-zip .\orig\<rom>.zip --session ... --nvram .\orig\<rom>.nv --trace state,dmd` and inspect `trace.state.jsonl` for the lamps/solenoids that fire during the event. Use `wpc-investigate\rom.py xref` to find memory references that write those lamp/solenoid addresses — those are your candidate functions.
3. **Pin the exact code with the debugger.** Re-run with `--trace state,dbg --watch-w '<addrs>'` to find every PC that writes the candidate addresses, or `--break-pc '<entry-pc>' --dbg-step-after 50` to walk a routine's prologue. Each hit captures a full register snapshot at the exact instruction; no polling, no missed events.
4. **Make the mod.** Patch the ROM byte(s) (via `build-wpc-rom`) to produce `dist\<rom>_modded.zip` whose internal layout matches the factory zip.
5. **Validate.** `init_nvram.py --rom-zip .\dist\<rom>_modded.zip --force` for the modded NVRAM, then re-run `replay.py` against the modded ROM+NVRAM with the same `--session`. Inspect the modded trace directly for the intended effect (e.g. expected DMD content at expected frames). If the patch should have *no* effect on a particular code path, `diff_traces.py` can confirm — bearing in mind the caveats in "Two-run comparison" above.

## Known limits

- **VP physics is not bit-deterministic.** A recorded session captures the **switch-edge stream VP wrote into VPM** (the `switchlog.jsonl` / `kind:"switch"` records), not the keystrokes that caused it. Re-running VP would not reproduce the same session. The skill never re-runs VP at replay time; switches go directly into libpinmame via `PinmameSetSwitch`.
- **Time keys are simulated seconds, not CPU cycles** (no cycle counter is exported by either libpinmame or VPM). Two-ROM diffs therefore align by event index, not by `t`.
- **Switch capture is edge-triggered at the `vp_putSwitch` chokepoint**, so every switch transition VP issues is logged with no polling window to miss it (this replaced an earlier per-frame `swMatrix`-diff recorder). A genuine input timing floor remains the emulation clock's resolution, but recorded edges are faithful to what VP drove.
- **`schemas/switches/<rom>.json` ships only for `congo_21`.** New ROMs trigger a discovery scan written to `<rom>.discovered.json`; promote it to `<rom>.json` after adding labels.
- **DMD frames are stored as raw `.bin`**, not PNG, to keep the replay itself free of image-library dependencies. Width/height/bits-per-pixel live in `dmd.index.jsonl`. Render to PNG with `replay/render_dmd.py` (needs Pillow); see "Inspecting DMD frames" above.

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── SKILL.md                          # this file
├── record.ps1                        # session capture (PS — launches VP, Windows)
├── record.sh                         # session capture (bash — launches VPX, macOS)
├── init_nvram.py                     # produce a freshly-reset NVRAM snapshot per ROM zip
├── replay.py                         # single-sided headless replay (+ --interactive)
├── dbg.py                            # thin client for the --interactive debugger socket
├── bin/
│   ├── VPinMAME64.dll                # patched VPinMAME — VPINMAME_SWITCHLOG switch-edge recorder (Windows record.ps1)
│   ├── libpinmame.dylib              # patched libpinmame — VPINMAME_SWITCHLOG recorder (macOS record.sh) + replay
│   ├── pinmame64.dll                 # patched libpinmame — used by replay_host.py
│   └── libpinmame.dll                # canonical-name copy of pinmame64.dll (loader alias)
├── lib/
│   ├── Common.ps1                    # Write-Step/Ok/Warn2, SHA-256, env, archive (record.ps1)
│   └── SessionSchema.ps1             # RpSessionWriter + meta reader (record.ps1)
├── replay/
│   ├── replay_host.py                # libpinmame ctypes driver — event-driven; spawns
│   │                                 #   a worker thread that blocks on PinmameDebugWait.
│   │                                 #   --interactive: socket-served frozen-CPU REPL
│   ├── diff_traces.py                # compare two replay output dirs (investigative)
│   ├── render_dmd.py                 # DMD .bin frames -> PNG stills (Pillow)
│   └── render_dmd_video.py           # DMD .bin frames -> real-time mp4 w/ timecode + muxed sound-trace audio (Pillow+ffmpeg)
└── schemas/
    ├── session.schema.json
    ├── trace.schema.json
    └── switches/
        └── congo_21.json
```

The recorder is split by OS — `record.ps1` (PowerShell) launches Visual
Pinball on Windows, `record.sh` (bash) launches VPX on macOS — because
launching the table and monitoring its process is platform-specific (the
patched-DLL env var, the window/process lifecycle). Both write the same
`session.jsonl`. The investigation-side wrappers (`replay`, `init_nvram`)
are Python because they're pure orchestration over `replay_host.py`.

## References

- libpinmame header (upstream): https://github.com/vpinball/pinmame/blob/master/src/libpinmame/libpinmame.h
- Our patched PinMAME source: the **`switch-recorder` branch off `github.com/vpinball/pinmame`** (`src/libpinmame/libpinmame.{h,cpp}`) — adds the `PinmameDebug*` API and the m6809 dispatch-loop / RM/WM hooks, the `vp_putSwitch` switch recorder (`VPINMAME_SWITCHLOG`), and the closed-loop pacing exports `PinmameGetEmulationTime`/`PinmameTimeFenceReached` (backed by `time_fence_published_time` in `cpuexec.c` and helpers in `wpc/vpintf.c`). **The prebuilt DLLs ship in `bin/` and are all you need for replay + debug of any WPC game — the source is only required to rebuild/extend them.** To rebuild: the patch set is vendored in **`pinmame-patches/`** (3 `git am`-able patches + a README with the pinned upstream base commit and apply/build steps). Clone upstream at that base, `git am` the patches, build (`pinmame_shared.vcxproj` under `build/libpinmame/` → `Release/pinmame64.dll`; the VP-side recorder is `build/vpinmame/vpinmame.vcxproj` → `VPinMAME64.dll`), then copy the DLL into `bin/` AND re-run `pinball-setup/setup-pinball.py` to deploy — forgetting the deploy makes the wrapper silently fall back to the unpatched DLL.
- PinMAME releases: https://github.com/vpinball/pinmame/releases
- Visual Pinball X releases: https://github.com/vpinball/vpinball/releases
- VPinMAME COM interface notes: https://github.com/tanseydavid/WPCResources/blob/master/PinMAME/pinmame-debugger-help.md
