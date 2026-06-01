---
name: wpc-investigate
description: Static and byte-level investigation of Williams Pinball Controller (WPC) ROMs with the self-contained, bank-aware rom.py tool — 6809 disassembly (dis), recursive-descent cross-reference and function discovery (xref/funcs), hex dump, byte/string search, and address↔file-offset. Use to disassemble a WPC ROM region, find who calls/references an address, locate functions, or verify patch bytes. For driving the live CPU debugger alongside this, see the wpc-debug skill; Ghidra remains available as an optional heavy fallback but is NOT the primary path.
---

# wpc-investigate

Static + byte-level reverse-engineering tools for Williams Pinball Controller
(1990s Williams/Bally) ROMs. The primary tool is **`rom.py`** — self-contained,
stdlib-only, and **bank-aware**. It does the day-to-day work without Ghidra:

- **`dis`** — from-scratch 6809 disassembly (decodes one instruction at a time,
  so banked code stays in its page).
- **`xref` / `funcs`** — recursive-descent cross-reference and function-start
  discovery ("who calls $X", "where do functions start").
- **`dump` / `search` / `strings` / `info`** — bytes, patterns, ASCII, metadata.

This pairs with the **live CPU debugger** (in `record-pinball`, driven via the
`wpc-debug` playbook): use the debugger to establish ground truth (which page a
PC is in, runtime register/RAM values, the call chain), then feed that `loc`
straight into `rom.py dis`/`xref`.

> **Ghidra is a deprecated fallback, not the primary path.** Its auto-analysis
> doesn't model WPC bank-switching, so banked `$4000-$7FFF` overlays decode as
> garbage (a format engine showed up as solenoid handling). `rom.py dis` + the
> live debugger are faithful and lighter. The Ghidra pipeline (`analyze.ps1`,
> `ghidra_scripts/`) is kept only for the rare whole-image one-off; the
> `decompiled.c` artifact is no longer checked in. See "Mode 3" below.

## When to invoke

- "disassemble $XXXX@pYY" / "what's the code at this address?"
- "who calls / references $XXXX?" / "where do functions start in page YY?"
- "where does this register get loaded from?" / "what writes this RAM byte?"
  (often: `xref` to narrow it, then the live debugger to confirm)
- "dump the bytes at $FFEE" / "convert this address to a file offset"
- "verify this patch byte before flipping it"

For "set a breakpoint and step / hold the CPU and poke around", that's the live
debugger — see the **`wpc-debug`** skill (and `record-pinball` for the host).

## How the pieces relate

```
   rom.py  (static, bank-aware — primary)        live debugger (record-pinball)
   ┌───────────────────────────────────┐         ┌──────────────────────────────┐
   │ dis   — disassemble a region      │  loc    │ replay.py --interactive +    │
   │ xref  — who calls/references $X   │ ◄─────── │ dbg.py: frozen CPU, regs,    │
   │ funcs — function starts           │ ground- │ mem, step, watchpoints,      │
   │ dump/search/strings — bytes       │  truth  │ the call chain (@S unwind)   │
   └───────────────────────────────────┘         └──────────────────────────────┘
                    ▲                                          │
                    └────────── feed live `loc` into ──────────┘
        (Ghidra: optional heavy fallback for whole-image xref — see Mode 3)
```

`rom.py xref`/`funcs` answer the static "who/where" questions Ghidra used to;
the live debugger answers the dynamic "what actually happened / which page"
questions static analysis can't. Use them together; reach for Ghidra rarely.

## Setup

Setup is a one-time install handled by the `pinball-setup` skill. It deploys:

- Ghidra 12.0.4 + the c0rner/ghidra_wpc_loader extension → `%LOCALAPPDATA%\Programs\ghidra_*`. Sets `GHIDRA_INSTALL_DIR`.
- Visual Pinball X, PinMAME standalone, our patched libpinmame (with the Debug API). Sets `PINMAME_DIR`.
- VPinMAME COM (`regsvr32`, needs Admin once).

If anything is missing, the relevant entry-point script (`analyze.ps1`, `replay.py`) prints a clear "run pinball-setup first" message.

---

## Mode 3: Static analysis (Ghidra) — optional heavy fallback

> **Deprecated for day-to-day work.** Reach for this only when you need a
> whole-image decompile to read unfamiliar logic and `rom.py dis`/`xref` aren't
> enough — and never trust its banked-code output without confirming against
> `rom.py dis` or the live debugger. The `decompiled.c` artifact is **no longer
> checked in** (regenerate locally if you want it). The custom `ghidra_scripts/`
> (bank xrefs, prologue/thunk scans) are kept for that rare one-off.

### Run the pipeline

```powershell
# Project-root CWD. Drops outputs in .\analysis\ next to wherever you run it.
& '${CLAUDE_PLUGIN_ROOT}/analyze.ps1' `
    -RomZip '.\orig\<rom>.zip' `
    -ProjectDir '.\analysis' `
    -ProjectName '<rom>' `
    [-Overwrite] `
    [-NoDecompile]
