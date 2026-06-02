---
name: setup
description: One-time installer for the WPC mod toolchain — Visual Pinball X + PinMAME + VPinMAME + the patched libpinmame (for record). Use only on first machine setup or when reinstalling a single component. Not needed for day-to-day mod work.
---

# setup

One-time install. Once setup runs successfully, day-to-day work doesn't need this skill loaded — `debug` and `record` use the installed components directly.

## When to invoke

- "set up the pinball toolchain" / "install the WPC mod tools"
- "install Visual Pinball / PinMAME / VPinMAME"
- "redeploy the patched libpinmame after a rebuild"

For everyday "analyze a ROM" / "replay a session" / "set a breakpoint" requests, **skip this skill** — the install is already done on a configured machine.

## What gets installed and where

Everything lands under one per-user data root (see "Setup scripts" below):
`%LOCALAPPDATA%\rom-workbench\` on Windows, `~/Library/Application Support/rom-workbench/`
on macOS, `~/.local/share/rom-workbench/` on Linux.

### Windows

| Component | Where | Env var |
|---|---|---|
| Visual Pinball X 10.8.0 | `<root>\vpinball` | `VPINBALL_DIR` |
| Patched libpinmame.dll (prebuilt from `bin/`) | `<root>\pinmame` | `PINMAME_DIR` |
| VPinMAME COM (regsvr32-registered) | `<root>\vpinmame` | `VPINMAME_DIR` |
| uv (installed if missing) | `%USERPROFILE%\.local\bin` | — |
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
| uv (installed if missing) | `~/.local/bin` | — |

On macOS the patched `libpinmame.dylib` ships prebuilt in `bin/` for the common
case; `setup-pinball.py` only **builds from source** (from `--pinmame-src`,
default `../pinmame` beside the repo) when no arch-matched prebuilt is available.
Env vars are written to `~/.zshenv` and `~/.bash_profile`.

## Setup scripts

### `setup-pinball.py` — cross-platform toolchain installer

One stdlib-only Python script handles **both** macOS and Windows (replacing the
old `setup-pinball.sh` / `setup-pinball.ps1` pair). Run it with either Python or uv:

```bash
# Fresh machine (no uv yet): plain Python — it installs uv, then the tools.
python3 '${CLAUDE_PLUGIN_ROOT}/bin/setup-pinball.py' [--force] [--install-root <dir>] [--pinmame-src <path>]

# Once uv is available, the uv-native invocation works too:
uv run '${CLAUDE_PLUGIN_ROOT}/bin/setup-pinball.py'
```

Everything installs under a **per-user data directory** (no admin needed for the
files themselves), with `vpinball/`, `pinmame/` and — on Windows — `vpinmame/`
underneath, plus a `cache/`:

| OS | Install root |
|---|---|
| macOS | `~/Library/Application Support/rom-workbench/` |
| Windows | `%LOCALAPPDATA%\rom-workbench\` |
| Linux | `$XDG_DATA_HOME` (or `~/.local/share`)`/rom-workbench/` |

Override with `--install-root`. Idempotent; pass `--force` to re-download/rebuild.

Steps:
1. **Ensure uv** (install via https://astral.sh/uv if missing). The Python tools run via `uv run`, so uv — not a system Python — is the only ongoing Python prerequisite.
2. **Visual Pinball X** — download + install into `<root>/vpinball/` (skips gracefully if the pinned release has no asset for this OS/arch; replay doesn't need VPX).
3. **Patched libpinmame** — deploy the prebuilt patched library from `bin/` into `<root>/pinmame/` (replay loads it via ctypes; it's self-contained, so nothing is downloaded).
   - **macOS** — install `libpinmame.dylib` (prefer the arch-matched prebuilt in `bin/`; otherwise build from `--pinmame-src`, default `../pinmame` beside the repo). Also deploy it into the VPX bundle, ad-hoc re-sign, and run a Gatekeeper trial-launch.
   - **Windows** — copy `lib/libpinmame.dll` into `<root>/pinmame/`. Then download VPinMAME COM into `<root>/vpinmame/` (for `bass64.dll` + the layout), deploy the patched `VPinMAME64.dll`, and `regsvr32`-register it (needs an elevated shell once — the script detects this and prints the exact relaunch command rather than failing).
4. **Persist env vars** at user scope: `VPINBALL_DIR`, `PINMAME_DIR`, and (Windows) `VPINMAME_DIR`. On macOS/Linux these are written to `~/.zshenv` and `~/.bash_profile`; on Windows to the user environment (HKCU).

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
ROMs from there by gamename, so it only has to live there at record time. Drop a
game's files into `./orig/` and `./tables/` and run `record.py --rom <name>`;
nothing to register.

## Acquiring game files (ROM zip + VPX table)

Each game needs **two** files: a ROM zip and a VPX table. Neither is auto-downloaded.

| File | What it is | Where it comes from |
|---|---|---|
| **ROM zip** (e.g. `congo_21.zip`) | The 6809 game ROM dump VPinMAME runs. Same shape PinMAME has used for 20+ years. | VPForums "ROMs" section, sometimes bundled with VPX downloads. |
| **VPX table** (`.vpx`, sometimes inside a `.zip`) | The VP playfield — physics, art, lamps, VBScript that wires keys → switches and tells VPinMAME which ROM to load. Must be `.vpx` (Visual Pinball X), not `.vpt` (older VP9) or `.fpt` (FuturePinball). | VPUniverse or VPForums table sections — community-authored. |

### Workflow when a user asks for game files

1. **Confirm the game.** Williams/Bally WPC-era only (1990s).

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
uv run '${CLAUDE_PLUGIN_ROOT}/bin/setup-pinball.py'
```

If you'd rather skip the downloads/env checks entirely, the patched DLL just needs to land on top of the installed one: copy it directly over `%LOCALAPPDATA%\rom-workbench\pinmame\libpinmame*.dll`.

Forgetting the deploy step makes `replay.py` fall back to the un-patched DLL. The giveaway error is `PinmameDebugAttach not found`.

## Prerequisites

To launch `setup-pinball.py` the first time you need **either** uv **or** any
Python 3.9+ (`python3 setup-pinball.py` will then install uv for you). After that:

### Windows
- **One Administrator PowerShell** for the one-time `regsvr32 VPinMAME.dll` step.

### macOS
- **cmake 3.25+**, **Xcode Command Line Tools** (`xcode-select --install`), **git** — only needed for the rare libpinmame *source-build* fallback (the prebuilt `lib/libpinmame.dylib` covers the common case). `setup-pinball.py` checks these only when it actually has to build.

`uv` is installed by `setup-pinball.py` if missing and then runs every Python tool — no system Python is needed for day-to-day work.

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── skills/setup/SKILL.md   # this file
├── bin/
│   └── setup-pinball.py    # cross-platform VP + libpinmame (+ VPinMAME on Windows) installer
└── lib/                    # prebuilt patched libraries this script deploys
    ├── libpinmame.dylib    # macOS
    ├── libpinmame.dll      # Windows
    └── VPinMAME64.dll      # Windows (VPinMAME COM recorder)
```
