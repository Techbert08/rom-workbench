#!/usr/bin/env python3
"""
WPC ROM binary explorer.

Usage:
  python rom.py [--rom <zip-or-bin>] info
  python rom.py [--rom <zip-or-bin>] dump <addr> [length]
  python rom.py [--rom <zip-or-bin>] search <"string"> | <HH HH ...>
  python rom.py [--rom <zip-or-bin>] strings [minlen] [--section all|banked|sys]

--rom defaults to auto-detect: searches orig/*.zip relative to CWD, then the
directory containing this script.

Addressing:
  0xNNNNN      file offset (≥5 hex digits or explicit 0x prefix)
  $NNNN        system ROM address ($8000–$FFFF)
  $NNNN@pXX   banked address in WPC page XX (hex), e.g. $4C0E@p37

WPC ROM layout:
  Banked pages occupy file[0 .. romSize-0x8001], page $firstPage first.
  System ROM ($8000–$FFFF) is always the last 32 KiB of the file.
  firstPage = 0x3F - (numPages - 1):  128K→$38, 256K→$34, 512K→$20, 1M→$00
"""

import sys, re, zipfile, argparse, os
from pathlib import Path

SIZE_TO_FIRST_PAGE = {
    128  * 1024: 0x38,
    256  * 1024: 0x34,
    512  * 1024: 0x20,
    1024 * 1024: 0x00,
}
BANK_SIZE = 0x4000
SYS_SIZE  = 0x8000


# ── ROM loading ───────────────────────────────────────────────────────────────

def load_rom(path: str) -> bytes:
    p = Path(path)
    if p.suffix.lower() == '.zip':
        with zipfile.ZipFile(p) as zf:
            entries = zf.infolist()
            def score(e):
                n = e.filename.lower()
                s = 0
                if '_g' in n: s += 10
                if re.match(r'^[a-z]{2,4}s\d', n): s -= 10
                if any(x in n for x in ('sound', 'snd', 'dcs')): s -= 5
                return s
            entries.sort(key=lambda e: -score(e))
            valid = {128*1024, 256*1024, 512*1024, 1024*1024}
            for e in entries:
                if e.file_size in valid:
                    return zf.read(e.filename)
            raise ValueError("No valid game ROM found in zip")
    return p.read_bytes()


def find_rom(start_dir: str = '.') -> str | None:
    for base in (start_dir, str(Path(__file__).parent.parent.parent)):
        orig = Path(base) / 'orig'
        if orig.is_dir():
            for f in sorted(orig.iterdir()):
                if f.suffix.lower() == '.zip':
                    return str(f)
    return None


# ── Address conversion ────────────────────────────────────────────────────────

def addr_to_offset(rom: bytes, spec: str) -> int:
    spec = spec.strip()
    if re.match(r'^0x[0-9a-fA-F]{4,}$', spec) or re.match(r'^[0-9a-fA-F]{5,}$', spec):
        return int(spec, 16)
    m = re.match(r'^\$?([0-9a-fA-F]{4})@p([0-9a-fA-F]{2})$', spec, re.I)
    if m:
        addr = int(m.group(1), 16)
        page = int(m.group(2), 16)
        first = SIZE_TO_FIRST_PAGE[len(rom)]
        if not (first <= page <= 0x3D):
            raise ValueError(f"Page 0x{page:02X} out of range (first=0x{first:02X})")
        return (page - first) * BANK_SIZE + (addr - BANK_SIZE)
    m = re.match(r'^\$([0-9a-fA-F]{4})$', spec, re.I)
    if m:
        addr = int(m.group(1), 16)
        if addr < 0x8000:
            raise ValueError(f"${addr:04X} is below $8000; use $ADDR@pXX for banked pages")
        return (len(rom) - SYS_SIZE) + (addr - 0x8000)
    return int(spec, 0)