```

Main artifact: `analysis\<rom>.decompiled.c` — every clean-decompiling function with a `// --- FUN_… @ <addr> ---` header. Functions whose body contains `Bad instruction` / `halt_baddata` are filtered out (probing data bytes as code).

For interactive use, open `analysis\<rom>.gpr` in `<ghidra>\ghidraRun.bat`.

### Pipeline (five post-scripts, in order)

All in `${CLAUDE_PLUGIN_ROOT}/ghidra_scripts`:

1. **`WpcBankXrefs.java`** — Harvest every default-space reference into `$4000–$7FFF`. Code refs → candidate entry points; data refs → pointer-chase 16-bit values for indirect function pointers. Probes each candidate in every `ROM_PAGE_XX` overlay. Iterates to fixpoint.
2. **`WpcPrologueScan.java`** — In code-dense overlays, scan unrecognised bytes for `PSHS <reglist>` (`0x34 + nonzero, non-0xFF reglist`). Each match → disassemble + createFunction.
3. **`WpcThunkResolve.java`** — Recover `(bank, target)` pairs from `$90C4` bank-switch thunks. Convention: `LDB #<bank> ; JSR $90C4 ; … JSR $4xxx`. Low yield on Congo (1 new function / 61 callers) — most thunk calls aren't followed by a banked JSR.
4. **`WpcDisplayScripts.java`** — Force-disassemble inline-parameter display scripts passed to `$D9A6`. The dispatcher reads a 16-bit pointer immediately after its `JSR` and adjusts the return PC past additional argument bytes, so script bodies look like data to Ghidra. Falls back to `target+1` if the first byte is in the standard-6809 illegal-opcode set (e.g. Congo's `0x05` script-type tag at `$C0CA`).
5. **`DecompileAllScript.java`** — Calls `analyzeChanges(currentProgram)` first to flush the analyzer over new functions (critical — without it ~700 fewer functions decompile cleanly). Then decompiles every function with 60s timeout, filters bad-instruction bodies, writes to output.

### Diagnostic scripts (run manually)

Not wired into the default pipeline; invoke against an existing project:

```powershell
# Per-block code/data/undef byte breakdown
& "$env:GHIDRA_INSTALL_DIR\support\analyzeHeadless.bat" `
    '.\analysis' '<rom>' -process '<rom-filename>' -noanalysis `
    -scriptPath '${CLAUDE_PLUGIN_ROOT}/ghidra_scripts' `
    -postScript WpcCoverageReport.java

# Dump inbound xrefs + surrounding context for every STA/STB $3FFC site.
# Useful when porting to a new game with different bank-switch conventions.
… -postScript WpcThunkCallers.java

# Scan every initialised byte for instructions whose 8/16-bit immediate
# operand matches one of TARGETS_8/TARGETS_16. Cheap way to find "what
# loads register A with 0x02?"
… -postScript WpcImmediateScan.java

# Dump all xrefs to a list of addresses across all spaces (default +
# every ROM_PAGE_XX overlay). Dedups by FROM address since RAM is
# mirrored into every overlay. Targets configured in the .java.
… -postScript WpcXrefDump.java
```

### Empirical results (Congo, 512 KiB)

| Pass | Coverage | Clean decompiles |
|---|---|---|
| Loader only (system ROM auto-analysis) | 61% sys-ROM, ~3% banked | 13 |
| + WpcBankXrefs | 12% total | 388 |
| + WpcPrologueScan | 22.6% total | 1,456 |
| + analyzeChanges() flush | 22.6% (same bytes) | **2,175** |

`analyzeChanges()` doesn't disassemble more bytes — it lets the analyzer wire up cross-references on existing functions, which unblocks the decompiler.

### Limitations

- **Banked coverage caps around 22% / ~2,000 functions on Congo** because most cross-bank calls go through RAM-springboard / stack-passed thunks (`BANK_SPRINGBOARD @ $0012`) whose `(bank, target)` is set up dynamically. Static pattern matching can't resolve these without real data-flow analysis.
- **`WpcPrologueScan` produces spurious functions** when `0x34 <reglist>` data bytes happen to disassemble. `DecompileAllScript`'s filter drops obvious ones but some "clean" functions are still false positives.
- **Inline-parameter dispatcher disassembly is fragile.** `WpcDisplayScripts.java` covers `$D9A6` but each WPC OS utility uses its own protocol — `$D827`, etc. would need their own pass.
- **Decompile output is approximate** for banked code, self-modifying code, and inline-parameter protocols. Always cross-check with the debugger.

---

## Mode 2: Dynamic analysis (live CPU debugger)

The event-driven and interactive CPU debuggers live in **`record-pinball`** (the
host: `replay.py`, `dbg.py`), and the methodology for driving them — breakpoint /
watchpoint / single-step recipes, the persistent frozen-CPU session, resolving
banked PCs, dereferencing pointers, the call-chain unwind, and the gotchas — is
the **`wpc-debug`** skill. Use it whenever a static read isn't enough and you
need runtime ground truth (which page a PC is in, live register/RAM values, who
actually called a routine). Feed any `loc` it reports back into `rom.py dis`/
`xref` below.

---

## Mode 1: `rom.py` — static + byte-level (primary)

Self-contained, stdlib-only, bank-aware. The day-to-day workhorse.

```powershell
python ${CLAUDE_PLUGIN_ROOT}/rom.py info                       # ROM size, version byte, checksum, RESET vec
python ${CLAUDE_PLUGIN_ROOT}/rom.py dump '$FFEC' 16            # system-ROM address
python ${CLAUDE_PLUGIN_ROOT}/rom.py dump '$4C0E@p37' 32        # banked: page $37, addr $4C0E
python ${CLAUDE_PLUGIN_ROOT}/rom.py dump 0x7FFEC 16            # raw file offset
python ${CLAUDE_PLUGIN_ROOT}/rom.py search "BD 90 C4"          # byte sequence (JSR $90C4)
python ${CLAUDE_PLUGIN_ROOT}/rom.py search '"Copyright"'       # ASCII string
python ${CLAUDE_PLUGIN_ROOT}/rom.py strings 6 --section sys    # printable ASCII runs ≥ 6 chars
python ${CLAUDE_PLUGIN_ROOT}/rom.py dis '$403F@p39' 40         # 6809 disassembly (n bytes)
python ${CLAUDE_PLUGIN_ROOT}/rom.py xref '$43A6@p39'           # who calls/jumps to an address
python ${CLAUDE_PLUGIN_ROOT}/rom.py xref '$1670' --data        # +LD/ST data references
python ${CLAUDE_PLUGIN_ROOT}/rom.py funcs --page 39            # discovered function starts
```

Without `--rom`, auto-detects `orig/*.zip`.

### `dis` — from-scratch 6809 disassembler

`rom.py dis '$ADDR@pPAGE' [nbytes]` decodes the WPC CPU (68B09E) instruction
stream **without Ghidra**. It decodes one instruction at a time so banked code
stays in its page (logical `$4000-$7FFF` addresses don't bleed across pages),
resolves branch/JSR targets, and annotates them with the page. This is the
go-to when Ghidra's auto-analysis mis-decodes a banked overlay (common on WPC),
or when you want a quick, paste-the-`loc`-from-a-breakpoint listing.

- Feed it the `loc` from a `dbg`/interactive-session `regs` and you get
  ground-truth, correctly-paged instructions.
- It's the same decoder the record-pinball interactive session uses for live
  `dis` (it imports `disasm_one` from this file).
- **Gotcha:** disassembly is only meaningful at a real instruction boundary.
  The byte after a display syscall is often inline-parameter *data*; starting a
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
- This replaces Ghidra's bank-xref / prologue-scan scripts for everyday use.

**Limits:** (1) cross-page calls route through the WPC OS bank dispatcher (system
code jumping to `$4xxx` with the page chosen at runtime) and can't be statically
attributed to a page — use the live debugger's stack unwind (`@S:2`) for those.
(2) `--data` catches *extended* operands (`LDA $4E6D`), not immediate pointer
loads (`LDY #$450F`). (3) A function reachable only via dispatch — never an
intra-page call or a prologue — may be missed.

### Address ↔ file offset

WPC ROM layout (any size): system ROM is always the last 32 KiB at `$8000–$FFFF`. The banked-page numbering shifts by total size:

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

## Suggested investigation workflow

For "I observed behaviour X, where does it come from?" (see the **`wpc-debug`**
skill for the live-debugger mechanics):

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
   `build-wpc-rom`.

---

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── SKILL.md                       # this file
├── rom.py                         # PRIMARY: bank-aware static tool (dis/xref/funcs/dump/search/strings)
├── analyze.ps1                    # Ghidra headless driver (optional heavy fallback)
└── ghidra_scripts/                # passed to Ghidra via -scriptPath (fallback only)
    ├── WpcBankXrefs.java          # pipeline step 1
    ├── WpcPrologueScan.java       # pipeline step 2
    ├── WpcThunkResolve.java       # pipeline step 3
    ├── WpcDisplayScripts.java     # pipeline step 4
    ├── DecompileAllScript.java    # pipeline step 5
    ├── WpcCoverageReport.java     # diagnostic
    ├── WpcThunkCallers.java       # diagnostic
    ├── WpcImmediateScan.java      # diagnostic: instructions with given imm operand
    └── WpcXrefDump.java           # diagnostic: xrefs to a list of addresses
```

## References

- Ghidra (pinned to 12.0.4): https://github.com/NationalSecurityAgency/ghidra
- WPC loader: https://github.com/c0rner/ghidra_wpc_loader
- Our patched libpinmame (debugger API): the `switch-recorder` branch off `github.com/vpinball/pinmame` (`src/libpinmame/libpinmame.{h,cpp}`); prebuilt DLLs ship in `record-pinball/bin/`
- The `Dbg` trace driver and worker thread: `../record-pinball/replay/replay_host.py`
