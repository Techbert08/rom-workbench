---
name: ghidra
description: Decompile banked 6809 pinball ROM routines to C with headless Ghidra — the heavy artillery for the debug skill. Use when rom.py's linear-sweep disassembly misaligns on inline data, when a routine is a dispatch-driven coroutine task with no static xref, or when you need C-level control flow (a mode/scheduler/dispatch handler) that's too tangled to read as raw 6809. Pairs with the live debugger (replay.py) — find the routine/address there, decompile it here.
---

# ghidra

> **Orientation:** load `rom-workbench:debug` first. Ghidra is a *complement* to
> `rom.py` (static) and `replay.py`/`dbg.py` (live), not a replacement. Reach for it
> only when those two hit a wall; most RE never needs it.

## When Ghidra earns its keep (and when it doesn't)

`rom.py dis` does a **linear sweep** — it decodes bytes in order, so a table of inline
data mid-routine throws off every following instruction (`?02`, bogus `JMP <$38`, etc.).
Ghidra does **recursive descent** — it follows branches/calls from an entry, so it walks
*around* inline data and reconstructs the real basic blocks. That difference is the whole
reason to use it. Reach for Ghidra when:

- `rom.py dis` of a routine is visibly **misaligned** (illegal opcodes between sane ones,
  `PULS` with no matching `PSHS`) — the classic banked-3A/3B inline-data breakage.
- The routine is **dispatch-driven** (0 static xref — run via a scheduler `JMP ,X`, an
  indirect `JSR [,X]`, or a task table) and you need to read its logic, not just step it.
- You want **C-level** control flow for a mode handler / the scheduler / a sw-dispatch
  gate — nested branches that are painful to trace by hand.

**Don't** use it for: a quick disasm of a clean routine (`rom.py dis` is faster), finding
who calls an address (`rom.py xref`), or anything you can read off a live single-step.
Ghidra's 6809 SLEIGH also has **occasional decode gaps** ("Unable to resolve constructor
at $XXXX") on rare opcodes / genuine inline data — cross-check those spots against
`rom.py` and the live debugger.

## Prereqs

- `GHIDRA_DIR` (set by the `setup` skill → `<data>/ghidra/ghidra_*_PUBLIC`). If unset,
  run setup, or point it at an existing Ghidra install.
- A **JDK 21+** on `PATH` (JDK 25 verified). Ghidra bundles nothing; setup only warns.
- The CPU ROM as a file on disk (e.g. `orig/cpu/<rom>cpua.a00`). Headless reads the file
  directly — it does NOT use the ROM zip.

## The one thing that matters: the banked memory map

A 6809 pinball ROM is bigger than the 64 KB address space, so it's **banked**. Ghidra's
raw loader maps a flat image and silently clips it — useless. You must rebuild the real
map: **RAM**, the **banked window** (one page at a time), and the **fixed resident bank**.

**Derive the geometry from `rom.py`** (it already knows it). `rom.py dis '$ADDR@pNN' 1`
prints `file 0x.....`; the page base in the file = `fileoff - (cpuaddr - 0x4000)`. For
**Stern Whitestar** (LOTR) this gives:

| Region | CPU addr | File offset |
|---|---|---|
| RAM | `$0000-$1FFF` | — (uninitialized) |
| Banked window | `$4000-$7FFF` | page: p38=0x00000, p39=0x04000, p3A=0x08000, p3B=0x0C000, p3C=0x10000, p3D=0x14000 |
| Resident bank | `$8000-$FFFF` | 0x18000 (fixed) |

A **focused single-page** map (RAM + the page your routine lives in at `$4000` + resident
at `$8000`) is enough when the routine only calls in-page and resident addresses — which
is the common case (resident is `$8xxx-$Fxxx`). If it calls into a *different* banked page
(`$4xxx-$7xxx` not in your page), that target resolves wrong; map that page instead, or
note it and decompile it separately.

**Pin `DP=0`.** These games run with the direct-page register 0; without pinning it the
decompiler emits `in_DP * 0x100 + offset` noise for every `$00xx` access. The script does
this for you.

## The second thing that matters: the inline-argument calling convention

