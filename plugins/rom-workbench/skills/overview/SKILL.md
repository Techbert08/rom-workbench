---
name: overview
description: End-to-end orientation for modding Stern/Sega Whitestar and Williams/Bally WPC 6809 pinball ROMs — the recommended workflow (manual + VBS atlas → full decompile → targeted blackbox → patch → test) and which skill owns each step. Load this first when starting a new mod or unsure where to begin; it loads automatically alongside any other rom-workbench skill.
---

# overview

Orientation map for the `rom-workbench` toolkit. Read once at the start of a task, then drop into the specific skill for the step you're on.

## The mental model

A pinball machine runs a single **6809 game ROM**. Modding it means **editing those ROM bytes** so the machine does what you want, then fixing the checksum so it still boots. The CPU is the same Motorola 68B09E whether the platform is Williams/Bally WPC or Sega/Stern Whitestar — the bank-switching geometry and peripheral map differ, but the disassembler, decompiler, and live debugger work the same way on both.

We never guess-and-burn. The toolkit emulates the game headlessly (patched PinMAME) so changes can be **recorded, replayed deterministically, and verified** — including a before/after of the dot-matrix display — before any hardware is touched.

**Determinism via switch streams.** A "session" is a timestamped list of switch edges. Replaying the same session against a ROM reproduces the same run every time. At replay time there is no physics — switches only change when the session says so.

## The workflow (and which skill owns each step)

```
 describe the change you want
        │
        ▼
 [setup]            install the emulator + patched libpinmame (once per machine)
                    per-game: extract VBS, build switch/lamp atlases from the manual
        │
        ▼
 [ghidra]           full-ROM decompilation → C-level view of all game logic
        │
        ▼
 [record] + [debug] targeted recordings to orient in the decompile:
        │           write-watchpoints find which code touches key RAM bytes;
        │           feed the confirmed PC back to the decompile to read the logic
        ▼
 [synthetic-record] hand-author a deterministic session that drives the ROM
        │           to the target state reproducibly
        ▼
 [build]            patch bytes, recompute checksum, emit a ROM zip
        │
        ▼
 test in emulator (replay + DMD video) ──► burn to EPROM ──► install
```

**Why decompile before live debugging?** A full-ROM decompile (one Ghidra run per bank page + resident — typically 7 runs for a 128 KiB game) produces a C-level map of everything reachable. You can read mode handlers, task schedulers, and flag-check logic directly. Live debugging then becomes targeted confirmation rather than open-ended hunting — you already know the code structure; you're confirming which branch fires at runtime and finding the right RAM addresses to watch.

- **[setup]** — one-time emulator install. Also the per-game project setup: extract the table's VBScript, rasterize the operator-manual switch/lamp matrix pages, record them in `names/<rom>.json` and `lamps/<rom>.json`. This atlas is the foundation every later skill builds on. Skip on day-to-day work once installed.
- **[ghidra]** — full-ROM decompile. Recursive descent from interrupt vectors and all known far-call/spawn targets, one ROM page at a time, with inline-argument trampolines correctly modeled. Use early — it reveals game structure before you touch the live debugger.
- **[record]** — boot the emulator, play or replay, emit traces (lamp/solenoid events, DMD frames, CPU debug). The live CPU debugger (`--interactive` + `dbg.py`) lives here because it runs on top of the replay substrate.
- **[debug]** — static `rom.py` (6809 disassembly, xref/funcs, dump/search) paired with the live debugger. Use for targeted orientation: write-watchpoints to find which code touches a key RAM byte, then return to the decompile to read the logic.
- **[synthetic-record]** — hand-author a switch-edge session to drive the ROM into a precise state deterministically, when live capture would be tedious.
- **[build]** — apply JSON patch specs, fix the checksum, produce `dist/<rom>_modded.zip`, validate by replaying a session against it.

## Working-directory convention

All per-mod work happens in one project directory:

```
<your-mod-dir>/
├── game.json               # platform (wpc/whitestar), bank_shadow, ROM name
├── orig/                   # factory ROM zip (and CPU ROM image for decompile)
├── tables/                 # VPX table + extracted <table>.vbs
├── dist/                   # built modded ROM zips
├── source/patches/         # JSON patch specs
├── sessions/               # recorded + synthetic sessions + replay outputs
├── names/<rom>.json        # switch-number → name atlas
├── lamps/<rom>.json        # lamp-number → name atlas
├── tools/ghidra/           # decompilation configs (*.cfg) + output .c files
└── notes/                  # findings across sessions
```

`game.json` records the platform and (for Whitestar) the `bank_shadow` address so `replay.py` resolves banked PCs correctly without per-call flags. The `setup` skill writes it during per-game project setup.

## Picking the next skill

| The user wants to… | Go to |
|---|---|
| install / repair the toolchain; build the switch/lamp atlas | `setup` |
| decompile game logic to C (start here for RE) | `ghidra` |
| disassemble a region, find callers, trace a RAM byte live | `debug` |
| play and capture gameplay; replay, live-debug, or diff runs | `record` |
| fabricate a precise, repeatable input sequence | `synthetic-record` |
| apply a patch and produce a bootable modded ROM | `build` |
