---
name: synthetic-record
description: Author a replayable Williams (WPC) pinball session by hand — no Visual Pinball, no physics — by emitting the switch-edge stream that drives the ROM into a chosen state, then replay it headlessly via record. Use to synthesize a deterministic test case (start a ball, spell a target bank, trigger and re-trigger a mode, force an edge case) when capturing it live would be tedious or non-reproducible.
---

# synthetic-record

> **Orientation:** if you haven't already, load `rom-workbench:overview` for the
> end-to-end mod workflow (setup → record → synthesize → debug → build → test)
> and where this step fits.

A recorded session is nothing but a `session.jsonl` of switch edges
(`{"t":<emu-sec>,"n":<sw#>,"on":0|1,"kind":"switch"}`) that `record`'s
`replay.py` injects into libpinmame with `PinmameSetSwitch`. Visual Pinball is
just one way to *produce* that stream. This skill **synthesizes** it: you feed
the ROM a plausible switch stream by hand and it drives the game into whatever
state you want — deterministically, with no VP and no ball physics.

That last part is the whole mental model: **there is no physics at replay
time.** Switches only change when you author an edge. The ROM still runs its
real logic — it fires solenoids, runs timers, expects the ball to move — but
nothing moves unless you say so. So synthesizing a session is the art of
feeding the ROM *just enough* believable switch activity to keep it in the
state you want.

## When to invoke

- "synthesize a session that triggers <mode>" / "make a fake recording"
- "drive Satellite Transfer twice in one ball to test the counter"
- "I don't want to re-record by hand — can you fabricate the inputs?"
- any deterministic test case where a live capture would be painful or flaky

Output is a normal session directory, so everything downstream
(`replay.py`, `render_dmd_video.py`, `diff_traces.py`, the `Dbg` trace) works
unchanged. For *capturing* a real session use `record`; for driving the
live debugger use `debug`.

## The two hard parts

### 1. Getting a ball into play (splice a real preamble)

Starting a ball is a solenoid/switch dance: press start → the ROM pulses the
trough solenoid → a ball reaches the shooter lane (shooter switch closes) →
the auto-plunger fires → the ball enters play (shooter switch opens). With no
physics you'd have to author every one of those edges by hand and get the
timing right.

Don't. **Splice the launch preamble from a real reference session.**
`Session.splice(until=T)` copies every real switch edge with `t <= T` verbatim
— including the coin-up that gives you credits, the trough state, and the
start/plunge sequence — then you author from `T` onward. Pick `T` just after the
shooter-lane switch clears (ball in play) and before any mode-relevant hits.
For Congo's reference recording that's `until=14.5`.

The splice also seeds the correct **initial switch state** (coin door closed,
balls in the trough) because those live at `t=0` in the reference session.

### 2. Keeping the ball alive (dodge ball-search)

After ~10–20 s with no playfield switch activity the ROM concludes the ball is
stuck/lost and runs **ball search** (bangs solenoids) and eventually drains the
ball. Your scripted hits keep it happy *while you're hitting things*, but any
idle gap — e.g. waiting out a 30 s mode timer — will trip it.

`Session.keepalive(sw, every, start, stop)` sprinkles a periodic tap of a
harmless playfield switch (a jet bumper) across a window to reset that timer.
Lay it down across the whole in-play span. Tune `every` below the ball-search
timeout (7 s works for Congo; the real recording never idled longer than that).

## Switch identity — a 3-layer workflow

You need the right switch *numbers*. They come from three sources, in order of
authority for the switch you care about:

1. **The PinMAME driver source** — ROM ground truth for the switches the driver
   models (start, trough, slings, jets, lanes, coin door): the game's
   `src/wpc/*.c` in the PinMAME tree (for Congo, `prelim/congo.c`). **It does not
   include most playfield targets** — the prelim sim doesn't model them.
2. **The table VBScript** (`orig/<table>.vbs`) — maps physical playfield objects
   to the switch numbers the ROM reads (`Controller.Switch(n)` /
   `vpmTimer.PulseSw n`), so it covers the targets the driver omits, but its
   human labels are sparse. Extract it from the table once:

   ```bash
   # macOS / Linux
   VPinballX_GL --extractvbs orig/<table>.vpx      # writes orig/<table>.vbs
   # Windows
   VPinballX.exe -ExtractVBS orig\<table>.vpx
   ```

   Then grep the `.vbs` for `Controller.Switch(` / `PulseSw` to read the wiring.
