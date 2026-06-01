---
name: pinball-setup
description: One-time installer for the WPC mod toolchain — Ghidra + WPC loader (for wpc-investigate), Visual Pinball X + PinMAME + VPinMAME + the patched libpinmame (for record-pinball), and per-game ROM/table registration via add-rom. Use only on first machine setup or when reinstalling a single component. Not needed for day-to-day mod work.
---

# pinball-setup

One-time install. Once setup runs successfully, day-to-day work doesn't need this skill loaded — `wpc-investigate` and `record-pinball` use the installed components directly.

## When to invoke

- "set up the pinball toolchain" / "install the WPC mod tools"
- "install Ghidra for WPC analysis"
- "install Visual Pinball / PinMAME / VPinMAME"
- "register Congo as a ROM" / "add a new game to the recorder"
- "redeploy the patched libpinmame after a rebuild"

For everyday "analyze a ROM" / "replay a session" / "set a breakpoint" requests, **skip this skill** — the install is already done on a configured machine.

## What gets installed and where

### Windows

| Component | Where | Env var |
|---|---|---|
| Ghidra 12.0.4 + c0rner WPC loader | `%LOCALAPPDATA%\Programs\ghidra_12.0.4_PUBLIC` | `GHIDRA_INSTALL_DIR` |
| Visual Pinball X 10.8.0 | `%LOCALAPPDATA%\Programs\vpinball` | `VPINBALL_DIR` |
| PinMAME standalone + libpinmame | `%LOCALAPPDATA%\Programs\pinmame` | `PINMAME_DIR` |
| VPinMAME COM (regsvr32-registered) | `%LOCALAPPDATA%\Programs\vpinmame` | `VPINMAME_DIR` |
| Picked Python interpreter | (already on PATH) | `PYTHON_FOR_RP` |
| Patched VPinMAME64.dll | deployed over the installed VPinMAME | — |
| Patched libpinmame (debugger API) | deployed over the installed PinMAME | — |

### macOS

| Component | Where | Env var |
|---|---|---|
| Visual Pinball X (macOS GL build) | `~/Library/Application Support/VPinball` | `VPINBALL_DIR` |
| Patched libpinmame.dylib | `~/Library/Application Support/PinMAME` | `PINMAME_DIR` |
| Picked Python interpreter | (already on PATH) | `PYTHON_FOR_RP` |

`libpinmame.dylib` is **built from source** by `setup-pinball.sh` (no macOS
prebuilt is distributed by upstream PinMAME). The patched source lives in
`../pinmame` (relative to the project root), which `setup-pinball.sh` auto-detects.
Env vars are written to `~/.zshenv` and `~/.bash_profile`.

## Setup scripts

### `setup-pinball.sh` — macOS toolchain installer

```bash
bash '${CLAUDE_PLUGIN_ROOT}/setup-pinball.sh' [--force] [--pinmame-src <path>]
```

Idempotent. Steps:
1. Verify Python 3.10+ and build prerequisites (cmake, git, clang).
2. Download Visual Pinball X macOS build (skips gracefully if no macOS asset exists for the pinned release).
3. Build patched `libpinmame.dylib` from `--pinmame-src` (default: `../pinmame` relative to the project root). Applies the three patches in `record-pinball/pinmame-patches/` if the branch doesn't exist yet.
4. Install `libpinmame.dylib` to `PINMAME_DIR` and (if found) into the VPX app bundle.
5. Write `PINMAME_DIR`, `VPINBALL_DIR`, `PYTHON_FOR_RP` to `~/.zshenv` and `~/.bash_profile`.

Does **not** install Ghidra. Run `setup-ghidra.ps1` separately on macOS via `pwsh` if you need it.

### `add-rom.sh` — macOS: register a game

```bash
bash '${CLAUDE_PLUGIN_ROOT}/add-rom.sh' \
    --rom-zip '<path-to-rom-zip>' \   # required
    [--rom <name>] \                  # default: basename of the zip
    [--table '<path-to-vpx>'] \       # optional
    [--skip-table] \                  # replay.py works fine without a table
    [--force]
```

Copies the ROM zip to `$PINMAME_DIR/roms/<rom>.zip` and records the VPX table path in `./config.json`.

---

### `setup-ghidra.ps1` — static analysis tools

```powershell
& '${CLAUDE_PLUGIN_ROOT}/setup-ghidra.ps1' [-Force]
```

