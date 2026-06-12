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

## How to run it (the headless recipe)

The reusable post-script `bin/ghidra_scripts/MapBankedRom.java` builds the map, pins DP,
labels known RAM, and decompiles the target addresses to `<out>/<ADDR>.c`. It reads a
**single line-based config file** (one positional arg) — *not* many CLI args, because
`analyzeHeadless.bat`/cmd re-split on space/comma/semicolon/equals and mangle list args on
Windows. Write the config, then invoke `analyzeHeadless` with a throwaway project
(`-deleteProject` keeps it clean and idempotent).

Config file (`#` comments ok; `block`/`label`/`target` repeat):

```ini
rom=<abs path to CPU ROM image on disk>      # e.g. orig/cpu/lotrcpua.a00 — the file, not the zip
out=<abs dir for the decompiled .c files>
dp=0                                          # direct-page value; omit / "none" to skip
block=RAM:0:none:2000                         # name:cpuHex:fileHex:sizeHex; file "none" => RAM
block=page3A:4000:8000:4000                   # banked page at $4000 <- file 0x8000
block=resident:8000:18000:8000                # resident bank at $8000 <- file 0x18000
label=92F:dtrStarted                          # cpuHex:name -> readable C (not DAT_092f)
label=8E2:offeredMode
target=705C                                   # each: recursive disasm + CreateFunction + decompile
target=7A0A
```

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
   the decompiled routine with real register/RAM context to resolve anything the decompiler
   left as `UNK_*` / "Could not recover jumptable" / a SLEIGH decode gap.

Label RAM you've already mapped (the `labels` arg) so the C is readable, and keep the
worked `.c` outputs with the notes — they're cheap to regenerate but valuable to diff.

## Gotchas

- **"Unable to resolve constructor at $X"** = the 6809 SLEIGH couldn't decode the bytes at
  `$X` — either a rare/unsupported opcode or genuine inline data. The decompiler truncates
  the block there. Check `rom.py dis $X` and the live trace; if it's inline data, the real
  control flow branches around it (don't trust the linear bytes).
- **`(*(code *)((ushort)bVar * 0x100 + 0x38))()`** and friends = an indirect/computed call
  the decompiler couldn't resolve (effect dispatcher, jump table). Resolve the target live.
- **Cross-bank calls** into another banked page are wrong in a single-page map — map the
  needed page (change the `page3A` block's file offset) and re-run.
- One zip, every OS: Ghidra is Java. The only platform difference is the launcher name
  (`analyzeHeadless` vs `.bat`).
