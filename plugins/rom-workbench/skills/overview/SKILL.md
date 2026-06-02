---
name: overview
description: End-to-end orientation for modding 1990s Williams/Bally (WPC) pinball ROMs with this toolkit — the full workflow (set up the emulator → record gameplay → synthesize a deterministic replay → reverse-engineer and patch the ROM → test in the emulator → burn to a chip) and how the build / record / synthetic-record / debug / setup skills fit together. Load this first when a user wants to change how a pinball game behaves, mod/hack a WPC ROM, or asks where to start; it loads automatically alongside any other rom-workbench skill so the big picture is always in view.
---

# overview

This is the orientation map for the `rom-workbench` toolkit: how a WPC pinball
ROM mod goes from "I want the game to do X" to a ROM you can burn and install,
and which skill owns each step. Read it once at the start of a task, then drop
into the specific skill for the step you're on.

## The mental model

A WPC pinball machine runs a single **6809 game ROM**. Everything the player sees
— scoring rules, modes, the dot-matrix display, version text — is code and data
in that ROM. Modding the game means **editing those ROM bytes** and fixing the
WPC checksum so the machine still boots.

We never guess-and-burn. The toolkit emulates the game (Visual Pinball X +
patched PinMAME) so a change can be **recorded, replayed deterministically, and
inspected** — including a before/after of the DMD — before any hardware is
touched. Two facts make this tractable:

- **Determinism via switch streams.** A "session" is just a timestamped list of
  switch edges. Replaying the same session against a ROM reproduces the same run
  every time, so you can diff factory vs. modded behavior exactly.
- **At replay time there is no physics.** Switches only change when an edge is
  authored or was captured live. The ROM still runs its real logic; nothing moves
  unless the session says so.

## The workflow (and which skill owns each step)

```
 describe the change you want
        │
        ▼
 [setup]            install the patched emulator (once per machine)
        │
        ▼
 [record]           play the game in VPX; capture a real session + NVRAM
        │
        ▼
 [synthetic-record] hand-author a session that exercises exactly the behavior
        │           (uses the real recording as a launch preamble)
        ▼
 [debug]            reverse-engineer: disassemble, cross-reference, run the live
        │           CPU debugger to find the bytes responsible
        ▼
 [build]            patch the bytes, recompute the WPC checksum, emit a ROM zip
        │
        ▼
 test in the emulator (replay + DMD video) ──► burn to EPROM ──► install
```

- **[setup]** — one-time install of Visual Pinball X + patched PinMAME. The patch
  is what enables recording and the in-process CPU debugger. Skip on day-to-day work.
- **[record]** — boot VPX, play, and capture a replayable `session.jsonl` (+ a
  reset NVRAM snapshot). Also the **host for replay and the live debugger**: every
  other skill replays sessions through it.
- **[synthetic-record]** — author a session by hand (switch-edge stream) to drive
  the ROM into a precise state deterministically, when capturing live would be
  tedious or non-reproducible.
- **[debug]** — the reverse-engineering loop: static `rom.py` (6809 disassembly,
  xref/funcs, dump/search) coupled to the live CPU debugger (breakpoints,
  watchpoints, single-step). This is where you find *what to patch*.
- **[build]** — apply JSON patch specs, fix the checksum, produce
  `dist/<rom>_modded.zip`, then validate by replaying a session against it.

## Working-directory convention

All per-mod work happens in one project directory (not in the plugin):

```
<your-mod-dir>/
├── orig/        # factory ROM zip (and extracted <table>.vbs)
├── tables/      # the Visual Pinball .vpx table
├── dist/        # built modded ROM zips
├── source/patches/   # JSON patch specs the build applies
├── sessions/    # recorded + synthetic sessions and their replays
├── names/       # ./names/<rom>.json — your switch-number → name map
└── notes/       # findings worth keeping across sessions
```

## Cross-session memory

Reverse-engineering a single behavior can span multiple sessions. **Save notes as
you go** — what switch numbers mean, which page/address a routine lives at, what a
RAM byte tracks — to `notes/` (and offer to record durable facts in memory). Pin
switch identities into `names/<rom>.json` so later work reads `pulse("travi")`
instead of `pulse(51)`.

## Picking the next skill

| The user wants to… | Go to |
|---|---|
| install / repair the toolchain | `setup` |
| play and capture gameplay, or replay/diff a session | `record` |
| fabricate a precise, repeatable input sequence | `synthetic-record` |
| find what code/bytes cause a behavior; trace a value | `debug` |
| apply a patch and produce a bootable modded ROM | `build` |
