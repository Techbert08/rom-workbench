#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Diff two replay output directories for the same session against different ROMs.

Each trace kind has its own diff strategy:
  - state: sort by (kind, n, t-bucketed); compare value sequences per (kind, n)
  - cputrace: align by switch-event index (read from session.jsonl), then
              line-window difflib on the cpu records
  - memwatch: per-(addr) compare of (pc, value) sequences
  - dmd: compare sha256 indexes; flag mismatched frames

Writes <out>/diff.html (and a machine-readable diff.json) summarising the
divergences.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
from collections import defaultdict
from pathlib import Path


def _read_jsonl(p: Path) -> list[dict]:
    out: list[dict] = []
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def diff_state(a_path: Path, b_path: Path) -> dict:
    a = _read_jsonl(a_path)
    b = _read_jsonl(b_path)

    def index(recs: list[dict]) -> dict[tuple[str, int], list[tuple[float, int]]]:
        idx: dict[tuple[str, int], list[tuple[float, int]]] = defaultdict(list)
        for r in recs:
            if r.get("kind") in ("lamp", "sol", "gi", "switch"):
                idx[(r["kind"], int(r["n"]))].append((float(r["t"]), int(r["v"])))
        return idx

    ia, ib = index(a), index(b)
    keys = set(ia) | set(ib)
    divergent: list[dict] = []
    for k in sorted(keys):
        seqa = [v for _, v in ia.get(k, [])]
        seqb = [v for _, v in ib.get(k, [])]
        if seqa != seqb:
            divergent.append(
                {
                    "kind": k[0],
                    "n": k[1],
                    "a_count": len(seqa),
                    "b_count": len(seqb),
                    "first_diff_index": next(
                        (i for i in range(min(len(seqa), len(seqb))) if seqa[i] != seqb[i]),
                        min(len(seqa), len(seqb)),
                    ),
                }
            )
    return {
        "kind": "state",
        "a_records": len(a),
        "b_records": len(b),
        "divergent_channels": divergent,
    }


def diff_cpu(a_path: Path, b_path: Path, max_diff_lines: int = 2000) -> dict:
    a = _read_jsonl(a_path)
    b = _read_jsonl(b_path)
    a_lines = [f"{r.get('pc','')} {r.get('disasm','') or ''}" for r in a if r.get("kind") == "cpu"]
    b_lines = [f"{r.get('pc','')} {r.get('disasm','') or ''}" for r in b if r.get("kind") == "cpu"]
    diff = list(difflib.unified_diff(a_lines, b_lines, lineterm="", n=2))
    truncated = False
    if len(diff) > max_diff_lines:
        diff = diff[:max_diff_lines]
        truncated = True
    return {
        "kind": "cputrace",
        "a_lines": len(a_lines),
        "b_lines": len(b_lines),
        "diff": diff,
        "truncated": truncated,
    }


def diff_mem(a_path: Path, b_path: Path) -> dict:
    a = _read_jsonl(a_path)
    b = _read_jsonl(b_path)

    def by_addr(recs: list[dict]) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in recs:
            if r.get("kind") == "memwrite":
                out[r["addr"]].append((r["pc"], r["value"]))
        return out

    ia, ib = by_addr(a), by_addr(b)
    keys = set(ia) | set(ib)
    divergent = []
    for k in sorted(keys):
        if ia.get(k, []) != ib.get(k, []):
            divergent.append({"addr": k, "a_count": len(ia.get(k, [])), "b_count": len(ib.get(k, []))})
    return {"kind": "memwatch", "divergent_addrs": divergent}


def diff_dmd(a_dir: Path, b_dir: Path) -> dict:
    a = {r["frame"]: r["sha256"] for r in _read_jsonl(a_dir / "dmd.index.jsonl") if r.get("kind") == "dmd"}
    b = {r["frame"]: r["sha256"] for r in _read_jsonl(b_dir / "dmd.index.jsonl") if r.get("kind") == "dmd"}
    keys = sorted(set(a) | set(b))
    mismatches = [f for f in keys if a.get(f) != b.get(f)]
    return {"kind": "dmd", "a_frames": len(a), "b_frames": len(b), "mismatched_frames": mismatches[:200], "total_mismatches": len(mismatches)}


def render_html(report: dict) -> str:
    parts = ["<!doctype html><meta charset=utf-8><title>record-pinball diff</title>"]
    parts.append("<style>body{font:14px/1.4 system-ui;margin:2em}h2{margin-top:2em}pre{background:#f6f6f6;padding:8px;overflow:auto;max-height:60vh}.ok{color:#0a0}.bad{color:#a00}</style>")
    parts.append("<h1>record-pinball diff</h1>")
    parts.append(f"<p>A: <code>{html.escape(str(report['a']))}</code></p>")
    parts.append(f"<p>B: <code>{html.escape(str(report['b']))}</code></p>")
    for section, data in report["sections"].items():
        parts.append(f"<h2>{section}</h2><pre>{html.escape(json.dumps(data, indent=2))}</pre>")
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True, help="replay output dir (factory)")
    ap.add_argument("--b", type=Path, required=True, help="replay output dir (modded)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    sections: dict[str, dict] = {}
    if (args.a / "trace.state.jsonl").exists() and (args.b / "trace.state.jsonl").exists():
        sections["State"] = diff_state(args.a / "trace.state.jsonl", args.b / "trace.state.jsonl")
    if (args.a / "trace.cpu.jsonl").exists() and (args.b / "trace.cpu.jsonl").exists():
        sections["CpuTrace"] = diff_cpu(args.a / "trace.cpu.jsonl", args.b / "trace.cpu.jsonl")
    if (args.a / "trace.mem.jsonl").exists() and (args.b / "trace.mem.jsonl").exists():
        sections["MemWatch"] = diff_mem(args.a / "trace.mem.jsonl", args.b / "trace.mem.jsonl")
    if (args.a / "dmd.index.jsonl").exists() and (args.b / "dmd.index.jsonl").exists():
        sections["DMD"] = diff_dmd(args.a, args.b)

    report = {"a": str(args.a), "b": str(args.b), "sections": sections}
    (args.out / "diff.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.out / "diff.html").write_text(render_html(report), encoding="utf-8")
    print(f"[diff_traces] wrote {args.out/'diff.json'} and diff.html")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
