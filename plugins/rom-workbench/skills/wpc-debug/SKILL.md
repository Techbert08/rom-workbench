---
name: wpc-debug
description: Playbook for debugging Williams Pinball Controller (WPC) ROMs — how to actually find things with the live CPU debugger and the static 6809 tools. Covers resolving banked PCs/pointers, the persistent interactive (GDB-like) session, breakpoint/watchpoint/single-step recipes, static cross-referencing and disassembly, and the known gotchas. Use when reverse-engineering a WPC ROM behaviour, tracing where a register/RAM byte comes from, or deciding which tool to reach for. The tools themselves live in record-pinball (the debugger host) and wpc-investigate (the static byte/disasm tools); this skill is the methodology that ties them together.
---

# wpc-debug

The "how to actually find things" companion to the WPC tooling. The tools live
elsewhere — this is the playbook for using them together:

- **`record-pinball`** hosts the live CPU debugger (`replay.py`, `dbg.py`): an
  event-driven and a persistent/interactive session against a recorded session.
- **`wpc-investigate`** hosts the static tools (`rom.py`): `dis` (6809
  disassembly), `xref`/`funcs` (recursive-descent cross-reference), `dump`,
  `search`, `strings`.

The CPU is a Motorola **68B09E** (6809), 2 MHz. ROM is banked: `$8000-$FFFF` is
the always-mapped system region; `$4000-$7FFF` is a 16 KB window into one of
~30 ROM pages selected by `WPC_ROM_BANK` ($3FFC).

## When to invoke

- "trace where this register / RAM byte comes from" / "what writes $XXXX?"
- "who calls / references $XXXX?" / "disassemble $XXXX@pYY"
- "set a breakpoint at $D9A6 and step it" / "hold the CPU and let me poke around"
- "this banked PC is ambiguous — which page is it?"

## The #1 thing: resolving banked code

A register snapshot (PC/S/U/X/Y/A/B/CC/DP) is **not enough** to locate code: a
PC in `$4000-$7FFF` is ambiguous until you know which page is mapped. The live
ROM bank (`WPC_ROM_BANK` @ `$3FFC`) is shadowed in RAM at **`(DP<<8)+0x11`**
(usually `$0011`; DP=0 in WPC system + most game code).

Every `dbg` hit reports `bank` (read from `(DP<<8)+0x11`) and `loc`
(`$<PC>@p<bank>` for banked PCs, `$<PC>` for system). Paste `loc` straight into
`rom.py dis` / `rom.py xref`.

## Two ways to drive the live debugger

### A. Event-driven (run once, read the JSONL)

Set breakpoints/watchpoints up front, replay from POST, analyse the trace after.
Good for "catch every hit of X over a whole boot" and watchpoint sweeps.

```powershell
# Break before each listed PC; single-step N after each hit; dump memory windows.
uv run <record-pinball>\replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --trace dbg `
  --break-pc 0x403F --dbg-step-after 30 --dbg-mem '@S:2,@X:16,0x0011'

