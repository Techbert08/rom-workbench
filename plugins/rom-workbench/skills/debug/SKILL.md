---
name: debug
description: Reverse-engineer Williams Pinball Controller (WPC) ROMs — static + byte-level analysis with the bank-aware rom.py tool (6809 dis, recursive-descent xref/funcs, dump/search/strings) coupled to the live CPU debugger (replay.py --interactive + dbg.py: breakpoints, watchpoints, single-step, a frozen-CPU REPL). Use to disassemble a region, find who calls/references an address, trace where a register or RAM byte comes from, set a breakpoint and step, resolve a banked PC, verify patch bytes, or identify what a switch number physically is.
---

# debug

Reverse-engineering a WPC ROM is one loop: **simulate → break/step → read the
disassembly at the live PC**. Both halves ship here:

- **Static (`rom.py`)** — self-contained, stdlib-only, **bank-aware**: `dis`
  (from-scratch 6809 disassembly), `xref`/`funcs` (recursive-descent
  cross-reference + function-start discovery), `dump`/`search`/`strings`/`info`.
- **Live CPU debugger** — `replay.py --interactive` + `dbg.py` drive the patched
  libpinmame Debug API against a recorded session: breakpoints, watchpoints,
  single-step, a persistent frozen-CPU REPL.

The two are coupled in code: the live debugger's `dis` imports `rom.py` so it
decodes the *actual* instruction stream at the live PC. The debugger host is the
same `replay.py` you use in the `record` skill — it lives there because the
replay substrate (sessions + NVRAM snapshots) is what the debugger runs on.

The CPU is a Motorola **68B09E** (6809), 2 MHz. ROM is banked: `$8000-$FFFF` is
the always-mapped system region; `$4000-$7FFF` is a 16 KB window into one of
~30 ROM pages selected by `WPC_ROM_BANK` (`$3FFC`).

## When to invoke

- "disassemble $XXXX@pYY" / "what's the code at this address?"
- "who calls / references $XXXX?" / "where do functions start in page YY?"
- "trace where this register / RAM byte comes from" / "what writes $XXXX?"
- "set a breakpoint at $D9A6 and step it" / "hold the CPU and let me poke around"
- "this banked PC is ambiguous — which page is it?"
- "what is switch N physically?" (interpret a live switch read / `PulseSw n`)
- "dump the bytes at $FFEE" / "convert this address to a file offset" / "verify
  this patch byte before flipping it"

## How the pieces relate

```
   rom.py  (static, bank-aware — primary)        live debugger (replay.py/dbg.py)
   ┌───────────────────────────────────┐         ┌──────────────────────────────┐
   │ dis   — disassemble a region      │  loc    │ replay.py --interactive +    │
   │ xref  — who calls/references $X   │ ◄─────── │ dbg.py: frozen CPU, regs,    │
   │ funcs — function starts           │ ground- │ mem, step, watchpoints,      │
   │ dump/search/strings — bytes       │  truth  │ the call chain (@S unwind)   │
   └───────────────────────────────────┘         └──────────────────────────────┘
                    ▲                                          │
                    └────────── feed live `loc` into ──────────┘
```

`rom.py xref`/`funcs` answer the static "who/where" questions; the live debugger
answers the dynamic "what actually happened / which page" questions static
analysis can't. Use them together — establish ground truth live (which page a PC
is in, runtime register/RAM values, the call chain), then feed that `loc`
straight into `rom.py dis`/`xref`.

## Setup

One-time install handled by the `setup` skill: Visual Pinball X + our patched
libpinmame (with the Debug API, sets `PINMAME_DIR`) + VPinMAME COM (`regsvr32`,
needs Admin once). If anything is missing, `replay.py` prints a clear "run the
setup skill first" message.

---

## The #1 thing: resolving banked code

A register snapshot (PC/S/U/X/Y/A/B/CC/DP) is **not enough** to locate code: a
PC in `$4000-$7FFF` is ambiguous until you know which page is mapped. The live
ROM bank (`WPC_ROM_BANK` @ `$3FFC`) is shadowed in RAM at **`(DP<<8)+0x11`**
(usually `$0011`; DP=0 in WPC system + most game code).

Every `dbg` hit reports `bank` (read from `(DP<<8)+0x11`) and `loc`
(`$<PC>@p<bank>` for banked PCs, `$<PC>` for system). Paste `loc` straight into
`rom.py dis` / `rom.py xref`.

## Live CPU debugger — two ways to drive it

### A. Event-driven (run once, read the JSONL)

Set breakpoints/watchpoints up front, replay from POST, analyse the trace after.
Good for "catch every hit of X over a whole boot" and watchpoint sweeps.

```powershell
# Break before each listed PC; single-step N after each hit; dump memory windows.
uv run ${CLAUDE_PLUGIN_ROOT}/bin/replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --trace dbg `
  --break-pc 0x403F --dbg-step-after 30 --dbg-mem '@S:2,@X:16,0x0011'