def offset_to_label(rom: bytes, offset: int) -> str:
    sys_base = len(rom) - SYS_SIZE
    if offset >= sys_base:
        return f"${0x8000 + (offset - sys_base):04X}"
    first = SIZE_TO_FIRST_PAGE[len(rom)]
    page_num = first + offset // BANK_SIZE
    page_off = offset % BANK_SIZE
    return f"ROM_PAGE_{page_num:02X}::${0x4000 + page_off:04X}"


# ── 6809 disassembler ─────────────────────────────────────────────────────────
#
# Motorola 6809 (the WPC CPU is a 68B09E). Decodes one instruction at a time so
# banked code reads correctly (logical $4000-$7FFF addresses stay in their page).

_BRANCH8 = ['BRA','BRN','BHI','BLS','BCC','BCS','BNE','BEQ',
            'BVC','BVS','BPL','BMI','BGE','BLT','BGT','BLE']
# unary op by low nibble (NEG/COM/LSR/.../CLR); '' = illegal
_UNARY = {0x0:'NEG',0x3:'COM',0x4:'LSR',0x6:'ROR',0x7:'ASR',0x8:'LSL',
          0x9:'ROL',0xA:'DEC',0xC:'INC',0xD:'TST',0xE:'JMP',0xF:'CLR'}
# memory-operation mnemonics by low nibble for the A-accumulator block
_OPS_A = ['SUBA','CMPA','SBCA','SUBD','ANDA','BITA','LDA','STA',
          'EORA','ADCA','ORA','ADDA','CMPX','JSR','LDX','STX']
_OPS_B = ['SUBB','CMPB','SBCB','ADDD','ANDB','BITB','LDB','STB',
          'EORB','ADCB','ORB','ADDB','LDD','STD','LDU','STU']
# low nibbles whose data is 16-bit (affects only immediate operand width)
_WIDE = {0x3,0xC,0xE}
_TFR_REGS = {0x0:'D',0x1:'X',0x2:'Y',0x3:'U',0x4:'S',0x5:'PC',
             0x8:'A',0x9:'B',0xA:'CC',0xB:'DP'}
_IDX_REGS = ['X','Y','U','S']


def _psh_list(pb, is_s):
    bits = [(0x01,'CC'),(0x02,'A'),(0x04,'B'),(0x08,'DP'),(0x10,'X'),
            (0x20,'Y'),(0x40,'U' if is_s else 'S'),(0x80,'PC')]
    return ','.join(n for m,n in bits if pb & m) or '#$00'


def _decode_indexed(rom, off):
    """Decode an indexed postbyte at file offset `off`. Returns (text, nbytes)."""
    pb = rom[off]
    n = 1
    r = _IDX_REGS[(pb >> 5) & 3]
    if not (pb & 0x80):                       # 5-bit signed offset
        v = pb & 0x1F
        if v & 0x10: v -= 0x20
        return f"{v:d},{r}", n
    ind = pb & 0x10
    mode = pb & 0x0F
    def wrap(s): return f"[{s}]" if ind else s
    if   mode == 0x0: txt = f",{r}+"
    elif mode == 0x1: txt = f",{r}++"
    elif mode == 0x2: txt = f",-{r}"
    elif mode == 0x3: txt = f",--{r}"
    elif mode == 0x4: txt = f",{r}"
    elif mode == 0x5: txt = f"B,{r}"
    elif mode == 0x6: txt = f"A,{r}"
    elif mode == 0x8:                         # 8-bit offset
        v = rom[off+1]; n += 1
        if v & 0x80: v -= 0x100
        txt = f"${v & 0xFF:02X},{r}" if v >= 0 else f"-${-v:02X},{r}"
    elif mode == 0x9:                         # 16-bit offset
        v = (rom[off+1] << 8) | rom[off+2]; n += 2
        txt = f"${v:04X},{r}"
    elif mode == 0xB: txt = f"D,{r}"
    elif mode == 0xC:                         # 8-bit PC-relative
        v = rom[off+1]; n += 1
        if v & 0x80: v -= 0x100
        txt = f"${v & 0xFF:02X},PCR"
    elif mode == 0xD:                         # 16-bit PC-relative
        v = (rom[off+1] << 8) | rom[off+2]; n += 2
        txt = f"${v:04X},PCR"
    elif mode == 0xF:                         # extended indirect [n16]
        v = (rom[off+1] << 8) | rom[off+2]; n += 2
        return f"[${v:04X}]", n
    else:
        return f"?idx${pb:02X}", n
    return wrap(txt), n