3. **Empirical, from a real recording** — the definitive answer to "which
   switch *does* X". Replay a session that exercised the feature with a
   `--watch-w` on the RAM the feature touches, and read the switch edge that
   immediately precedes each effect. This is how the Congo TRAVI-COM/satellite
   targets were pinned (replay with `--watch-w 0x068F`, the satellite counter;
   the two scoring hits were preceded by sw52 and sw51).

Record the numbers you pin as names in `./names/<rom>.json` in your working
directory (a `{"<num>": "<name>"}` map; see `schemas/names.schema.json`), so
future scenarios read `pulse("travi")` not `pulse(51)`.

## The builder API (`synth.py`)

```python
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/bin")   # so `synth` is importable
from synth import Session
s = Session("congo_21", seed_from="sessions/<utc-reference>")

s.splice(until=14.5)               # real launch preamble -> ball in play, cursor=14.5
s.at(18.0)                          # absolute cursor
s.pulse("com")                      # momentary closure (close + release after ~40ms)
s.pulse("com", repeat=3, gap=2.0)   # a burst, 2s apart; cursor advances past it
s.alternate(["travi","com"], rounds=3, gap=1.5)   # travi,com,travi,com,... (6 hits)
s.set("shooter", True, at=20.0)     # level switch on (no auto-release)
s.wait(30)                          # advance cursor (idle)
s.keepalive("bottom_jet", every=7.0, start=16.0, stop=s.end()+1)
s.write("sessions/<out>", labels=[...], notes="...")
```

- Names resolve via `./names/<rom>.json` in the working directory; a raw int or
  numeric string always works; an unknown name is a hard error (no silent
  misfire).
- Times are **emulation seconds**. Inputs only take after boot warm-up
  (the machine reaches state==1, ~12 s in); the spliced `start` already lands
  after that, so author later beats relative to it.
- `alternate(...)` exists because many target banks are **edge-triggered**:
  re-hitting an already-lit member is a no-op, so members must be alternated,
  not repeated, to advance.
- `pulse`/`alternate` without `at=` chain off the cursor; with `at=` they place
  an absolute burst and leave the cursor alone.

Validate before replaying:

```powershell
python3 ${CLAUDE_PLUGIN_ROOT}/bin/synth.py validate sessions\<out>
```

## Replay & verify

A synthetic session is a normal session — replay it with `record`:

```powershell
python3 ${CLAUDE_PLUGIN_ROOT}/bin/replay.py --rom <rom> --rom-zip <zip> `
    --session sessions\<out> --nvram <nv> --trace state,dmd,dbg `
    --watch-w 0x<addr> --out-dir sessions\<out>\replays\<stem>\run1 --overwrite

# Eyeball it: render the DMD to a real-time mp4 with burned-in timecode.
python3 ${CLAUDE_PLUGIN_ROOT}/bin/render_dmd_video.py sessions\<out>\replays\<stem>\run1
```

Confirm the scenario actually played: watch the RAM that proves the state you
were aiming for (a mode flag, a counter, the score), and/or scrub the DMD video.
If it didn't trigger, iterate — the loop is cheap: edit the scenario script,
regenerate, replay. Common fixes: more/again-alternated target hits, a longer
idle for a timer, tighter keepalive, a different splice cutoff.

## Files

```
${CLAUDE_PLUGIN_ROOT}/
├── skills/synthetic-record/SKILL.md  # this file
└── bin/
    └── synth.py                      # builder library + `validate` CLI
```

Switch names are per-game **working-directory** data, not part of the plugin:
author `./names/<rom>.json` in your project dir as you pin switches (a
`{"<num>": "<name>"}` map; see `schemas/names.schema.json` for the format).

## Gotchas

- **No physics.** A ball never drains, advances, or feeds unless you author the
  edges (or splice them). Modes that need a specific ball path (lock, ramp,
  saucer) require authoring those switch closures explicitly.
- **Credits.** `start` does nothing without credits. The spliced preamble
  includes the reference session's coin-up; if you don't splice, add coin
  pulses (`pulse("coin_door"...)` is the interlock, not a coin — use the coin
  switch for your game) or free play.
- **Edge-triggered banks** need `alternate`, not `pulse(repeat=…)`.
- **Idle gaps** > ball-search timeout need `keepalive`.
- **Pacing/warm-up** is the same closed-loop model as `record`; see its
  SKILL.md "Determinism / pacing model". Inputs before state==1 are dropped.
```
