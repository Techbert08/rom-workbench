# ROM Workbench

A Claude Code plugin for reverse-engineering and modding Williams Pinball Controller (WPC) ROMs from the 1990s Williams/Bally era.

## Skills included

| Skill | Description |
|---|---|
| `wpc-investigate` | Static + byte-level ROM analysis — bank-aware 6809 disassembly, recursive-descent xref/function discovery, hex dump, byte/string search via `rom.py` |
| `wpc-debug` | Methodology playbook for driving the live CPU debugger: banked-PC resolution, breakpoints, watchpoints, single-step, the persistent frozen-CPU session |
| `build-wpc-rom` | Apply JSON patch specs to a WPC ROM zip, recompute the WPC checksum, produce a drop-in patched zip |
| `record-pinball` | Capture gameplay sessions in Visual Pinball + VPinMAME; replay headlessly with state/DMD/audio/debugger traces; event-driven CPU debugger with breakpoints and watchpoints |
| `synthetic-recording` | Author a replayable WPC session by hand — emit the switch-edge stream to drive the ROM into a chosen state without Visual Pinball |
| `pinball-setup` | One-time toolchain installer: Visual Pinball X, VPinMAME (Windows), patched libpinmame with the debug API |

## Install

```
/plugin marketplace add Techbert08/rom-workbench
/plugin install rom-workbench@rom-workbench
```

Skills are then available as `/rom-workbench:wpc-investigate`, `/rom-workbench:record-pinball`, etc.

## Requirements

- Claude Code (latest version)
- [uv](https://docs.astral.sh/uv/) (runs the replay/analysis scripts; `pinball-setup` installs it if missing). No system Python required — uv provisions the interpreter and per-script dependencies.
- Windows only: an Administrator PowerShell once during setup for the `regsvr32 VPinMAME.dll` step (everything else — setup, recording, building — is cross-platform Python run via uv)
- Windows: Visual Pinball X + VPinMAME for session recording (run `pinball-setup` first)
- macOS: `libpinmame.dylib` ships in `bin/` for headless replay (no VP needed)

## Game-specific configuration

Place a `names/<rom>.json` file in your project (working) directory for human-readable switch names in the synthetic recording skill — a JSON object mapping switch number (string) to name. None ship with the plugin; see `schemas/names.schema.json` for the format.

## Private repo access

This repo is private. For background auto-updates at Claude Code startup, set a GitHub token with `repo` scope:

```bash
export GITHUB_TOKEN=ghp_xxxx   # add to ~/.zshrc
```

Manual installs work without a token as long as you're authenticated via `gh auth login`.
