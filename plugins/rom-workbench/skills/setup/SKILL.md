---
name: setup
description: One-time installer for the WPC mod toolchain — Visual Pinball X + PinMAME + VPinMAME + the patched libpinmame (for record). Use only on first machine setup or when reinstalling a single component. Not needed for day-to-day mod work.
---

# setup

> **Orientation:** if you haven't already, load `rom-workbench:overview` for the
> end-to-end mod workflow (setup → record → synthesize → debug → build → test)
> and where this step fits.

One-time install. Once setup runs successfully, day-to-day work doesn't need this skill loaded — `debug` and `record` use the installed components directly.

## When to invoke

- "set up the pinball toolchain" / "install the WPC mod tools"
- "install Visual Pinball / PinMAME / VPinMAME"
- "redeploy the patched libpinmame after a rebuild"

For everyday "analyze a ROM" / "replay a session" / "set a breakpoint" requests, **skip this skill** — the install is already done on a configured machine.

## What gets installed and where

Everything lands under the plugin's **persistent data directory**
(`${CLAUDE_PLUGIN_DATA}`, i.e. `~/.claude/plugins/data/<id>/`), which survives
plugin updates and is cleaned up when the plugin is uninstalled. The heavy
artifacts (`vpinball/`, `pinmame/`, Windows `vpinmame/`, `cache/`) and a Python
`venv/` live there. (Run outside a plugin context, it falls back to a per-user
app-data root: `%LOCALAPPDATA%\rom-workbench\` on Windows,
`~/Library/Application Support/rom-workbench/` on macOS,
`~/.local/share/rom-workbench/` on Linux.)

The small `config.env` pointer file stays at the **stable** app-data location
regardless, so every tool can find it (and, through it, the data dir + venv)
without an environment variable to bootstrap.

### Windows

| Component | Where | Env var |
|---|---|---|
| Visual Pinball X 10.8.0 | `<root>\vpinball` | `VPINBALL_DIR` |
| Patched libpinmame.dll (prebuilt from `bin/`) | `<root>\pinmame` | `PINMAME_DIR` |
| VPinMAME COM (regsvr32-registered) | `<root>\vpinmame` | `VPINMAME_DIR` |
| Python venv + Pillow (for the DMD-render tools) | `<root>\venv` | — |
| Patched VPinMAME64.dll | deployed over the installed VPinMAME | — |

No PinMAME standalone is downloaded: replay loads only the self-contained
`libpinmame.dll`, which ships prebuilt in `bin/`. VPinMAME is still installed —
it supplies `bass64.dll` (a hard dependency of the patched `VPinMAME64.dll`) and
the directory layout VP's COM path expects.

### macOS

| Component | Where | Env var |
|---|---|---|
| Visual Pinball X (macOS GL build) | `<root>/vpinball` | `VPINBALL_DIR` |
| Patched libpinmame.dylib | `<root>/pinmame` | `PINMAME_DIR` |
| Python venv + Pillow (for the DMD-render tools) | `<root>/venv` | — |

On macOS the patched `libpinmame.dylib` ships prebuilt in `bin/` for the common
case; `setup-pinball.py` only **builds from source** (from `--pinmame-src`,
default `../pinmame` beside the repo) when no arch-matched prebuilt is available.
Env vars are written to `~/.zshenv` and `~/.bash_profile`.

## Setup scripts

### `setup-pinball.py` — cross-platform toolchain installer

One stdlib-only Python script handles **both** macOS and Windows (replacing the
old `setup-pinball.sh` / `setup-pinball.ps1` pair). It requires Python 3.9+ and
pip on PATH; run it directly, passing the plugin data dir so the install lands
there:

```bash
python3 '${CLAUDE_PLUGIN_ROOT}/bin/setup-pinball.py' --plugin-data '${CLAUDE_PLUGIN_DATA}' [--force] [--pinmame-src <path>]
```

Everything installs under the plugin's **persistent data directory** (no admin
needed for the files themselves), with `vpinball/`, `pinmame/`, a `venv/` and —
on Windows — `vpinmame/` underneath, plus a `cache/`:

| Context | Install root |
|---|---|
| Plugin (normal) | `${CLAUDE_PLUGIN_DATA}` = `~/.claude/plugins/data/<id>/` |
| Fallback (macOS) | `~/Library/Application Support/rom-workbench/` |
| Fallback (Windows) | `%LOCALAPPDATA%\rom-workbench\` |
| Fallback (Linux) | `$XDG_DATA_HOME` (or `~/.local/share`)`/rom-workbench/` |

`--plugin-data` defaults to the `CLAUDE_PLUGIN_DATA` env var; override the whole
root with `--install-root`. Idempotent; pass `--force` to re-download/rebuild
(also recreates the venv).

Steps:
1. **Create the venv and install Pillow + PyMuPDF** — confirm `pip` is available for the launching interpreter, create a virtual environment at `<root>/venv`, then `pip install pillow pymupdf` **into that venv** (Pillow for the DMD-render tools; PyMuPDF to rasterize operator-manual matrix pages when building per-game atlases). Every tool re-execs itself into this venv on startup (`workbench_env.bootstrap_venv()`), so they resolve no matter which `python3` launched it — on Windows or POSIX.
2. **Visual Pinball X** — download + install into `<root>/vpinball/` (skips gracefully if the pinned release has no asset for this OS/arch; replay doesn't need VPX).
3. **Patched libpinmame** — deploy the prebuilt patched library from `bin/` into `<root>/pinmame/` (replay loads it via ctypes; it's self-contained, so nothing is downloaded).
   - **macOS** — install `libpinmame.dylib` (prefer the arch-matched prebuilt in `bin/`; otherwise build from `--pinmame-src`, default `../pinmame` beside the repo). Also deploy it into the VPX bundle, ad-hoc re-sign, and run a Gatekeeper trial-launch.
   - **Windows** — copy `lib/libpinmame.dll` into `<root>/pinmame/`. Then download VPinMAME COM into `<root>/vpinmame/` (for `bass64.dll` + the layout), deploy the patched `VPinMAME64.dll`, and `regsvr32`-register it so VPX's COM `VPinMAME.Controller` loads **our** patched build (the switch recorder `record.py` depends on). Registration is forced when a *different* VPinMAME DLL currently owns the COM server — a common cause of "recording produced no switches" — and skipped only when ours is already registered. Needs Administrator once; the script triggers a UAC prompt and waits, printing the exact manual relaunch command if elevation is declined.
4. **Persist the install dirs** — `VPINBALL_DIR`, `PINMAME_DIR`, and (Windows) `VPINMAME_DIR` — two ways:
   - As user-scope env vars, for interactive shells: macOS/Linux write `~/.zshenv` and `~/.bash_profile`; Windows writes the user environment (HKCU).
   - Into `config.env` at the **stable** platform-default app-data dir (`%LOCALAPPDATA%\rom-workbench\config.env`, `~/Library/Application Support/rom-workbench/config.env`, or `$XDG_DATA_HOME/rom-workbench/config.env`) — note this stays at the app-data location even though the artifacts now live under `${CLAUDE_PLUGIN_DATA}`. The file also records `CLAUDE_PLUGIN_DATA` itself, which is how each tool locates the venv to re-exec into (that variable is *not* in the ambient shell the tools are launched from). Every entrypoint calls `workbench_env.load_config()` at startup to read it, so the toolchain works in a fresh shell that never inherited the user env vars — no "open a new terminal" step. An explicit shell export still wins over the file.

Downloads are SHA-256 verified against a sidecar recorded on first use (trust-on-first-use). To pin upstream hashes, fill in the empty `expected_sha` constants near the top of the script.

### Per-game files: working-dir convention (no registration step)

There's no per-game install command. `record.py` finds a game's files by
convention from the working directory:

| File | Looked for at | Override |
|---|---|---|
| ROM zip | `./orig/<rom>.zip`, then `./dist/<rom>.zip` | `--rom-zip <path>` |
| VPX table | `./tables/<rom>.vpx` | `--table <path>` |

`record.py` stages the ROM zip into VP's `roms/` dir (`$PINMAME_DIR/roms` on
macOS, `%VPINMAME_DIR%\roms` on Windows) itself, just before launch — VP loads
ROMs from there by gamename, so it only has to live there at record time. On
**Windows** the full VPX build finds ROMs through the VPinMAME COM server, which
reads them from the registry (`HKCU\…\Visual PinMame\globals\rompath`), *not* an
env var — so `record.py` points `rompath` (plus `nvram_path`/`cfg_path`) at that
staged dir just before launch and restores the previous values afterward, so a
recording always finds the ROM no matter what other VPinMAME install last touched
the registry. Drop a game's files into `./orig/` and `./tables/` and run
`record.py --rom <name>`; nothing to register.

## Per-game project setup (the atlas + manifest)

Beyond dropping files in by convention, each mod project benefits from a one-time
**project setup** that the later skills (record / synthetic-record / debug) then
rely on, instead of each re-deriving the same facts. It produces three things in
the working directory:

| Artifact | File | What it is |
|---|---|---|
| **Game manifest** | `./game.json` | Platform + the flags the tools need (schema: `schemas/game.schema.json`). |
| **Switch atlas** | `./names/<rom>.json` | Switch # → name (schema: `schemas/names.schema.json`). |
| **Lamp atlas** | `./lamps/<rom>.json` | Lamp # → name/descriptor (schema: `schemas/lamps.schema.json`). |

Two of the steps are mechanical; the atlas-building is **judgment work you (Claude)
do** — VBScripts are author-specific and there is no reliable mechanical extractor,
so don't try to write one. Read the table's VBS and the manual and record what you
find.

### Step 1 — Stage the table + extract the VBS (mechanical)

If the table arrived zipped, unzip it into `./tables/`. Then extract its VBScript
once — it's the source of truth for switch/solenoid/lamp wiring, and extracting it
here means no other skill has to:

```powershell
# Windows
& "$env:VPINBALL_DIR\VPinballX.exe" -ExtractVBS "tables\<table>.vpx"   # writes tables\<table>.vbs
```
```bash
# macOS / Linux
"$VPINBALL_DIR/VPinballX_GL" --extractvbs tables/<table>.vpx           # writes tables/<table>.vbs
```

### Step 2 — Write `./game.json` (mechanical-ish; captures the platform flags)

This is the mechanism that makes the disassembler/simulator "do the right thing"
without you remembering per-call flags. The **platform** (`wpc` vs `whitestar`)
and, for Whitestar, the **`bank_shadow`** RAM address determine how banked ROM
addresses resolve. Record them once:

```json
{ "rom": "lotr", "platform": "whitestar", "bank_shadow": "0x0243",
  "title": "The Lord of the Rings", "manufacturer": "Stern", "year": 2003,
  "table": "tables/<table>.vpx", "vbs": "tables/<table>.vbs",
  "ipdb": 4858, "manual_url": "https://archive.org/.../<game>_djvu.txt" }
```

`replay.py` reads `platform`/`bank_shadow` from `game.json` (walking up from the
CWD, so it works from `sessions/` too) and defaults `--platform`/`--bank-shadow`
to them — an explicit CLI flag still overrides. `rom.py` already auto-detects
Whitestar ROM geometry from file sizes, so the disassembler needs no flag.

**Telling the two platforms apart:** WPC = Williams/Bally, ~1990–1999, ROM zip is
a single `*.bin`-style dump. Whitestar = Sega/Stern, ~1995–2006, the ROM zip holds
multiple files including a `*cpu*.aNN` main image (e.g. `lotrcpua.a00`) and often a
`*bios*`/`s2*` sound set. The `bank_shadow` for Whitestar (the game-RAM address
mirroring the `$3200` bank register) is **game-specific**; `0x0243` is verified for
LOTR. To find it for another Whitestar title, use the debugger: watch writes around
the known LOTR value and correlate with bank switches, or trace the routine that
writes `$3200`.

### Step 3 — Build the switch + lamp atlases (judgment work — you do this)

The atlases are how later work reads `pulse("ring_scoop")` and "is the
`mode_start` lamp lit" instead of magic numbers. Build them incrementally; a
partial atlas is useful. Sources, in order of reliability:

**A. The table VBS (`tables/<table>.vbs`)** — the wiring, but author-specific:
- **Switches:** grep for `SolCallback(`, `vpmDictateSol`, `Controller.Switch(`,
  `PulseSw`, `swCopy`, and the `Sub`/object names they call (e.g. `SolTower`,
  `bsTL.SolOut`). These tie a switch/solenoid **number** to a playfield object
  whose name often reveals identity. Dedicated/flipper switches show up as named
  constants (`sLRFlipper`).
- **Lamps:** find `Sub UpdateLamps` (or a `LampCallback`). Many tables list
  `Lamp N, lN` where `N` is the PinMAME lamp number and `lN` is a VPX light
  object — generic, so the number is confirmed but the **identity isn't**; that
  comes from the manual or empirics. Some tables instead name the light objects
  descriptively or position them on the playfield (correlate to identity).
- Expect every table to differ. Use the VBS to enumerate the **numbers that
  exist** and any names the author left; don't expect it to hand you identities.

**B. The operator manual's Switch/Lamp Matrix pages — THE primary source.**
This is far superior to VBS guessing or empirical probing: the matrix pages name
every switch and lamp at its grid position, and the numbers map **directly** to the
PinMAME numbers the tools use. Process:

1. **Get the manual as a PDF.** Find it via **IPDB** (`ipdb.org/machine.cgi?id=<id>`)
   or search `"<manufacturer> <game> pinball manual pdf"` (`sternpinball.com`,
   `pinballrebel.com`, archive.org). Save it (e.g. to `~/Downloads`). Prefer the PDF
   over djvu-text: the matrix grids are graphical and OCR-text of them is jumbled.
2. **Ask the user which pages hold the matrices** — "which PDF page is the Switch
   Matrix, and which is the Lamp Matrix?" (or which page range to scan). The user
   flipping to the right page is far cheaper than you rasterizing dozens of pages
   blind. For LOTR these were **page 6 (switches)** and **page 7 (lamps)**.
3. **Rasterize just those pages/regions to PNG and read them visually.** Use
   **PyMuPDF** (`pip install pymupdf` — a self-contained wheel; `pdftoppm`/`pdftotext`
   are often not installed and OCR mangles grids). Render **quadrant crops at 6–8×
   zoom** so cell text is legible; full pages at low zoom are unreadable:
   ```python
   import fitz
   doc = fitz.open(r"C:/Users/<you>/Downloads/<game>-Manual.pdf")
   page = doc[6]                                  # 0-indexed: PDF page 7
   r = page.rect
   clip = fitz.Rect(0, r.height*0.09, r.width*0.5, r.height*0.24)   # top-left quadrant
   page.get_pixmap(matrix=fitz.Matrix(7,7), clip=clip).save(r"C:/.../crop.png")
   ```
   Then Read the PNG (the Read tool shows images visually). Walk the grid
   column-by-column / row-by-row, anchoring on the column-drive headers (Qn / Un)
   and the small per-cell number boxes.
4. **Derive the cell→number formula from the visible cell numbers, don't assume it.**
   The two matrices index OPPOSITELY and it's table-family-specific:
   - **Stern/Sega Whitestar switches: column-major** `switch = (col-1)*8 + row`
     (8×8 = 1-64); dedicated flipper switches are separate (81-84).
   - **Stern/Sega Whitestar lamps: row-major** `lamp = (row-1)*8 + col`
     (8 cols × 10 rows = 1-80); numbers >80 are flashers/aux.
   Read a few labelled cells (e.g. row 1 = 1,2,3,4… vs 1,9,17…) to confirm which
   way it runs before trusting the rest. These numbers are the PinMAME numbers.

**C. The table VBS (`tables/<table>.vbs`)** — secondary, fills gaps the manual omits
and confirms which numbers actually exist:
- **Switches:** grep `SolCallback(`, `Controller.Switch(`, `PulseSw`, and the object
  names they call (`bsTL.SolOut`, `SolTower`) — ties a number to a playfield object.
- **Lamps:** `Sub UpdateLamps` lists `Lamp N, lN` (N = PinMAME number, lN = generic
  VPX light object — confirms the number exists; identity comes from the manual).
- Author-specific; use it to enumerate numbers, not to name them.

**D. Empirical confirmation (the tiebreaker).** Run a session with `--trace state`
(+`dmd` for context) and read `trace.state.jsonl`: each `{"kind":"lamp","n":N,...}`
(and `sw`) uses the same PinMAME number. Integrate a lamp's on-time over a window
where it should be lit (from the DMD) vs a contrast window. **Cross-check the matrix
against known switches** (e.g. confirm sw9=LEFT VUK, the jets, slings) — if the
empirical switches match the matrix, the lamp numbering is the same direct mapping.
Beware lamp PWM/flashing and shots needing entrance+made switch pairs (a lone pulse
won't "make" a ramp/orbit), which make pure probing unreliable — the matrix is why
this step is a *check*, not the primary method.

Record findings as you go (`verified: true` once empirically confirmed) and pin
durable identities into the atlases so the next session starts ahead. (Worked
example: the LOTR project's `notes/15` + `names/lotr.json` + `lamps/lotr.json`.)

## Acquiring game files (ROM zip + VPX table)

Each game needs **two** files: a ROM zip and a VPX table. Neither is auto-downloaded.

| File | What it is | Where it comes from |
|---|---|---|
| **ROM zip** (e.g. `congo_21.zip`) | The 6809 game ROM dump VPinMAME runs. Same shape PinMAME has used for 20+ years. | VPForums "ROMs" section, sometimes bundled with VPX downloads. |
| **VPX table** (`.vpx`, sometimes inside a `.zip`) | The VP playfield — physics, art, lamps, VBScript that wires keys → switches and tells VPinMAME which ROM to load. Must be `.vpx` (Visual Pinball X), not `.vpt` (older VP9) or `.fpt` (FuturePinball). | VPUniverse or VPForums table sections — community-authored. |

### Workflow when a user asks for game files

1. **Confirm the game.** Williams/Bally WPC (1990s) or Sega/Stern Whitestar
   (e.g. Lord of the Rings) — both share the 6809 core; set `platform` in
   `game.json` accordingly (see "Per-game project setup" above).

2. **Send the user to one of these sites** (both free, one-time account):
   - **VPUniverse** — `https://vpuniverse.com/` — VPX Tables → search.
   - **VPForums** — `https://www.vpforums.org/` — ROMs / Tables sections.

3. **Tell them what to look for.**
   - **Table:** prefer a recent VPW (VPin Workshop) build or a highly-rated "complete" version. Avoid "patch" / "update" downloads — those layer on top of a base release. Direct `.vpx` or a `.zip` containing one; optional `.directb2s` / `.altcolor` / `.pup` can come later.
   - **ROM:** named after the PinMAME ROM ID (e.g. `congo_21.zip`, `tz_94h.zip`). The VPX usually states which revision it's scripted against — pick that.

4. **Save in `~/Downloads`** (i.e. `C:\Users\<user>\Downloads\`). User pings back when done.

5. **Claude extracts the `.vpx` from its `.zip`** if needed and drops the files
   into the game working dir by convention: ROM zip → `./orig/<rom>.zip`, table →
   `./tables/<rom>.vpx`. Then `record.py --rom <rom>` picks them up.

### Legality and authorship

- **PinMAME ROMs**: ROM dumps. Community convention is that personal use for games you own is uncontroversial; the sites have been distributing them for ~20 years; legal status is the same as any other ROM dump (gray). The skill takes no position.
- **VPX tables**: 100% community-authored, freely redistributed by their authors. The VPW team has been the modern gold standard for Williams/Bally table mods.

## Redeploying the patched DLLs after a rebuild

The patched DLLs ship prebuilt in `lib/` and are sufficient for
day-to-day use — you only rebuild to **extend** them (a new export, a debugger
tweak). Rebuilding needs the patched PinMAME source: the `switch-recorder`
branch off `github.com/vpinball/pinmame` (it adds the `PinmameDebug*` API, the
m6809 hooks, the `vp_putSwitch` recorder, and the closed-loop pacing exports).
Clone it to a local directory of your choosing and point `$PinmameSrc` at it.
(Maintainer note: the exact clone path for this project is recorded in Claude's
project memory.)

```powershell
$PinmameSrc = 'C:\path\to\your\pinmame'   # your local clone of the patched branch

# Rebuild (from any PowerShell)
& 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\amd64\MSBuild.exe' `
    "$PinmameSrc\build\libpinmame\pinmame_shared.vcxproj" `
    /p:Configuration=Release /p:Platform=x64 /m /nologo

# Copy into the plugin lib/ under the canonical loader name and re-run
# setup-pinball.py — its deploy step (re)installs the DLL into PINMAME_DIR.
Copy-Item "$PinmameSrc\build\libpinmame\Release\pinmame64.dll" `
          ${CLAUDE_PLUGIN_ROOT}\lib\libpinmame.dll -Force
python3 '${CLAUDE_PLUGIN_ROOT}/bin/setup-pinball.py' --plugin-data '${CLAUDE_PLUGIN_DATA}'
```

If you'd rather skip the downloads/env checks entirely, the patched DLL just needs to land on top of the installed one: copy it directly over `${CLAUDE_PLUGIN_DATA}\pinmame\libpinmame*.dll`.

Forgetting the deploy step makes `replay.py` fall back to the un-patched DLL. The giveaway error is `PinmameDebugAttach not found`.

## Prerequisites

You need **Python 3.9+ and pip** on PATH. `setup-pinball.py` runs directly under
that interpreter and uses it to build the toolkit `venv/` (into which Pillow is
installed). Every day-to-day tool is still launched as `python3 <tool>.py`, but
re-execs itself into that venv on startup, so the launching interpreter only
needs to be able to *create* a venv. Additionally:

### Windows
- **One Administrator PowerShell** for the one-time `regsvr32 VPinMAME.dll` step.

### macOS
- **cmake 3.25+**, **Xcode Command Line Tools** (`xcode-select --install`), **git** — only needed for the rare libpinmame *source-build* fallback (the prebuilt `lib/libpinmame.dylib` covers the common case). `setup-pinball.py` checks these only when it actually has to build.

The third-party Python dependencies are Pillow (DMD-render tools) and PyMuPDF (rasterizing operator-manual matrix pages for the atlases), installed into the venv by `setup-pinball.py`; everything else is stdlib-only.

## File layout

The plugin ships its code under `${CLAUDE_PLUGIN_ROOT}`; setup writes the
installed toolchain + venv under the persistent data dir `${CLAUDE_PLUGIN_DATA}`:

```
${CLAUDE_PLUGIN_ROOT}/           # shipped with the plugin (read-only)
├── skills/setup/SKILL.md        # this file
├── bin/
│   ├── setup-pinball.py         # cross-platform VP + libpinmame (+ VPinMAME on Windows) installer
│   └── workbench_env.py         # shared config + venv bootstrap (bootstrap_venv)
└── lib/                         # prebuilt patched libraries this script deploys
    ├── libpinmame.dylib         # macOS
    ├── libpinmame.dll           # Windows
    └── VPinMAME64.dll           # Windows (VPinMAME COM recorder)

${CLAUDE_PLUGIN_DATA}/           # written by setup; survives plugin updates
├── venv/                        # Python venv (Pillow); every tool re-execs into it
├── vpinball/                    # Visual Pinball X         → VPINBALL_DIR
├── pinmame/                     # patched libpinmame       → PINMAME_DIR
├── vpinmame/                    # VPinMAME COM (Windows)   → VPINMAME_DIR
└── cache/                       # downloaded archives (trust-on-first-use SHA-256)
```

(`config.env` lives separately, at the stable app-data path — see step 4 above.)
