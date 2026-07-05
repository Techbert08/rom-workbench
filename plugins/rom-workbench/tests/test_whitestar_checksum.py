#!/usr/bin/env python3
"""Tests for build.py's Whitestar checksum fixup.

These verify the property the boot self-test ($9F62) and a hardware 16-bit
authenticator actually enforce:
  * 8-bit boot test:  (sum of all bytes) & 0xFF == 0xFF
  * 16-bit invariant: (sum of all bytes) & 0xFFFF == stored word at $FFEE

The factory LOTR CPU ROM satisfies both (8-bit 0xFF, 16-bit 0x84FF == $FFEE).
After whitestar_update_checksum() the 16-bit sum must again equal the stored
$FFEE word, with $FFEE left byte-identical to factory.

Run standalone (no pytest needed):  python3 test_whitestar_checksum.py
Optionally point at a real ROM:      LOTR_CPU=/path/lotrcpua.a00 python3 ...
It also runs under pytest if available.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"


def _load_build():
    """Import build.py with workbench_env stubbed (avoids the venv bootstrap)."""
    stub = types.ModuleType("workbench_env")

    class _CException(SystemExit):
        pass

    def die(msg):  # mirrors workbench_env.die: aborts the build
        raise _CException(f"DIE: {msg}")

    for fn in ("ok", "step", "warn"):
        setattr(stub, fn, lambda *a, **k: None)
    stub.die = die
    stub.load_config = lambda *a, **k: {}
    stub.load_game_manifest = lambda *a, **k: {}
    stub.bootstrap_venv = lambda *a, **k: None
    stub._C = stub._c = types.SimpleNamespace()
    sys.modules["workbench_env"] = stub

    sys.path.insert(0, str(BIN))
    spec = importlib.util.spec_from_file_location("build", BIN / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, _CException


build, BuildDie = _load_build()

SYS_SIZE = 0x8000


def _synth_rom(size=0x20000, target_word=0x84FF):
    """A 128K synthetic ROM that satisfies both invariants, like the factory
    image: a pseudo-random body, a generous blank (0xFF) free-space run below
    $FFEC, and a stored $FFEE word (default 0x84FF, low byte 0xFF) that equals
    the true 16-bit sum.

    The word<->sum relationship is self-referential and not solvable from a
    single spare byte, so we establish it the same way the toolkit does: set the
    stored word, then let whitestar_update_checksum() reduce the free-space sum
    to match it. That makes this fixture a valid starting point; the behavioural
    tests then perturb it and re-run the fixup."""
    assert target_word & 0xFF == 0xFF and target_word != 0xFFFF
    rom = bytearray((i * 37 + 11) & 0xFF for i in range(size))
    base = size - SYS_SIZE
    free_start = base + (0xF800 - 0x8000)
    free_end = base + (0xFFEC - 0x8000)
    for o in range(free_start, free_end):
        rom[o] = 0xFF
    word_off = base + (0xFFEE - 0x8000)
    rom[word_off] = (target_word >> 8) & 0xFF
    rom[word_off + 1] = target_word & 0xFF
    build.whitestar_update_checksum(rom)  # reduce free space so 16-bit sum == word
    return rom


def _stored_word(rom):
    base = len(rom) - SYS_SIZE
    o = base + (0xFFEE - 0x8000)
    return (rom[o] << 8) | rom[o + 1]


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _check_invariants(rom, expect_word):
    _assert(sum(rom) & 0xFF == 0xFF, f"8-bit sum {sum(rom)&0xFF:#04x} != 0xFF")
    _assert(sum(rom) & 0xFFFF == expect_word,
            f"16-bit sum {sum(rom)&0xFFFF:#06x} != stored word {expect_word:#06x}")
    _assert(_stored_word(rom) == expect_word, "$FFEE word changed")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_factory_rom_satisfies_both_invariants():
    """Sanity: the real (or synthetic) factory ROM already passes both checks."""
    rom = _factory_rom()
    word = _stored_word(rom)
    _check_invariants(rom, word)


def test_perturbation_restores_16bit_sum_and_keeps_ffee():
    """A patch that LOWERS the sum is corrected back to == $FFEE, $FFEE intact."""
    rom = _factory_rom()
    word = _stored_word(rom)
    # Perturb real code bytes (drop several toward 0x00 -> lowers the sum a lot).
    for o in range(0x100, 0x100 + 200):
        rom[o] = 0x00
    _assert(sum(rom) & 0xFFFF != word, "perturbation did not change the sum")
    build.whitestar_update_checksum(rom)  # auto-scan
    _check_invariants(rom, word)


def test_perturbation_that_raises_the_sum():
    """A patch that RAISES the sum is also corrected (needs the mod-2^16 wrap)."""
    rom = _factory_rom()
    word = _stored_word(rom)
    for o in range(0x100, 0x100 + 200):
        rom[o] = 0xFE  # raise toward 0xFF
    build.whitestar_update_checksum(rom)
    _check_invariants(rom, word)


def test_explicit_blank_region():
    """--checksum-blank START consumes a contiguous 0xFF run and still fixes up."""
    rom = _factory_rom()
    word = _stored_word(rom)
    for o in range(0x200, 0x200 + 300):
        rom[o] = 0x00
    base = len(rom) - SYS_SIZE
    start = base + (0xF800 - 0x8000)  # inside the synthetic/real blank run
    if any(b != 0xFF for b in rom[start:start + 258]):
        return  # real ROM: this window isn't blank; auto path already covered
    build.whitestar_update_checksum(rom, blank_addr=start)
    _check_invariants(rom, word)


def test_explicit_blank_region_rejects_vector_overlap():
    """Guard: a blank region that would spill into $FFEC-$FFFF must be refused."""
    rom = _factory_rom()
    for o in range(0x100, 0x100 + 400):  # large correction -> many pad bytes
        rom[o] = 0x00
    base = len(rom) - SYS_SIZE
    # Start so close to $FFEC that the run overruns into the vector region.
    bad_start = base + (0xFFEA - 0x8000)
    try:
        build.whitestar_update_checksum(rom, blank_addr=bad_start)
    except BuildDie:
        return
    raise AssertionError("expected a die() for a pad run overrunning $FFEC")


def test_already_correct_is_noop():
    rom = _factory_rom()
    before = bytes(rom)
    build.whitestar_update_checksum(rom)
    _assert(bytes(rom) == before, "no-op fixup modified the ROM")


def test_disabled_word_is_left_alone():
    rom = _factory_rom()
    base = len(rom) - SYS_SIZE
    wo = base + (0xFFEE - 0x8000)
    rom[wo] = rom[wo + 1] = 0xFF  # $FFEE = 0xFFFF -> self-test disabled
    before = bytes(rom)
    build.whitestar_update_checksum(rom)
    _assert(bytes(rom) == before, "fixup touched a checksum-disabled ROM")


# --------------------------------------------------------------------------- #

def _factory_rom():
    p = os.environ.get("LOTR_CPU")
    if not p:
        # Try the sibling lotr repo's factory ROM; else synthesize.
        cand = HERE.parents[3] / "lotr" / "orig" / "cpu" / "lotrcpua.a00"
        p = str(cand) if cand.exists() else None
    if p and Path(p).exists():
        return bytearray(Path(p).read_bytes())
    return _synth_rom()


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    src = os.environ.get("LOTR_CPU") or "(auto: lotr repo or synthetic)"
    print(f"ROM source: {src}")
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