# Find every writer/reader of a RAM slot.
uv run <record-pinball>\replay.py ... --trace dbg --watch-w '0x1670' --dbg-mem '0x0011'
```

`--dbg-mem` windows are read via `PinmameReadMainCPUByte` while the CPU is
frozen. Forms: fixed `0xADDR[:LEN]` or register-relative `@REG[+/-OFF][:LEN]`
(REG ∈ pc,s,u,x,y), resolved from that hit's registers. Highest-value uses:
- `@S:2` → top-of-stack = the **return address** → who called this routine.
- `@X:16` / `@U:16` → **dump the struct/string** a pointer points at.
- A wide `@S:48` → unwind the **call chain** (scan for `$4xxx`/`$8xxx` words).

### B. Persistent interactive session (GDB-like — prefer this for iteration)

Boots **once**, holds the CPU **frozen**, and serves commands over a socket so
the *next* probe is decided from what the *last* one showed — no re-boot per
probe, state survives between commands. This is the big lever for iterative work.

```powershell
# Launch in the background; wait for "[dbg] paused at <loc>".
uv run <record-pinball>\replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --interactive --break-pc 0x4037
# Then drive it (each call = one command; the emulator stays paused):
uv run <record-pinball>\dbg.py regs
uv run <record-pinball>\dbg.py dis @pc 12
uv run <record-pinball>\dbg.py mem @u 24
uv run <record-pinball>\dbg.py step 20
uv run <record-pinball>\dbg.py continue until 0x4067
uv run <record-pinball>\dbg.py wp add w 0x1670
uv run <record-pinball>\dbg.py quit
```

Commands: `regs | mem <addr> [len] | dis [addr] [n] | step [n] |
continue [until <pc>] | bp add|del <pc> | bp list | wp add r|w <addr> |
wp del <addr> | bank | quit`. Address forms anywhere: `0xNNNN`, `$NNNN`,
`NNNN`(hex), or register-relative `@X @S+2 @U-1` (resolved from the frozen regs).

## Static analysis (no emulator)

`rom.py` reads ROM bytes directly — fast, faithful, bank-aware. Feed it the
`loc` from any live breakpoint.

```powershell
uv run <wpc-investigate>\rom.py dis  '$4037@p39' 40   # 6809 disassembly
uv run <wpc-investigate>\rom.py xref '$43A6@p39'       # who calls/jumps to it
uv run <wpc-investigate>\rom.py xref '$1670' --data    # +LD/ST data references
uv run <wpc-investigate>\rom.py funcs --page 39        # discovered function starts
uv run <wpc-investigate>\rom.py dump '$450F@p39' 15    # raw bytes (e.g. a table)
```

`xref`/`funcs` do bank-aware recursive-descent disassembly from prologue +
vector seeds. Banked `$4000-$7FFF` targets are scoped to the source's page;
`$8000+` targets are global. **Limit:** cross-page calls route through the WPC
OS bank dispatcher (system code jumping to `$4xxx` with the page chosen at
runtime) and can't be statically attributed to a page — for those, use the live
session's stack unwind (`@S:2`, wide `@S`).

## Resolving a *banked* return address (the page is on the stack, not in the routine)

A return address in `$4000-$7FFF` recovered from the stack is **page-ambiguous**,
and — the trap — its page is usually **not** the page of the routine you unwound
it from. Cross-page calls go through a **bank-switch gate** (the `$8A04`/`$8A07`
family, `$86FC`, …): a tiny system stub that sets `WPC_ROM_BANK` (`STA $3FFC`)
and returns with `PULS CC,A,B,PC`. The gate's return frame holds **both** the
caller PC **and** the caller's ROM bank as adjacent saved bytes — so when it
pops PC it simultaneously restores the bank. To attribute the return address to
a page, read that restored-bank byte; don't assume it shares the current page.

Recipe (worked example — the routine that loads Congo's version digits):
1. At the callee entry (`--break-pc 0x4037`, A/B live), dump the gate frame:
   `dbg.py mem @s 16`. Disassemble the gate (`rom.py dis '$8A07' 12`) to learn
   its `PULS` layout, then map the frame bytes onto it.
2. For `$8A07`'s `PULS CC,A,B,PC`: the pulled **PC** = caller return; the byte
   the gate reloads into `$11`/`$3FFC` = caller **bank**. Here that gave caller
   `$42C6` with bank `0x3A` → **`$42C6@p3A`**, *not* `@p39`.
3. `rom.py funcs --page <bank>` to find the enclosing function, then `dis` it.

(This is exactly the bug that left `notes/congo-version-display.md` chasing
`$42C6@p39` — wrong page — for a whole session. Always read the gate's bank byte.)

## Picking the tool

| Question | Reach for |
|---|---|
| "what runs during X / what are the regs at PC Y" | live debugger (A or B) |
| "let me poke around from here" / iterative bisection | interactive session (B) |
| "who calls / references $X" (all paths, incl. unexecuted) | `rom.py xref` |
| "disassemble this region" | `rom.py dis` (live `loc` → static) |
| "what writes this RAM byte (executed paths)" | `--watch-w` (A) |
| "what's at this address / find this string" | `rom.py dump`/`search`/`strings` |

## Gotchas (all real-world bitten)

- **Banked reads return zero.** `PinmameReadMainCPUByte` does not apply the WPC
  bank to `$4000-$7FFF`. The interactive `dis`/`mem` work around it by reading
  the ROM image at `page=bank` (ROM doesn't self-modify, so it's faithful).
  RAM/IO (<`$4000`) and system ROM (`$8000+`) read fine live.
- **`dis <addr>` follows the *current* bank.** Right for `@pc`; to decode a
  different page use static `rom.py dis '$addr@pPAGE'` with the page spelled out.
  (Bitten decoding `$4155` while the CPU sat in a page-3C sub-call → got data.)
- **Inline-parameter data after a call.** Many WPC display syscalls are followed
  by inline parameter bytes, not the next instruction — so a "return address" on
  the stack can point at data, and a static disasm starting mid-routine
  misaligns. Establish boundaries by stepping the live CPU from a known entry.
- **Stack slots are transient.** A value the engine reads via `(U+offset)` lives
  on its frame; watchpointing that address is mostly stack noise. Trace the
  value to its *register* origin, not the stack slot.
- **Banked code requires `rom.py dis` + the live debugger.** WPC bank-switching means `$4000-$7FFF` overlays are page-specific; static tools that don't model this decode garbage. Always supply `@pPAGE` and confirm against the live debugger.

## Why a custom debugger / why Python

PinMAME is a MAME-0.76-era core; its built-in debugger is a legacy TUI, not
scriptable. The whole path is a Python driver + the patched libpinmame DLL in
one process; new observability is added by exporting from libpinmame
(the patched `switch-recorder` source; prebuilt DLLs ship in
`record-pinball/bin/`). The bank-resolution, `--dbg-mem`, and interactive
session needed **no** new DLL exports — just the existing `PinmameDebug*` +
`PinmameReadMainCPUByte`. Implementation: `record-pinball/replay/replay_host.py`.
