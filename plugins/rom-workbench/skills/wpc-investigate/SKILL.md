---
name: wpc-investigate
description: Static and byte-level investigation of Williams Pinball Controller (WPC) ROMs with the self-contained, bank-aware rom.py tool — 6809 disassembly (dis), recursive-descent cross-reference and function discovery (xref/funcs), hex dump, byte/string search, and address↔file-offset. Use to disassemble a WPC ROM region, find who calls/references an address, locate functions, or verify patch bytes. For driving the live CPU debugger alongside this, see the wpc-debug skill.
---

# wpc-investigate

Static + byte-level reverse-engineering tools for Williams Pinball Controller
(1990s Williams/Bally) ROMs. The primary tool is **`rom.py`** — self-contained,
stdlib-only, and **bank-aware**. It is the day-to-day workhorse:

- **`dis`** — from-scratch 6809 disassembly (decodes one instruction at a time,
  so banked code stays in its page).
- **`xref` / `funcs`** — recursive-descent cross-reference and function-start
  discovery ("who calls $X", "where do functions start").
- **`dump` / `search` / `strings` / `info`** — bytes, patterns, ASCII, metadata.

This pairs with the **live CPU debugger** (in `record-pinball`, driven via the
`wpc-debug` playbook): use the debugger to establish ground truth (which page a
PC is in, runtime register/RAM values, the call chain), then feed that `loc`
straight into `rom.py dis`/`xref`.

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
```

`rom.py xref`/`funcs` answer the static "who/where" questions; the live
debugger answers the dynamic "what actually happened / which page" questions
static analysis can't. Use them together.

## Setup

Setup is a one-time install handled by the `pinball-setup` skill. It deploys:

- Visual Pinball X, PinMAME standalone, our patched libpinmame (with the Debug API). Sets `PINMAME_DIR`.
- VPinMAME COM (`regsvr32`, needs Admin once).

If anything is missing, `replay.py` prints a clear "run pinball-setup first" message.

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
stream. It decodes one instruction at a time so banked code stays in its page
(logical `$4000-$7FFF` addresses don't bleed across pages), resolves branch/JSR
targets, and annotates them with the page. This is the go-to for a quick,
paste-the-`loc`-from-a-breakpoint listing.

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
- Bank-aware recursive-descent from seeds, scoped correctly to each page.

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
└── rom.py                         # bank-aware static tool (dis/xref/funcs/dump/search/strings)
```

## References

- Our patched libpinmame (debugger API): the `switch-recorder` branch off `github.com/vpinball/pinmame` (`src/libpinmame/libpinmame.{h,cpp}`); prebuilt DLLs ship in `record-pinball/bin/`
- The `Dbg` trace driver and worker thread: `../record-pinball/replay/replay_host.py`