def disasm_one(rom, off, addr, page):
    """Decode one instruction. Returns (nbytes, mnemonic, operand, target_addr).
    `addr` is the logical 6809 address; `page` the bank (or None for system)."""
    op = rom[off]
    pre = 0
    if op in (0x10, 0x11):
        pre = op
        op = rom[off+1]
    base = off + (2 if pre else 1)
    hi = op & 0xF0
    lo = op & 0x0F

    def u8():  return rom[base]
    def u16(): return (rom[base] << 8) | rom[base+1]
    def s8():  v = rom[base]; return v - 0x100 if v & 0x80 else v

    # ── prefixed (0x10 / 0x11) ────────────────────────────────────────────
    if pre == 0x10:
        if 0x21 <= op <= 0x2F:                # long branches
            disp = u16(); disp -= 0x10000 if disp & 0x8000 else 0
            tgt = (addr + 4 + disp) & 0xFFFF
            return 4, 'L'+_BRANCH8[lo], f"${tgt:04X}", tgt
        m10 = {0x83:('CMPD','imm16'),0x8C:('CMPY','imm16'),0x8E:('LDY','imm16'),
               0x93:('CMPD','dir'),0x9C:('CMPY','dir'),0x9E:('LDY','dir'),0x9F:('STY','dir'),
               0xA3:('CMPD','idx'),0xAC:('CMPY','idx'),0xAE:('LDY','idx'),0xAF:('STY','idx'),
               0xB3:('CMPD','ext'),0xBC:('CMPY','ext'),0xBE:('LDY','ext'),0xBF:('STY','ext'),
               0xCE:('LDS','imm16'),0xDE:('LDS','dir'),0xDF:('STS','dir'),
               0xEE:('LDS','idx'),0xEF:('STS','idx'),0xFE:('LDS','ext'),0xFF:('STS','ext'),
               0x3F:('SWI2','inh')}
        if op in m10: mn, mode = m10[op]
        else: return 2, f"?10{op:02X}", '', None
    elif pre == 0x11:
        m11 = {0x83:('CMPU','imm16'),0x8C:('CMPS','imm16'),
               0x93:('CMPU','dir'),0x9C:('CMPS','dir'),
               0xA3:('CMPU','idx'),0xAC:('CMPS','idx'),
               0xB3:('CMPU','ext'),0xBC:('CMPS','ext'),0x3F:('SWI3','inh')}
        if op in m11: mn, mode = m11[op]
        else: return 2, f"?11{op:02X}", '', None
    # ── 0x00-0x3F ─────────────────────────────────────────────────────────
    elif hi in (0x00,):                       # direct-mode unary
        if lo in _UNARY: mn, mode = _UNARY[lo], 'dir'
        else: return 1, f"?{op:02X}", '', None
    elif 0x12 <= op <= 0x1F or 0x30 <= op <= 0x3F:
        simple = {0x12:'NOP',0x13:'SYNC',0x19:'DAA',0x1D:'SEX',0x39:'RTS',
                  0x3A:'ABX',0x3B:'RTI',0x3D:'MUL',0x3F:'SWI'}
        if op in simple: mn, mode = simple[op], 'inh'
        elif op == 0x16:
            disp = u16(); disp -= 0x10000 if disp & 0x8000 else 0
            tgt = (addr + 3 + disp) & 0xFFFF
            return 3, 'LBRA', f"${tgt:04X}", tgt
        elif op == 0x17:
            disp = u16(); disp -= 0x10000 if disp & 0x8000 else 0
            tgt = (addr + 3 + disp) & 0xFFFF
            return 3, 'LBSR', f"${tgt:04X}", tgt
        elif op in (0x1A,0x1C,0x3C): mn, mode = {0x1A:'ORCC',0x1C:'ANDCC',0x3C:'CWAI'}[op], 'imm8'
        elif op in (0x1E,0x1F):
            pb = u8()
            s = _TFR_REGS.get(pb >> 4,'?'); d = _TFR_REGS.get(pb & 0xF,'?')
            return 2, ('EXG' if op==0x1E else 'TFR'), f"{s},{d}", None
        elif op in (0x30,0x31,0x32,0x33):
            txt, n = _decode_indexed(rom, base)
            return 1+n, {0x30:'LEAX',0x31:'LEAY',0x32:'LEAS',0x33:'LEAU'}[op], txt, None
        elif op in (0x34,0x35,0x36,0x37):
            pb = u8(); is_s = op in (0x34,0x35)
            return 2, {0x34:'PSHS',0x35:'PULS',0x36:'PSHU',0x37:'PULU'}[op], _psh_list(pb,is_s), None
        else: return 1, f"?{op:02X}", '', None
    elif 0x20 <= op <= 0x2F:                  # short branches
        tgt = (addr + 2 + s8()) & 0xFFFF
        return 2, _BRANCH8[lo], f"${tgt:04X}", tgt
    # ── 0x40-0x5F  accumulator inherent unary ─────────────────────────────
    elif hi in (0x40,0x50):
        if lo in _UNARY and lo != 0xE:
            return 1, _UNARY[lo] + ('A' if hi==0x40 else 'B'), '', None
        return 1, f"?{op:02X}", '', None
    # ── 0x60-0x7F  indexed / extended unary ───────────────────────────────
    elif hi in (0x60,0x70):
        if lo not in _UNARY: return 1, f"?{op:02X}", '', None
        mn = _UNARY[lo]
        mode = 'idx' if hi==0x60 else 'ext'
    # ── 0x80-0xBF  A-accumulator / X ops ; 0xC0-0xFF  B / D / U ops ────────
    elif 0x80 <= op <= 0xFF:
        ops = _OPS_A if op < 0xC0 else _OPS_B
        mn = ops[lo]
        col = hi & 0x30                       # 0x00 imm,0x10 dir,0x20 idx,0x30 ext
        if op == 0x8D:                        # BSR is the odd one in the imm column
            tgt = (addr + 2 + s8()) & 0xFFFF
            return 2, 'BSR', f"${tgt:04X}", tgt
        if col == 0x00:                       # immediate
            if mn.startswith('ST') or mn == 'JSR':
                return 1, f"?{op:02X}", '', None
            mode = 'imm16' if lo in _WIDE else 'imm8'
        else:
            mode = {0x10:'dir',0x20:'idx',0x30:'ext'}[col]
    else:
        return 1, f"?{op:02X}", '', None

    # ── render operand for the common modes ───────────────────────────────
    nbytes = base - off
    tgt = None
    if mode == 'inh':
        operand = ''
    elif mode == 'imm8':
        operand = f"#${u8():02X}"; nbytes += 1
    elif mode == 'imm16':
        operand = f"#${u16():04X}"; nbytes += 2
    elif mode == 'dir':
        operand = f"<${u8():02X}"; nbytes += 1
    elif mode == 'ext':
        a = u16(); operand = f"${a:04X}"; nbytes += 2
        if mn in ('JSR','JMP'): tgt = a
    elif mode == 'idx':
        txt, n = _decode_indexed(rom, base); operand = txt; nbytes += n
    else:
        operand = '?'
    return nbytes, mn, operand, tgt