Idempotent. Steps:
1. Verify Java 21+ and git are on PATH.
2. Download Ghidra 12.0.4 (~400 MB), SHA-256 verify, extract.
3. Set `GHIDRA_INSTALL_DIR` (user scope).
4. Clone or update `https://github.com/c0rner/ghidra_wpc_loader`.
5. Build the extension via Ghidra's bundled gradle wrapper (first build downloads gradle 9.3.1).
6. Drop the built extension into `<ghidra>\Ghidra\Extensions\ghidra_wpc_loader\` so `analyzeHeadless` auto-loads it.

If any step fails partway, `-Force` and re-run gives you a clean install.

### `setup-pinball.ps1` — dynamic-analysis / replay tools

```powershell
& '${CLAUDE_PLUGIN_ROOT}/setup-pinball.ps1' [-Force]
```

**Needs an Administrator PowerShell once** for `regsvr32`. If not elevated, the script detects this and prints the exact relaunch command rather than failing silently.

Idempotent. Steps:
1. Verify PowerShell 7+ and pick a Python interpreter; record as `PYTHON_FOR_RP`.
2. Download Visual Pinball X 10.8.0.
3. Download PinMAME 3.6 standalone + libpinmame.
4. Download VPinMAME 3.6 COM, `regsvr32` it.
5. Deploy the patched `VPinMAME64.dll` and `pinmame64.dll` from `<record-pinball>\bin\` over the installed DLLs. Backs up the originals as `*.orig` first.

Downloads are SHA-256 verified against a sidecar file recorded on first download. To switch to upstream-pinned hashes, paste the recorded hash into the `Expected*Sha256` constants near the top of the script.

### `add-rom.ps1` — register a game

```powershell
& '${CLAUDE_PLUGIN_ROOT}/add-rom.ps1' `
    -RomZip '<path-to-rom-zip>' `    # required
    [-Rom <name>] `                  # default: basename of the zip
    [-Table '<path-to-vpx>'] `       # optional; otherwise prompted
    [-SkipTable] `                   # use record-pinball's InpOnly mode only
    [-Force]                         # overwrite staged zip / registered table
```

Per-game registration:
1. Copies the ROM zip to `%VPINMAME_DIR%\roms\<rom>.zip`.
2. If `-Table` is supplied, copies the `.vpx` into `%VPINBALL_DIR%\Tables\` and records the path in `%LOCALAPPDATA%\record-pinball\config.json` under `tables.<rom>`.
3. Otherwise prompts: open VPUniverse search / paste a path / skip.

After this, `record.py -Rom <name>` picks up the table automatically.

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

5. **Claude extracts the `.vpx` from its `.zip`** if needed (`Expand-Archive`) and runs `add-rom.ps1 -RomZip <…> -Table <…>`.

### Legality and authorship

- **PinMAME ROMs**: ROM dumps. Community convention is that personal use for games you own is uncontroversial; the sites have been distributing them for ~20 years; legal status is the same as any other ROM dump (gray). The skill takes no position.
- **VPX tables**: 100% community-authored, freely redistributed by their authors. The VPW team has been the modern gold standard for Williams/Bally table mods.

## Redeploying the patched DLLs after a rebuild

The patched DLLs ship prebuilt in `record-pinball/bin/` and are sufficient for
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

# Copy into the skill bin/ and re-run setup-pinball.ps1 — its deploy step
# (re)installs the DLL into PINMAME_DIR.
Copy-Item "$PinmameSrc\build\libpinmame\Release\pinmame64.dll" `
          <repo>\.claude\skills\record-pinball\bin\pinmame64.dll -Force
& '${CLAUDE_PLUGIN_ROOT}/setup-pinball.ps1'
```

The deploy step alone (skipping the downloads) is the bottom ~30 lines of `setup-pinball.ps1`; copy directly to `%LOCALAPPDATA%\Programs\pinmame\libpinmame*.dll` if you want to skip the env-var checks.

Forgetting the deploy step makes `replay.py` fall back to the un-patched DLL. The giveaway error is `PinmameDebugAttach not found`.

## Prerequisites

### Windows
- **PowerShell 7+** (`pwsh`).
- **Python 3.10+** on PATH.
- **JDK 21+** on PATH (Eclipse Temurin recommended, for Ghidra only).
- **git** on PATH (for cloning the WPC loader, for Ghidra only).
- **One Administrator PowerShell** for `regsvr32 VPinMAME.dll`.

### macOS
- **Python 3.10+** on PATH (or via Homebrew: `brew install python3`).
- **cmake 3.25+** (`brew install cmake`).
- **Xcode Command Line Tools** (`xcode-select --install`).
- **git** (bundled with Xcode CLT).

Each script verifies its own dependencies and exits with a clear message if anything is missing.

## File layout

```
${CLAUDE_PLUGIN_ROOT}/
├── SKILL.md                # this file
├── setup-ghidra.ps1        # Ghidra + WPC loader installer (for wpc-investigate; Windows)
├── setup-pinball.ps1       # VP + PinMAME + VPinMAME installer (for record-pinball; Windows)
├── setup-pinball.sh        # libpinmame.dylib + VPX installer (for record-pinball; macOS)
├── add-rom.ps1             # per-game ROM + table registration (Windows)
└── add-rom.sh              # per-game ROM + table registration (macOS)
```
