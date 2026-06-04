#!/usr/bin/env python3
"""
synthetic-record: author a *replayable* WPC switch-event session by hand.

A recorded session is just a `session.jsonl` of switch edges
(`{"t":<emu-sec>,"n":<sw#>,"on":0|1,"kind":"switch"}`) that `record`'s
`replay.py` injects via `PinmameSetSwitch`. There is nothing magic about it
having come from Visual Pinball — so we can *synthesize* one: feed the ROM a
plausible switch stream that drives it into a chosen state, with no VP and no
physics. See SKILL.md for the methodology (ball-search keepalive, the 3-layer
switch-identity workflow, pacing/warm-up gotchas).

This module is the Python builder. Typical use (from a scenario script):

    from synth import Session
    s = Session("congo_21", seed_from="sessions/<utc>")
    s.splice(until=14.5)                 # real launch preamble: ball into play
    s.at(18.0)
    s.alternate(["travi", "com"], rounds=3, gap=1.5)   # spell TRAVI-COM
    s.pulse("com", repeat=3, gap=2.0)                   # score the mode
    s.keepalive("bottom_jet", every=7.0, start=16.0, stop=s.end())
    s.write("sessions/<utc>-synth", labels=["satellite-transfer"], notes="...")

CLI:  python3 synth.py validate <session-dir>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from workbench_env import bootstrap_venv

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Session:
    """Accumulates switch edges and writes a replayable session directory."""

    def __init__(self, rom: str, seed_from: str | Path | None = None,
                 pulse_width: float = 0.04):
        self.rom = rom
        self.pulse_width = float(pulse_width)
        self.seed_from = str(seed_from) if seed_from else None
        self.edges: list[tuple[float, int, int]] = []   # (t, n, on)
        self.cursor: float = 0.0
        self.labels_by_num, self.name_to_num = self._load_switches(rom)

    # -- switch identity -----------------------------------------------------

    def _load_switches(self, rom: str):
        """Return (num->name for labels, name->num for resolution).

        Switch names are user-authored ROM data and live in the working
        directory by convention: ./names/<rom>.json, a JSON object mapping each
        switch number (as a decimal string) to a human-readable name. None ship
        with the plugin; see schemas/names.schema.json for the format. Absent
        file -> numbers only (names can't be resolved, which is fine for
        number-driven scenarios)."""
        labels: dict[int, str] = {}
        names: dict[str, int] = {}
        path = Path("names") / f"{rom}.json"
        if path.exists():
            j = json.loads(path.read_text(encoding="utf-8"))
            for num, name in j.items():
                labels[int(num)] = name
                names[str(name).lower()] = int(num)
        return labels, names

    def resolve(self, sw) -> int:
        """Resolve a switch given as int, numeric string, or alias name."""
        if isinstance(sw, bool):
            raise TypeError("switch must be an int or name, not bool")
        if isinstance(sw, int):
            return sw
        s = str(sw).strip()
        if s.isdigit():
            return int(s)
        n = self.name_to_num.get(s.lower())
        if n is None:
            raise KeyError(
                f"unknown switch {sw!r} for {self.rom}; "
                f"known names: {sorted(self.name_to_num)}"
            )
        return n

    # -- low-level emit ------------------------------------------------------

    def _emit(self, t: float, n: int, on: int):
        self.edges.append((round(float(t), 6), int(n), 1 if on else 0))

    # -- cursor helpers ------------------------------------------------------

    def at(self, t: float) -> "Session":
        """Move the authoring cursor to absolute time t (seconds)."""
        self.cursor = float(t)
        return self

    def wait(self, sec: float) -> "Session":
        """Advance the cursor by `sec` seconds."""
        self.cursor += float(sec)
        return self

    def end(self) -> float:
        """Largest event time emitted so far (0 if none)."""
        return max((t for t, _, _ in self.edges), default=0.0)

    # -- actions -------------------------------------------------------------

    def set(self, sw, on: bool, at: float | None = None) -> "Session":
        """Drive a level switch on/off (no auto-release)."""
        t = self.cursor if at is None else float(at)
        self._emit(t, self.resolve(sw), 1 if on else 0)
        if at is None:
            self.cursor = t
        return self

    def pulse(self, sw, at: float | None = None, width: float | None = None,
              repeat: int = 1, gap: float = 1.0) -> "Session":
        """Momentary closure(s): close then release after `width` seconds.

        `repeat`>1 emits a burst spaced `gap` apart. The cursor advances to the
        last release edge so subsequent un-`at`'d actions follow in sequence.
        """
        n = self.resolve(sw)
        w = self.pulse_width if width is None else float(width)
        base = self.cursor if at is None else float(at)
        last = base
        for i in range(repeat):
            t0 = base + i * gap
            self._emit(t0, n, 1)
            self._emit(t0 + w, n, 0)
            last = t0 + w
        if at is None:
            self.cursor = last
        return self

    def alternate(self, switches, rounds: int = 1, at: float | None = None,
                  width: float | None = None, gap: float = 1.0) -> "Session":
        """Pulse each switch in `switches` in turn, `rounds` times.

        e.g. alternate(["travi","com"], rounds=3) -> travi,com,travi,com,...
        (6 hits). Used for edge-triggered "light each member" target banks
        where re-hitting an already-lit member is a no-op, so members must be
        alternated rather than repeated.
        """
        w = self.pulse_width if width is None else float(width)
        base = self.cursor if at is None else float(at)
        seq = list(switches)
        i = 0
        last = base
        for _ in range(rounds):
            for sw in seq:
                t0 = base + i * gap
                n = self.resolve(sw)
                self._emit(t0, n, 1)
                self._emit(t0 + w, n, 0)
                last = t0 + w
                i += 1
        if at is None:
            self.cursor = last
        return self

    def keepalive(self, sw, every: float, start: float, stop: float,
                  width: float | None = None) -> "Session":
        """Sprinkle periodic pulses across [start, stop) to dodge ball-search.

        With no physics the playfield is silent between scripted actions; the
        ROM's ball-search will eventually decide the ball is lost. A periodic
        jet-bumper tap resets that timer. Does NOT move the cursor.
        """
        n = self.resolve(sw)
        w = self.pulse_width if width is None else float(width)
        t = float(start)
        while t < stop:
            self._emit(t, n, 1)
            self._emit(t + w, n, 0)
            t += float(every)
        return self

    # -- preamble splicing ---------------------------------------------------

    def splice(self, until: float, ref: str | Path | None = None,
               since: float = 0.0) -> "Session":
        """Copy real switch edges from a reference session (the launch preamble).

        Reads `<ref>/session.jsonl` (defaults to seed_from) and appends every
        switch edge with `since <= t <= until`, verbatim. This is the
        "start from an existing trace to get a ball into play" mechanism:
        splice the real start/trough/plunge sequence, then author from there.
        Sets the cursor to `until`.
        """
        ref_dir = Path(ref) if ref else (Path(self.seed_from) if self.seed_from else None)
        if ref_dir is None:
            raise ValueError("splice() needs a reference session (ref= or seed_from=)")
        path = ref_dir / "session.jsonl"
        n_added = 0
        with path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("kind") != "switch":
                    continue
                t = float(rec["t"])
                if since <= t <= until:
                    self._emit(t, int(rec["n"]), 1 if rec["on"] else 0)
                    n_added += 1
        self.cursor = float(until)
        self._spliced = (str(path), since, until, n_added)
        return self

    # -- output --------------------------------------------------------------

    def _sorted_edges(self):
        # Stable sort by (t, n, on) so on precedes off at equal t is NOT
        # forced; equal-t edges keep emission order via the on-flag tiebreak.
        return sorted(self.edges, key=lambda e: (e[0], e[1], e[2]))

    def write(self, out_dir: str | Path, labels=None, notes: str = "") -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        edges = self._sorted_edges()
        meta = {
            "v": 1,
            "kind": "meta",
            "rom": self.rom,
            "mode": "Synthetic",
            "synthetic": True,
            "seed_from": self.seed_from,
            "start_ts": _now_iso(),
            "comment": "Hand-authored by synthetic-record/synth.py",
        }
        sess = out / "session.jsonl"
        with sess.open("w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(meta) + "\n")
            for t, n, on in edges:
                f.write(json.dumps({"t": t, "n": n, "on": on, "kind": "switch"}) + "\n")
        meta_json = {
            "rom": self.rom,
            "mode": "Synthetic",
            "synthetic": True,
            "seed_from": self.seed_from,
            "labels": list(labels or []),
            "notes": notes,
            "n_edges": len(edges),
            "duration_sec": round(self.end(), 3),
            "start_ts": meta["start_ts"],
        }
        (out / "session.meta.json").write_text(
            json.dumps(meta_json, indent=2) + "\n", encoding="utf-8")
        return sess


# ---------------------------------------------------------------------------
# Validation CLI
# ---------------------------------------------------------------------------

def validate(session_dir: Path) -> int:
    """Sanity-check a session.jsonl: meta first, monotonic-ish, balanced pulses."""
    path = session_dir / "session.jsonl"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2
    edges = []
    meta = None
    problems = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if lineno == 1:
            if rec.get("kind") != "meta":
                problems.append("line 1 is not a meta record")
            meta = rec
            continue
        if rec.get("kind") == "switch":
            edges.append((float(rec["t"]), int(rec["n"]), 1 if rec["on"] else 0))
    # per-switch on/off balance (every closure eventually released)
    state: dict[int, int] = {}
    held = set()
    for t, n, on in sorted(edges, key=lambda e: (e[0], e[1], e[2])):
        state[n] = on
    held = {n for n, v in state.items() if v == 1}
    nums = sorted({n for _, n, _ in edges})
    print(f"session : {path}")
    print(f"meta    : rom={meta.get('rom')} mode={meta.get('mode')} "
          f"synthetic={meta.get('synthetic')}")
    print(f"edges   : {len(edges)}  switches={nums}")
    print(f"duration: {max((t for t,_,_ in edges), default=0):.3f}s")
    if held:
        print(f"held-at-end (level switches still closed): {sorted(held)}")
    if problems:
        for p in problems:
            print(f"PROBLEM : {p}")
        return 1
    print("OK")
    return 0


def main(argv=None):
    bootstrap_venv()  # re-exec under the toolkit venv if not already there
    ap = argparse.ArgumentParser(description="synthetic-record tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="sanity-check a synthetic session dir")
    v.add_argument("session_dir", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "validate":
        return validate(args.session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