def cmd_dis(rom, spec, length: int = 64):
    off = addr_to_offset(rom, spec)
    # recover logical addr + page from the spec
    m = re.match(r'^\$?([0-9a-fA-F]{4})(?:@p([0-9a-fA-F]{2}))?$', spec.strip(), re.I)
    if m:
        addr = int(m.group(1), 16)
        page = int(m.group(2), 16) if m.group(2) else None
    else:                                     # file-offset spec
        lbl = offset_to_label(rom, off)
        mm = re.search(r'\$([0-9A-F]{4})', lbl)
        addr = int(mm.group(1), 16) if mm else 0x4000
        page = None if lbl.startswith('$') else int(lbl[9:11], 16)
    psuf = f"@p{page:02X}" if page is not None else ""
    print(f"Disassemble {length} bytes from {spec} (file 0x{off:05X}):")
    end = min(off + length, len(rom))
    while off < end:
        n, mn, operand, tgt = disasm_one(rom, off, addr, page)
        raw = ' '.join(f"{b:02X}" for b in rom[off:off+n])
        # annotate jump/branch targets with page (banked targets stay in-page)
        ann = ''
        if tgt is not None:
            if tgt >= 0x8000:                 tann = f"${tgt:04X}"
            elif 0x4000 <= tgt < 0x8000 and page is not None: tann = f"${tgt:04X}@p{page:02X}"
            else:                             tann = f"${tgt:04X}"
            ann = f"   -> {tann}"
        print(f"  ${addr:04X}{psuf}  {raw:<14}  {mn:<6} {operand}{ann}")
        off += n
        addr = (addr + n) & 0xFFFF