# Find every writer/reader of a RAM slot.
uv run ${CLAUDE_PLUGIN_ROOT}/bin/replay.py ... --trace dbg --watch-w '0x1670' --dbg-mem '0x0011'
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
uv run ${CLAUDE_PLUGIN_ROOT}/bin/replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --interactive --break-pc 0x4037
# Then drive it (each call = one command; the emulator stays paused):
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py regs
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py dis @pc 12
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py mem @u 24
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py step 20
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py continue until 0x4067
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py wp add w 0x1670
uv run ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py quit
```

Commands: `regs | mem <addr> [len] | dis [addr] [n] | step [n] |
continue [until <pc>] | bp add|del <pc> | bp list | wp add r|w <addr> |
wp del <addr> | bank | quit`. Address forms anywhere: `0xNNNN`, `$NNNN`,
`NNNN`(hex), or register-relative `@X @S+2 @U-1` (resolved from the frozen regs).

---

## Static analysis: `rom.py` (no emulator)

Self-contained, stdlib-only, bank-aware. Reads ROM bytes directly — fast,
faithful. Feed it the `loc` from any live breakpoint.

```powershell
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py info                  # ROM size, version byte, checksum, RESET vec
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump '$FFEC' 16        # system-ROM address
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump '$4C0E@p37' 32    # banked: page $37, addr $4C0E
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump 0x7FFEC 16        # raw file offset
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py search "BD 90 C4"      # byte sequence (JSR $90C4)
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py search '"Copyright"'   # ASCII string
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py strings 6 --section sys # printable ASCII runs ≥ 6 chars
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dis '$403F@p39' 40     # 6809 disassembly (n bytes)
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py xref '$43A6@p39'       # who calls/jumps to an address
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py xref '$1670' --data    # +LD/ST data references
uv run ${CLAUDE_PLUGIN_ROOT}/bin/rom.py funcs --page 39        # discovered function starts
```

Without `--rom`, auto-detects `orig/*.zip` in the working directory; otherwise
pass `--rom <path>`.

### `dis` — from-scratch 6809 disassembler

`rom.py dis '$ADDR@pPAGE' [nbytes]` decodes the WPC CPU instruction stream one
instruction at a time, so banked code stays in its page (logical `$4000-$7FFF`
addresses don't bleed across pages), resolves branch/JSR targets, and annotates
them with the page. Go-to for a quick, paste-the-`loc`-from-a-breakpoint listing.

- Feed it the `loc` from a `dbg`/interactive `regs` for ground-truth, correctly
  paged instructions. It's the same decoder the live interactive session uses
  (it imports `disasm_one` from this file).
- **Gotcha:** disassembly is only meaningful at a real instruction boundary. The
  byte after a display syscall is often inline-parameter *data*; starting a
  static listing mid-routine can misalign. Prefer stepping the live CPU from a
  known entry to establish boundaries.

### `xref` / `funcs` — recursive-descent cross-reference

`rom.py xref '$ADDR@pPAGE'` lists every instruction that calls/jumps/branches to
an address; `--data` adds extended LD/ST references. `rom.py funcs [--page PP]`
lists discovered function starts. Both run a **bank-aware recursive-descent
disassembly** from seeds (PSHS/PSHU prologues + CPU vectors), so they read real
instructions rather than grepping bytes:

- Banked `$4000-$7FFF` targets are scoped to the **source's page** (the only page
  mapped while it runs). `$8000+` targets are global (any page can reference).
- `funcs` reports only **validated call targets** (high precision); raw prologue
  bytes that land in data are used as seeds but not reported as functions.

**Limits:** (1) cross-page calls route through the WPC OS bank dispatcher (system
code jumping to `$4xxx` with the page chosen at runtime) and can't be statically
attributed to a page — use the live debugger's stack unwind (`@S:2`) for those.
(2) `--data` catches *extended* operands (`LDA $4E6D`), not immediate pointer
loads (`LDY #$450F`). (3) A function reachable only via dispatch — never an
intra-page call or a prologue — may be missed.

### Address ↔ file offset

WPC ROM layout (any size): system ROM is always the last 32 KiB at `$8000–$FFFF`.
The banked-page numbering shifts by total size:

| ROM size | Banked pages |
|---|---|
| 128 KiB | $38–$3D (6 pages) |
| 256 KiB | $34–$3D (14 pages) |
| 512 KiB | $20–$3D (30 pages) |
| 1 MiB   | $00–$3D (62 pages) |

Formula: `file_offset(page, addr) = (page - firstPage) × 0x4000 + (addr - 0x4000)`.

### Address formats `rom.py` accepts

| Format | Meaning | Example |
|---|---|---|
| `$NNNN` | System ROM ($8000–$FFFF) | `$FFEE`, `$8DB3` |
| `$NNNN@pXX` | Banked page XX | `$4C0E@p37` |
| `0xNNNNN` | Raw file offset | `0x7FFEE` |

---

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

---

## What is switch N? (orienting on a live switch read)

When a step or disassembly shows a switch read, or a table's VBScript shows
`vpmTimer.PulseSw n`, you need to know what switch N physically *is*. Three
sources, in order of authority for the switch you care about:

1. **The PinMAME driver source** — ROM ground truth for the switches the driver
   models (start, trough, slings, jets, lanes, coin door): the game's
   `src/wpc/*.c` in the PinMAME tree (for Congo, `prelim/congo.c`). It does
   **not** include most playfield targets — the prelim sim doesn't model them.
2. **The table VBScript** (`orig/<table>.vbs`) — maps physical playfield objects
   to the switch numbers the ROM reads (`Controller.Switch(n)` /
   `vpmTimer.PulseSw n`), so it covers the targets the driver omits. Extract it
   from the table once:

   ```bash
   # macOS / Linux
   VPinballX_GL --extractvbs orig/<table>.vpx      # writes orig/<table>.vbs
   # Windows
   VPinballX.exe -ExtractVBS orig\<table>.vpx
   ```

   Then grep the `.vbs` for `Controller.Switch(` / `PulseSw` to read the wiring.
3. **Empirical, from a real recording** — the definitive answer to "which switch
   *does* X". Replay a session that exercised the feature with `--watch-w` on the
   RAM the feature touches, and read the switch edge that immediately precedes
   each effect (this is how the Congo TRAVI-COM/satellite targets were pinned:
   `--watch-w 0x068F`, the two scoring hits preceded by sw52 and sw51).

(This is the same recipe the `synthetic-record` skill uses to *author* sessions
by switch name — there it's name→number to drive the ROM, here it's number→
meaning to interpret what the CPU is reading.)

## Picking the tool

| Question | Reach for |
|---|---|
| "what runs during X / what are the regs at PC Y" | live debugger (A or B) |
| "let me poke around from here" / iterative bisection | interactive session (B) |
| "who calls / references $X" (all paths, incl. unexecuted) | `rom.py xref` |
| "disassemble this region" | `rom.py dis` (live `loc` → static) |
| "what writes this RAM byte (executed paths)" | `--watch-w` (A) |
| "what's at this address / find this string" | `rom.py dump`/`search`/`strings` |
| "what is switch N physically" | driver source → `.vbs` → empirical `--watch-w` |

## Suggested investigation workflow

For "I observed behaviour X, where does it come from?":

1. **Orient.** `rom.py strings`/`search` to find the hook (a format string, a
   known constant). `rom.py xref` on it to find the code that references it.
2. **Confirm dynamically.** Break at the candidate entry with the live debugger;
   the trace/regs tell you which path actually fires and which page it's in.
3. **Read it.** `rom.py dis '<loc>'` on the confirmed `loc` to see what the
   routine does and where it loads its inputs.
4. **Trace the source.** If a value comes from RAM, `--watch-w <addr>` finds
   every writer; if from a register, follow it up the call chain (`@S:2` unwind).
   `rom.py xref` finds the static callers to cross-check.
5. **Verify patch location** with `rom.py dump`/`dis` before flipping bytes via
   the `build` skill.

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
- **Banked code requires `rom.py dis` + the live debugger.** WPC bank-switching
  means `$4000-$7FFF` overlays are page-specific; static tools that don't model
  this decode garbage. Always supply `@pPAGE` and confirm against the live debugger.

## Why a custom debugger / why Python

PinMAME is a MAME-0.76-era core; its built-in debugger is a legacy TUI, not
scriptable. The whole path is a Python driver + the patched libpinmame DLL in
one process; new observability is added by exporting from libpinmame (the
patched `switch-recorder` source; prebuilt DLLs ship in `lib/`). The
bank-resolution, `--dbg-mem`, and interactive session needed **no** new DLL
exports — just the existing `PinmameDebug*` + `PinmameReadMainCPUByte`.
Implementation: `bin/replay_host.py`.

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── skills/debug/SKILL.md   # this file
├── bin/
│   ├── rom.py              # bank-aware static tool (dis/xref/funcs/dump/search/strings)
│   ├── replay.py           # debugger host — --interactive holds the CPU frozen
│   ├── dbg.py              # thin client for the --interactive debugger socket
│   └── replay_host.py      # libpinmame ctypes driver (imports rom.py for live dis)
└── lib/                    # prebuilt patched libpinmame (Debug API)
```

## References

- libpinmame header (upstream): https://github.com/vpinball/pinmame/blob/master/src/libpinmame/libpinmame.h
- Our patched libpinmame (debugger API): the `switch-recorder` branch off
  `github.com/vpinball/pinmame` (`src/libpinmame/libpinmame.{h,cpp}`); prebuilt
  DLLs ship in `lib/`. See the `record` skill's References for the rebuild path.
