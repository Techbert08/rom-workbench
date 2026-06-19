---
name: debug
description: Static + live analysis of banked 6809 pinball ROMs (WPC and Whitestar) — bank-aware rom.py (6809 dis, recursive-descent xref/funcs, dump/search/strings) coupled to the live CPU debugger (replay.py --interactive + dbg.py — breakpoints, watchpoints, single-step, frozen-CPU REPL). Use to disassemble a region, find who calls/references an address, trace where a RAM byte comes from, set a breakpoint, resolve a banked PC, or verify patch bytes. For C-level logic of whole routines, see the ghidra skill first.
---

# debug

> **Orientation:** load `rom-workbench:overview` for the end-to-end workflow. This
> skill is for **targeted orientation** — confirming which code path fires, finding
> what writes a RAM byte, stepping through a specific routine. For C-level logic of
> mode handlers and schedulers, start with the `ghidra` skill's full-ROM decompile.

Reverse-engineering a 6809 pinball ROM is: decompile → orient live → confirm. Both
halves of the live side ship here:

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
- "what writes $XXXX?" / "trace where this RAM byte comes from"
- "set a breakpoint at $XXXX and step it" / "hold the CPU and let me poke around"
- "this banked PC is ambiguous — which page is it?"
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

## Sega/Stern Whitestar ROMs (e.g. Lord of the Rings) — `--platform whitestar`

Most of this toolkit was built for **Williams WPC**, but the CPU is the same
**68B09E**, so the disassembler and the live debugger (breakpoints / watchpoints
/ single-step) work on **Sega/Stern Whitestar** games too. The differences that
bite — and how the tools now handle them:

- **Multi-file ROM zip.** A Whitestar set has the 128 KB main CPU ROM
  (`*cpua*.a00`), a 512 KB display ROM (`*dsp*.a00`), 1 MB BSMT sound ROMs, and a
  2 MB `bios.u8`. `rom.py` now scores by name **and** by a valid 6809 reset
  vector, so it auto-selects the CPU ROM. (`rom.py info`'s *checksum/version*
  line is WPC-specific and meaningless here — ignore it; Whitestar uses a
  different checksum.)
- **Bank resolution.** WPC's system shadow `(DP<<8)+0x11` does not exist on
  Whitestar — the bank register (`$3200`, write-only) is mirrored by a
  *game-specific* RAM byte (**`$0243`** on LOTR). `replay.py` needs
  **`--platform whitestar`** (optionally `--bank-shadow 0xNNNN` for other games)
  so `bank`/`loc` resolve to the correct rom.py page (`page = first_page +
  (shadow & (npages-1))`). Without it the printed `bank=` is garbage on Whitestar.
  **You don't pass these by hand if the project has a `game.json`** — `replay.py`
  reads `platform`/`bank_shadow` from it (walking up from the CWD) and defaults
  the flags; an explicit flag still overrides. The `setup` skill writes that
  manifest during per-game project setup.
- **RAM reads need the handler-fallback DLL (patch `0004`).** Whitestar maps its
  whole game RAM `$0000-$1FFF` through the `ram_r` *function handler* (not a
  direct bank), so the stock `PinmameReadMainCPUByte` (which used
  `memory_get_read_ptr`) returned **0 for all RAM** — `dbg.py mem`, `--dbg-mem`,
  and the `$0243` bank-shadow read were silently zero. `pinmame-patches/0004`
  adds a `cpunum_read_byte` fallback so RAM/IO read correctly. If `mem 0x0e00`
  (the live task table) reads all-zero, your deployed `libpinmame.dll` predates
  this fix — rebuild + redeploy. (WPC is unaffected; its RAM is a direct bank.)
- **I/O map (from PinMAME `se.c`, confirmed live):** `$3000`=dedicated switches
  (active-low: D0 L-flip, D1 L-EOS, D2 R-flip, D3 R-EOS, D7 Test); `$3100`=DIP;
  `$3200`=bank reg; `$3300`/`$3400`=switch column-select / row-read;
  `$3500`/`$3600`=DMD; `$3800`=sound. Lamps `$2008-$200A`, solenoids `$2000-$2003`.
- **Switch matrix** is debounced into **zero-page**: state `$9F+col`, just-pressed
  edges `$A7+col` (so switch N → col=(N-1)/8, bit=(N-1)%8). DMD **text lives in
  the display ROM**, not the CPU ROM — the CPU triggers messages by effect ID.
- **IRQ vector is just `RTI`**; all periodic work is in the **FIRQ**.

## Live CPU debugger — two ways to drive it

### A. Event-driven (run once, read the JSONL)

Set breakpoints/watchpoints up front, replay from POST, analyse the trace after.
Good for "catch every hit of X over a whole boot" and watchpoint sweeps.

```powershell
# Break before each listed PC; single-step N after each hit; dump memory windows.
python3 ${CLAUDE_PLUGIN_ROOT}/bin/replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --trace dbg `
  --break-pc 0x403F --dbg-step-after 30 --dbg-mem '@S:2,@X:16,0x0011'