# ── Code map: recursive-descent disassembly (xref / funcs) ────────────────────
#
# Bank-aware static analysis to replace Ghidra for the day-to-day "who calls /
# references this address" and "where do functions start" questions. We descend
# from seeds (function prologues + CPU vectors), following intra-region control
# flow, and record every control/data edge. Banked $4000-$7FFF targets stay in
# the source's page (that's the only page mapped while it runs); $8000+ targets
# are the global system region.
#
# Known limit (documented, not a bug): cross-page calls go through the WPC OS
# bank-switch dispatcher (system code jumping to $4xxx with the page supplied at
# runtime). Those can't be statically resolved to a page, so a banked function
# only reachable via dispatch — never via an intra-page call or a PSHS/PSHU
# prologue — may be missed. Use the live debugger's stack unwind for those.

_CONTROL = (set(_BRANCH8) | {'L' + b for b in _BRANCH8}
            | {'BSR', 'LBSR', 'JSR', 'JMP'})


def _addr_off(rom, addr, page):
    first = SIZE_TO_FIRST_PAGE[len(rom)]
    if page is not None:
        return (page - first) * BANK_SIZE + (addr - BANK_SIZE)
    return (len(rom) - SYS_SIZE) + (addr - 0x8000)


def _classify(mn, operand):
    if mn in ('RTS', 'RTI'):                       return 'stop'
    if mn in ('JMP', 'BRA', 'LBRA'):               return 'stop'   # but target is followed
    if mn in ('PULS', 'PULU') and 'PC' in operand: return 'stop'
    if mn in ('JSR', 'BSR', 'LBSR'):               return 'call'
    if mn in _CONTROL and mn not in ('BRA', 'LBRA', 'BRN', 'LBRN'):
        return 'cond'
    return 'normal'


_EXT_RE = re.compile(r'^\$([0-9A-F]{4})$')


