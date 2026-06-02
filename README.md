# ROM Workbench

A Claude Code plugin for reverse-engineering and modding Williams Pinball Controller (WPC) ROMs from the 1990s Williams/Bally era.

## Skills included

| Skill | Description |
|---|---|
| `build` | Apply JSON patch specs to a WPC ROM zip, recompute the WPC checksum, produce a drop-in patched zip |
| `record` | Capture gameplay sessions in Visual Pinball + VPinMAME; replay headlessly with state/DMD/audio/debugger traces; event-driven CPU debugger with breakpoints and watchpoints |
| `synthetic-record` | Author a replayable WPC session by hand — emit the switch-edge stream to drive the ROM into a chosen state without Visual Pinball |
| `debug` | Reverse-engineer ROMs end to end: bank-aware 6809 disassembly + recursive-descent xref/function discovery (`rom.py`) coupled to the live CPU debugger — banked-PC resolution, breakpoints, watchpoints, single-step, the persistent frozen-CPU session |
| `setup` | One-time toolchain installer: Visual Pinball X, PinMAME, VPinMAME, patched libpinmame with the debug API |

## Install

```
/plugin marketplace add Techbert08/rom-workbench
/plugin install rom-workbench@rom-workbench
```

Skills are then available as `/rom-workbench:debug`, `/rom-workbench:record`, etc.

## Requirements

- Claude Code (latest version)
- [uv](https://docs.astral.sh/uv/) (runs the replay/analysis scripts; the `setup` skill installs it if missing). No system Python required — uv provisions the interpreter and per-script dependencies.
- PowerShell 7+ (for the Windows recording/registration scripts; setup itself is a cross-platform Python script)
- Windows: Visual Pinball X + PinMAME for session recording (run the `setup` skill first)
- macOS: `libpinmame.dylib` ships in `lib/` for headless replay (no VP needed)

## Game-specific configuration

Place a `names/<rom>.json` file in your project directory for human-readable switch names in the synthetic recording skill. See `names/README.md` for the format.

## Private repo access

This repo is private. For background auto-updates at Claude Code startup, set a GitHub token with `repo` scope:

```bash
export GITHUB_TOKEN=ghp_xxxx   # add to ~/.zshrc
```

Manual installs work without a token as long as you're authenticated via `gh auth login`.