# Find every writer/reader of a RAM slot.
python3 ${CLAUDE_PLUGIN_ROOT}/bin/replay.py ... --trace dbg --watch-w '0x1670' --dbg-mem '0x0011'
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
python3 ${CLAUDE_PLUGIN_ROOT}/bin/replay.py --rom congo_21 `
  --rom-zip .\dist\congo_21_modded.zip --session .\sessions\<utc> `
  --nvram .\dist\congo_21_modded.nv --interactive --break-pc 0x4037
# Then drive it (each call = one command; the emulator stays paused):
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py regs
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py dis @pc 12
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py mem @u 24
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py step 20
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py continue until 0x4067
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py wp add w 0x1670
python3 ${CLAUDE_PLUGIN_ROOT}/bin/dbg.py quit
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
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py info                  # ROM size, version byte, checksum, RESET vec
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump '$FFEC' 16        # system-ROM address
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump '$4C0E@p37' 32    # banked: page $37, addr $4C0E
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dump 0x7FFEC 16        # raw file offset
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py search "BD 90 C4"      # byte sequence (JSR $90C4)
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py search '"Copyright"'   # ASCII string
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py strings 6 --section sys # printable ASCII runs ≥ 6 chars
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py dis '$403F@p39' 40     # 6809 disassembly (n bytes)
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py xref '$43A6@p39'       # who calls/jumps to an address
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py xref '$1670' --data    # +LD/ST data references
python3 ${CLAUDE_PLUGIN_ROOT}/bin/rom.py funcs --page 39        # discovered function starts
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

A return address in `$4000-$7FFF` recovered from the stack is **page-ambiguous**.
For WPC games, cross-page calls go through a **bank-switch gate** (`$8A04`/`$8A07`
family): a tiny stub that sets `WPC_ROM_BANK` and returns with `PULS CC,A,B,PC`. The
gate's return frame holds **both** the caller PC **and** the caller's ROM bank as
adjacent saved bytes. To attribute a banked return address to its page: `dbg.py mem @s 16`
at the callee entry, disassemble the gate to learn its `PULS` layout, and read the bank
byte the gate restores — don't assume the return shares the current page.

For Whitestar games, cross-page calls use the inline-argument trampoline ABI (`$B3E6`) —
the bank is encoded at the call site, so the live `bank` read from the debugger already
tells you the active page. See the `ghidra` skill for how the trampoline ABI works.

## What is switch N? (orienting on a live switch read)

The primary source is the switch/lamp atlas built from the operator manual during project
setup (see `setup` skill). Check `./names/<rom>.json` first. If the switch isn't there
yet: grep the table VBScript (`tables/<table>.vbs`) for `Controller.Switch(N)` /
`PulseSw N` / `SolCallback(` to find the playfield object it connects to. For definitive
confirmation, `--watch-w` on the RAM byte the feature touches and read the switch edge
immediately before the write.

## Picking the tool

| Question | Reach for |
|---|---|
| "what runs during X / what are the regs at PC Y" | live debugger (A or B) |
| "let me poke around from here" / iterative bisection | interactive session (B) |
| "who calls / references $X" (all paths, incl. unexecuted) | `rom.py xref` |
| "disassemble this region" | `rom.py dis` (live `loc` → static) |
| "what writes this RAM byte (executed paths)" | `--watch-w` (A) |
| "what's at this address / find this string" | `rom.py dump`/`search`/`strings` |
| "what is switch N physically" | atlas (`names/<rom>.json`) → `.vbs` → empirical `--watch-w` |
| "read C-level logic of a mode/scheduler/handler" | `ghidra` skill full-ROM decompile |

## When to use Ghidra alongside this skill

`rom.py dis` linear-sweeps, so inline data mid-routine misaligns every following
instruction. Dispatch-driven coroutine tasks (0 xref, run via `JMP ,X`) are painful to
read as raw 6809. **The `ghidra` skill's full-ROM decompile (done up front) gives you C
for all of this before you touch a watchpoint.** Use the live debugger here to orient —
find the PC that writes a key RAM byte, read the active bank — then return to the
decompile's `.c` file to understand the logic. Needs `setup` to have installed Ghidra.

## Suggested investigation workflow

For "I observed behaviour X, where does it come from?":

1. **Read the decompile first.** `grep` the `tools/ghidra/out_*/` tree for the RAM byte
   or lamp that changes during X. The C files show which routines read/write it and
   under what conditions.
2. **Orient live.** `--watch-w <addr>` on that RAM byte confirms which PC fires at the
   real event and which ROM page it's in. Cross-reference the `loc` with the decompile.
3. **Confirm dynamically.** Break at the candidate entry with the live debugger;
   regs tell you which branch actually fires.
4. **Trace the source.** If a value comes from a register, follow it up the call chain
   (`@S:2` unwind). `rom.py xref` finds static callers to cross-check.
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