def build_codemap(rom):
    """Returns (func_starts, edges).
    func_starts: set of (page_or_None, addr).
    edges: list of (src_addr, src_page, mn, tgt_addr, kind) where kind is
           'call' | 'jump' | 'branch' | 'data'."""
    first = SIZE_TO_FIRST_PAGE[len(rom)]
    sys_base = len(rom) - SYS_SIZE

    visited: set[int] = set()        # instruction-start file offsets
    func_starts: set = set()
    edges: list = []
    work: list = []                  # (addr, page)

    def region_of(addr, page):
        return (0x4000, 0x8000) if page is not None else (0x8000, 0x10000)

    # ── seeds: prologues (PSHS/PSHU with a reg list) across the whole image.
    #    These bootstrap the descent but are NOT reported as functions unless
    #    they turn out to be call targets — a raw 0x34/0x36 byte often lands in
    #    data, so seeding-as-function would be noise. Only call/vector targets
    #    (below) are trusted as real entries.
    for off in range(len(rom) - 1):
        if rom[off] in (0x34, 0x36) and rom[off + 1] not in (0x00,):
            if off >= sys_base:
                page, addr = None, 0x8000 + (off - sys_base)
            else:
                page, addr = first + off // BANK_SIZE, 0x4000 + off % BANK_SIZE
            work.append((addr, page))
    # ── seeds: CPU vectors $FFF0-$FFFF (each a $8000+ entry) ──
    for v in range(0xFFF2, 0x10000, 2):
        off = sys_base + (v - 0x8000)
        tgt = (rom[off] << 8) | rom[off + 1]
        if tgt >= 0x8000:
            work.append((tgt, None)); func_starts.add((None, tgt))

    while work:
        addr, page = work.pop()
        lo, hi = region_of(addr, page)
        if not (lo <= addr < hi):
            continue
        off = _addr_off(rom, addr, page)
        if off in visited or not (0 <= off < len(rom)):
            continue
        visited.add(off)
        n, mn, operand, tgt = disasm_one(rom, off, addr, page)
        if mn.startswith('?'):       # ran into data; abandon this path
            continue
        cls = _classify(mn, operand)

        if mn in _CONTROL and tgt is not None:
            kind = 'call' if cls == 'call' else ('jump' if mn in ('JMP', 'BRA', 'LBRA') else 'branch')
            edges.append((addr, page, mn, tgt, kind))
            # enqueue + register the target
            if 0x4000 <= tgt < 0x8000:
                if page is not None:                # banked target = same page
                    work.append((tgt, page))
                    if kind == 'call': func_starts.add((page, tgt))
                # else sys->banked: page unknown at static time; don't follow
            elif tgt >= 0x8000:
                work.append((tgt, None))
                if kind == 'call': func_starts.add((None, tgt))
        else:
            m = _EXT_RE.match(operand)              # extended data reference
            if m:
                edges.append((addr, page, mn, int(m.group(1), 16), 'data'))

        if cls != 'stop':
            work.append((addr + n, page))

    return func_starts, edges


def _fmt_loc(addr, page):
    return f"${addr:04X}@p{page:02X}" if page is not None else f"${addr:04X}"


def cmd_funcs(rom, page_spec=None):
    want = None
    if page_spec is not None:
        want = int(str(page_spec).lstrip('p').lstrip('$'), 16)
    func_starts, _ = build_codemap(rom)
    rows = sorted(func_starts, key=lambda pa: (pa[0] if pa[0] is not None else 0xFF, pa[1]))
    shown = 0
    for pg, addr in rows:
        if want is not None and pg != want:
            continue
        print(f"  {_fmt_loc(addr, pg)}")
        shown += 1
    print(f"{shown} function start(s)" + (f" in page 0x{want:02X}" if want is not None else ""))