The #1 cause of "Ghidra keeps misaligning" on these ROMs is **not** the bank map — it's an
unmodeled calling convention. These games make far/cross-bank calls and spawn scheduler
tasks through **trampoline** routines that read their arguments from **inline data bytes
placed right after the call site**; execution resumes *after* those bytes. On LOTR:

```
  BD B3 E6        JSR  $B3E6           ; far-call trampoline
  43 01 38        <inline hi,lo,bank>  ; NOT code — $B3E6 consumes these (verb $4301 @ p38)
  35 02           PULS A               ; real code resumes HERE
```

Ghidra's recursive descent doesn't know `$B3E6` swallows 3 bytes, so it decodes `43 01 38`
as instructions (`COMA` / `JMP <$38`) and derails the whole routine — the telltale
`Could not recover jumptable` / `(*(code *)(... * 0x100 + 0x38))()` noise. **You fix this by
declaring the trampoline and its inline-arg width** (`trampoline=` / `abi=`, below). The
script then, for every call site, marks the inline bytes as data, overrides the call's
fall-through to resume after them, re-disassembles, and annotates the decoded target as a
comment in the C. It iterates to a fixpoint (one fix reveals more code → more call sites).

To find a trampoline's inline-arg width, disassemble its body: an inline-arg trampoline
reads its own return address off the stack and steps past the args — the signature is
`LDX ,S` then `LDA ,X+` (/`LDB ,X+` …) then advancing the saved return (`LEAS`/`STX`).
The count of `,X+` reads = the inline byte count. **Whitestar/LOTR confirmed set:**
`$B3E6`=3 (far-call `hi,lo,bank`), `$A233`=4 (spawn `id,bank,pcHi,pcLo`), `$A242`=3
(spawn, bank fixed `$3A`), `$A45E`=1 — all preset by `abi=whitestar`.

**`PULS regs,PC` is also handled automatically.** On the 6809 that's the standard
subroutine return, but Ghidra's model treats it as a computed jump (another bogus
`UNRECOVERED_JUMPTABLE`). The script force-overrides every `PULS/PULU …,PC` to a RETURN, so
functions close cleanly. No config needed.

## How to run it (the headless recipe)

The reusable post-script `bin/ghidra_scripts/MapBankedRom.java` builds the map, pins DP,
labels known RAM, and decompiles the target addresses to `<out>/<ADDR>.c`. It reads a
**single line-based config file** (one positional arg) — *not* many CLI args, because
`analyzeHeadless.bat`/cmd re-split on space/comma/semicolon/equals and mangle list args on
Windows. Write the config, then invoke `analyzeHeadless` with a throwaway project
(`-deleteProject` keeps it clean and idempotent).

Config file (`#` comments ok, full-line **or** trailing; `block`/`label`/`target`/
`trampoline` repeat):

```ini
rom=<abs path to CPU ROM image on disk>      # e.g. orig/cpu/lotrcpua.a00 — the file, not the zip
out=<abs dir for the decompiled .c files>
dp=0                                          # direct-page value; omit / "none" to skip
abi=whitestar                                 # preset: registers the Whitestar/LOTR trampolines
block=RAM:0:none:2000                         # name:cpuHex:fileHex:sizeHex; file "none" => RAM
block=page3A:4000:8000:4000                   # banked page at $4000 <- file 0x8000
block=resident:8000:18000:8000                # resident bank at $8000 <- file 0x18000
label=92F:dtrStarted                          # cpuHex:name -> readable C (not DAT_092f)
label=8E2:offeredMode
trampoline=B3E6:3:farcall                     # cpuHex:nInlineBytes[:fmt] — see below (abi= sets these)
follow=1                                       # also decompile called helpers to this depth (default 0)
target=705C                                   # each: recursive disasm + CreateFunction + decompile
target=7A0A
```

- **`abi=whitestar`** — registers the confirmed LOTR/Whitestar inline-arg trampolines
  (`B3E6`/`A233`/`A242`/`A45E`) and a `bankShadow` label. Use this for LOTR; add per-game
  `trampoline=` lines for others.
- **`trampoline=cpuHex:nInlineBytes[:fmt]`** — declare one inline-arg trampoline. `fmt`
  decodes the bytes into the annotation comment: `farcall` (`hi,lo,bank` → `far-call $hilo @
  p<bank>`), `spawn4` (`id,bank,hi,lo`), `spawn3` (`id,hi,lo`), or `raw` (hex dump, default).
