# ROM Workbench

A Claude Code plugin for reverse-engineering and modding the ROMs of 1990s
Williams/Bally pinball machines (the WPC platform).

## Overview

ROM Workbench lets you change how a real pinball machine behaves by editing its
game ROM — with Claude doing the reverse-engineering. You describe the change you
want in plain language ("award an extra ball the first time I spell CONGO",
"start the version display at 2.2", "make this mode re-triggerable"), and Claude
works with you to:

1. **Find** the relevant code in the factory ROM.
2. **Patch** the bytes and rebuild a checksum-correct ROM.
3. **Test** the change in an emulator — including a before/after look at the
   dot-matrix display — so you can confirm it's right before touching hardware.

When the modified ROM behaves the way you want in Visual Pinball, you can burn it
to a physical EPROM and install it in your machine.

The whole loop runs through Claude: you talk to it, it drives the tools. The
skills below are the specialized capabilities it reaches for at each step.

> **Tip:** this is deep, multi-step reverse-engineering. It goes best with the
> strongest model and room to run — use **Opus** and turn on **Auto mode** so
> Claude can work through long record → investigate → patch → test loops without
> stopping at every step.

## Installation

**From your terminal** (no Claude session needed):

```
claude plugin marketplace add Techbert08/rom-workbench
claude plugin install rom-workbench@rom-workbench
```

**From inside Claude Code**, the same thing as slash commands:

```
/plugin marketplace add Techbert08/rom-workbench
/plugin install rom-workbench@rom-workbench
```

**Claude desktop app:** open the **Customize** menu, add the marketplace
`Techbert08/rom-workbench`, then enable the **rom-workbench** plugin from there.

Once installed, the skills are available as `/rom-workbench:setup`,
`/rom-workbench:record`, `/rom-workbench:debug`, and so on — or just describe what
you want and Claude will pick the right one.

## Getting started

You need a machine with some Python 3.9+ available; everything else is
self-installing. The workflow below is a narrative — at each step you can either
run the named skill or just tell Claude what you want in plain language.

### 1. Set up your machine

Run `/rom-workbench:setup`, or just tell Claude "set up my machine for pinball
modding." This installs a **patched** Visual Pinball X + PinMAME emulator; the
patch is what lets the toolkit record your gameplay and replay it deterministically.
The patches applied are in the plugin if you're curious.

> **macOS note:** because setup swaps a patched library into the notarized
> Visual Pinball bundle, the first launch can warn that the app is "damaged."
> That's Gatekeeper reacting to the re-signed binary, not real damage — setup
> re-signs the bundle and walks you through approving it once in **System
> Settings → Privacy & Security** ("Open Anyway").

Do this from a **fresh project directory** for the mod you want to make — your
ROM, table, recordings, and notes all live there.

### 2. Get a ROM and a table

Ask Claude to help you find the factory ROM and a Visual Pinball 10 (VPX) table
for the game you want to modify. It places them in your working directory (the
ROM under `orig/`, the table under `tables/`).

### 3. Record some gameplay

Ask Claude to record some gameplay, or run `/rom-workbench:record`. It boots
Visual Pinball and hands you the table to play. Controls:

- **5** — insert a coin
- **1** — start a game
- **left / right Shift** — the flippers
- **enter** - ball plunger

Play through the behavior you care about, then close the window. Claude folds your
inputs into a replayable session.

### 4. Build a synthetic replay of the behavior

Now have Claude author a **synthetic replay** that exercises exactly the behavior
you want to change, or run `/rom-workbench:synthetic-record`. Describe the in-game
actions needed to trigger it, and tell Claude to use the table's scripts, the ROM,
and your real recording to work out the switch sequence. Ask it to **save notes**
as it discovers things — this can span several sessions.

### 5. Make the modification

Describe the actual change you want and point Claude at the replay that exercises
it. Claude reaches for `/rom-workbench:debug` to locate the code and patch the
ROM. When it's done you can:

- ask for a **before/after video** of the dot-matrix display to confirm the change,
- ask Claude to boot Visual Pinball with the modified ROM so you can try it
  yourself, or
- build the patched ROM zip directly with `/rom-workbench:build`.

### 6. Put it on real hardware

Once the modified ROM behaves correctly in the emulator, burn it to a physical
EPROM and install it in your machine.

## Skills

Claude loads these automatically; you can also invoke any of them directly.

| Skill | What it's for |
|---|---|
| `overview` | End-to-end orientation — the mod workflow and how the other skills fit together |
| `setup` | One-time toolchain installer (patched Visual Pinball X + PinMAME) |
| `record` | Capture gameplay in Visual Pinball and replay it headlessly with state / DMD / audio / debugger traces |
| `synthetic-record` | Author a replayable session by hand — drive the ROM into a chosen state without playing |
| `debug` | Reverse-engineer the ROM: bank-aware 6809 disassembly + cross-reference coupled to a live CPU debugger |
| `build` | Apply patch specs to a ROM zip, recompute the WPC checksum, and produce a drop-in patched zip |