def cmd_xref(rom, spec, data=False):
    spec = spec.strip()
    m = re.match(r'^\$?([0-9a-fA-F]{4})@p([0-9a-fA-F]{2})$', spec, re.I)
    if m:
        T, Tpage = int(m.group(1), 16), int(m.group(2), 16)
    else:
        mm = re.match(r'^\$?([0-9a-fA-F]{4})$', spec, re.I)
        if not mm:
            raise ValueError(f"xref target must be $NNNN or $NNNN@pXX, got {spec!r}")
        T, Tpage = int(mm.group(1), 16), None
        # Only $4000-$7FFF is the banked window. $0000-$3FFF (RAM/IO) and
        # $8000-$FFFF (system ROM) are global — references to them come from
        # any page, so xref them with Tpage=None (no page required).
        if 0x4000 <= T < 0x8000:
            raise ValueError(f"${T:04X} is banked; specify the page as $NNNN@pXX")
    _, edges = build_codemap(rom)
    hits = []
    for src_addr, src_page, mn, tgt, kind in edges:
        if tgt != T:
            continue
        if kind == 'data' and not data:
            continue
        if Tpage is not None:                      # banked target: callers share its page
            if src_page != Tpage:
                continue
        else:                                      # system target: any caller
            pass
        hits.append((src_page, src_addr, mn, kind))
    hits.sort(key=lambda h: (h[0] if h[0] is not None else 0xFF, h[1]))
    label = _fmt_loc(T, Tpage)
    print(f"xref {label}  ({len(hits)} reference(s)"
          + ("" if data else "; add --data for LD/ST refs") + "):")
    for src_page, src_addr, mn, kind in hits:
        print(f"  {_fmt_loc(src_addr, src_page):14s}  {mn:<5} ({kind})")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_info(rom: bytes):
    first     = SIZE_TO_FIRST_PAGE[len(rom)]
    num_pages = len(rom) // BANK_SIZE
    sys_base  = len(rom) - SYS_SIZE

    cksum_off   = sys_base + (0xFFEE - 0x8000)
    cksum_word  = (rom[cksum_off] << 8) | rom[cksum_off + 1]
    ver_byte    = cksum_word & 0xFF
    delta_off   = sys_base + (0xFFEC - 0x8000)
    delta_word  = (rom[delta_off] << 8) | rom[delta_off + 1]
    reset_off   = sys_base + (0xFFFE - 0x8000)
    reset_vec   = (rom[reset_off] << 8) | rom[reset_off + 1]

    print(f"ROM size    : {len(rom)//1024} KiB  ({len(rom):,} bytes)")
    print(f"Pages       : {num_pages} total  ({num_pages-2} banked ${first:02X}-$3D + 2 system)")
    print(f"System ROM  : file 0x{sys_base:05X}-0x{len(rom)-1:05X}  -> $8000-$FFFF")
    print(f"Version     : {(ver_byte>>4)&0xF}.{ver_byte&0xF}  "
          f"(ver_byte=0x{ver_byte:02X} @ $FFEE / file 0x{cksum_off:05X})")
    print(f"Checksum    : 0x{cksum_word:04X}  delta=0x{delta_word:04X}  "
          f"{'[DISABLED]' if delta_word == 0x00FF else '[ENFORCED]'}")
    print(f"RESET vec   : ${reset_vec:04X}  (@ $FFFE / file 0x{reset_off:05X})")
    try:
        end = min(next((i for i, b in enumerate(rom[:256]) if b == 0xFF), 80), 256)
        print(f"Copyright   : {rom[:end].decode('ascii', errors='replace').strip()}")
    except Exception:
        pass


def cmd_dump(rom: bytes, spec: str, length: int = 64):
    offset = addr_to_offset(rom, spec)
    print(f"Dump {length} bytes at {spec} (file 0x{offset:05X}):")
    end, row = min(offset + length, len(rom)), 16
    for base in range(offset, end, row):
        chunk    = rom[base:min(base+row, end)]
        hex_part = ' '.join(f'{b:02X}' for b in chunk)
        asc_part = ''.join(chr(b) if 0x20 <= b <= 0x7E else '.' for b in chunk)
        print(f"  {offset_to_label(rom, base):30s}  0x{base:05X}: {hex_part:<47}  |{asc_part}|")


def cmd_search(rom: bytes, pattern: str):
    if pattern.startswith('"') and pattern.endswith('"'):
        needle = pattern[1:-1].encode('ascii')
        desc   = f'ASCII "{pattern[1:-1]}"'
    else:
        needle = bytes(int(x, 16) for x in pattern.split())
        desc   = pattern
    print(f"Searching {len(rom)//1024} KiB for {desc} ({len(needle)} bytes)...")
    hits, i = [], 0
    while (i := rom.find(needle, i)) != -1:
        hits.append(i); i += 1
    print(f"  {len(hits)} hit(s)")
    for h in hits:
        lo  = max(0, h - 4)
        ctx = rom[lo : min(h + len(needle) + 12, len(rom))]
        print(f"  {offset_to_label(rom, h):30s}  0x{h:05X}: "
              f"{' '.join(f'{b:02X}' for b in ctx):<48}  "
              f"|{''.join(chr(b) if 0x20<=b<=0x7E else '.' for b in ctx)}|")


