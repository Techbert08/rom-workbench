#!/usr/bin/env python3
"""Thin client for the persistent interactive debugger session.

The session is a long-lived `replay.py --interactive` process that boots a ROM
to a breakpoint and then holds the m6809 CPU *frozen* while it serves commands
over a localhost TCP socket. Each `dbg.py` invocation opens one connection,
sends one command line, prints the reply, and exits — but the emulator stays
booted and paused between calls, so successive probes share one boot and each
is informed by the last (no re-running from POST).

Usage:
    python3 dbg.py [--port 47655] <command ...>

Commands (also: `dbg.py help`):
    regs                      registers + resolved $PC@pBANK location
    mem <addr> [len]          hex/ASCII dump (len default 16)
    dis [addr] [n]            disassemble n instrs (default: live PC, n=8)
    step [n]                  single-step n instructions, list each landing
    continue [until <pc>]     resume until the next break (optionally a temp bp)
    bp add|del <pc> | bp list
    wp add r|w <addr> | wp del <addr> | wp list
    bank                      the ROM page currently mapped at $4000-$7FFF
    quit                      stop the emulator and end the session

Address forms accepted anywhere an <addr>/<pc> is expected:
    0xNNNN   $NNNN   NNNN(hex)   @X @S+2 @U-1   (register-relative)

Examples:
    python3 dbg.py regs
    python3 dbg.py dis @pc 12
    python3 dbg.py mem @x 8
    python3 dbg.py continue until 0x4067
    python3 dbg.py step 5
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from workbench_env import bootstrap_venv


def find_port(explicit: int | None) -> int:
    if explicit:
        return explicit
    # Fall back to a dbg.session.json dropped by the host under any recent
    # replay out-dir, so callers don't have to remember the port.
    for p in sorted(Path(".").glob("**/dbg.session.json"),
                    key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            return int(json.loads(p.read_text())["port"])
        except Exception:
            continue
    return 47655


def main() -> int:
    bootstrap_venv()  # re-exec under the toolkit venv if not already there
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if not args.rest:
        print(__doc__)
        return 0

    port = find_port(args.port)
    cmd = " ".join(args.rest)
    try:
        with socket.create_connection((args.host, port), timeout=10.0) as s:
            s.sendall((cmd + "\n").encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            sys.stdout.write(buf.decode("utf-8", "replace"))
            if not buf.endswith(b"\n"):
                sys.stdout.write("\n")
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        print(f"dbg: cannot reach session on {args.host}:{port} ({e}). "
              f"Is `replay.py --interactive` running?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