- **`follow=<depth>`** — after fixing trampolines, also decompile every helper called (to
  this call-graph depth) into its own `<ADDR>.c`, so call sites read `FUN_xxxx()` with real
  bodies instead of `UNK_xxxx` stubs. Direct calls are always same-page or resident (cross-
  page goes via a trampoline), so following them is safe. Default 0 (targets only).

```bash
# POSIX (.bat on Windows). $GHIDRA_DIR + the plugin dir from setup.
"$GHIDRA_DIR/support/analyzeHeadless" /tmp/ghidraproj p \
  -import orig/cpu/lotrcpua.a00 -processor 6809:BE:16:default \
  -scriptPath "$CLAUDE_PLUGIN_ROOT/bin/ghidra_scripts" \
  -postScript MapBankedRom.java tools/ghidra/lotr_p3a.cfg \
  -deleteProject
```

```powershell
# Windows
& "$env:GHIDRA_DIR\support\analyzeHeadless.bat" C:\tmp\ghidraproj p `
  -import orig\cpu\lotrcpua.a00 -processor 6809:BE:16:default `
  -scriptPath "<plugin>\bin\ghidra_scripts" `
  -postScript MapBankedRom.java tools\ghidra\lotr_p3a.cfg -deleteProject
```

Read the emitted `<out>/<ADDR>.c`. The language id is `6809:BE:16:default` (the 6809 ships
inside Ghidra's `MC6800` processor module — no third-party install). A worked LOTR config
lives at `tools/ghidra/lotr_p3a.cfg` in that project.

## Workflow: pair it with the live debugger

The high-yield loop that cracked the LOTR Destroy-the-Ring start (notes/36):

1. **Live**: a write-watchpoint (`replay.py --watch-w 0xADDR --trace dbg`) catches the PC
   that writes a state byte at the real event, plus the task pointer / stack — that's the
   routine address to decompile and the page it's on.
2. **Ghidra**: decompile that address (+ helpers it calls) with the recipe above.
3. **Live again**: confirm/branch — single-step (`--break-pc`, `--dbg-step-after`) through
   the decompiled routine with real register/RAM context to resolve anything still left as
   `UNK_*` / a genuine indirect dispatch / a SLEIGH decode gap. (A *new* mid-routine
   `Could not recover jumptable` usually means an inline-arg trampoline you haven't declared
   yet — add it to the config rather than chasing it live.)

Label RAM you've already mapped (the `labels` arg) so the C is readable, and keep the
worked `.c` outputs with the notes — they're cheap to regenerate but valuable to diff.

## Gotchas

- **`Could not recover jumptable` / `(*(code *)(... * 0x100 + 0x38))()` mid-routine** = an
  inline-arg trampoline you haven't declared. The bytes after the call are args, not code.
  Identify the trampoline and add `trampoline=ADDR:N` (or `abi=whitestar`). This was *the*
  misalignment cause; once declared the script fixes every call site automatically.
- **`UNRECOVERED_JUMPTABLE` at a `PULS …,PC`** is fixed automatically (forced to RETURN). If
  you still see one, it's a genuine computed jump (effect dispatcher / real jump table) —
  resolve the target live.
- **"Unable to resolve constructor at $X"** = the 6809 SLEIGH couldn't decode the bytes at
  `$X` — a rare/undocumented opcode (e.g. `$01`/`$5B`/`$6B`) or a data table mis-entered as
  code. The decompiler truncates there. Cross-check `rom.py dis $X` + the live trace; if it's
  data, the real control flow branches around it. This is a per-site oddity, not a
  systematic break — the surrounding function still decompiles.
- **Cross-bank calls** into another banked page are wrong in a single-page map — map the
  needed page (change the `page3A` block's file offset) and re-run. (Direct JSRs are always
  same-page/resident; cross-page always goes through the trampoline ABI.)
- **One `analyzeHeadless` at a time.** The JVM can still hold the project lock when the next
  run starts, so a back-to-back loop fails silently (no fixes, stale output). Give each run
  its **own project-location dir** (`/tmp/gp_a`, `/tmp/gp_b`, …) or wait between runs; kill
  any stray `java` first.
- One zip, every OS: Ghidra is Java. The only platform difference is the launcher name
  (`analyzeHeadless` vs `.bat`).