def cmd_strings(rom: bytes, minlen: int = 4, section: str = 'all'):
    sys_base = len(rom) - SYS_SIZE
    if   section == 'sys':    data, base_off = rom[sys_base:], sys_base
    elif section == 'banked': data, base_off = rom[:sys_base], 0
    else:                     data, base_off = rom, 0

    run, run_start = [], 0
    for i, b in enumerate(data):
        if 0x20 <= b <= 0x7E:
            if not run: run_start = i
            run.append(b)
        else:
            if len(run) >= minlen:
                off = base_off + run_start
                print(f"  {offset_to_label(rom, off):30s}  0x{off:05X}: \"{bytes(run).decode()}\"")
            run = []
    if len(run) >= minlen:
        off = base_off + run_start
        print(f"  {offset_to_label(rom, off):30s}  0x{off:05X}: \"{bytes(run).decode()}\"")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='WPC ROM binary explorer')
    ap.add_argument('--rom', default=None, help='ROM .zip or raw binary (default: auto-detect from orig/)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('info', help='ROM metadata: version, checksum, page layout, copyright')

    pd = sub.add_parser('dump', help='Hex+ASCII dump')
    pd.add_argument('addr',   help='$NNNN (sys), $NNNN@pXX (banked), or 0xNNNNN (file offset)')
    pd.add_argument('length', nargs='?', type=lambda x: int(x, 0), default=64)

    ps = sub.add_parser('search', help='Search for byte pattern or ASCII string')
    ps.add_argument('pattern', help='"quoted string" or space-separated hex bytes')

    pst = sub.add_parser('strings', help='Find ASCII runs')
    pst.add_argument('minlen', nargs='?', type=int, default=4)
    pst.add_argument('--section', choices=['all', 'banked', 'sys'], default='all')

    pdi = sub.add_parser('dis', help='6809 disassembly')
    pdi.add_argument('addr',   help='$NNNN (sys), $NNNN@pXX (banked), or 0xNNNNN (file offset)')
    pdi.add_argument('length', nargs='?', type=lambda x: int(x, 0), default=64)

    pxr = sub.add_parser('xref', help='Who references an address (recursive-descent)')
    pxr.add_argument('addr', help='$NNNN (sys) or $NNNN@pXX (banked)')
    pxr.add_argument('--data', action='store_true', help='also include LD/ST data refs')

    pfn = sub.add_parser('funcs', help='Discovered function starts (recursive-descent)')
    pfn.add_argument('--page', default=None, help='restrict to one page, e.g. 39')

    args = ap.parse_args()
    rom_path = args.rom or find_rom()
    if not rom_path:
        print("ERROR: ROM not found. Pass --rom <path> or place zip in orig/", file=sys.stderr)
        sys.exit(1)

    rom = load_rom(rom_path)
    if len(rom) not in SIZE_TO_FIRST_PAGE:
        print(f"WARNING: unusual ROM size {len(rom)} bytes — mapping may be wrong", file=sys.stderr)

    {'info': cmd_info, 'dump': cmd_dump, 'search': cmd_search,
     'strings': cmd_strings, 'dis': cmd_dis,
     'xref': cmd_xref, 'funcs': cmd_funcs}[args.cmd](
        *([rom] + ([args.addr, args.length] if args.cmd in ('dump', 'dis')
                   else [args.pattern]      if args.cmd == 'search'
                   else [args.minlen, args.section] if args.cmd == 'strings'
                   else [args.addr, args.data] if args.cmd == 'xref'
                   else [args.page]            if args.cmd == 'funcs'
                   else []))
    )


if __name__ == '__main__':
    main()
