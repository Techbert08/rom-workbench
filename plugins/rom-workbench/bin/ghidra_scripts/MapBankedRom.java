// MapBankedRom.java — generic Ghidra headless post-script for banked 6809 pinball ROMs
// (Williams WPC / Stern Whitestar). The stock raw-binary loader maps a flat image and
// clips a >64KB ROM; this rebuilds the real 6809 memory map (RAM + one banked page in
// the banked window + the fixed resident bank), pins the direct-page register, models the
// inline-argument calling convention (the #1 cause of misalignment — see below), labels
// known RAM, then recursive-descent disassembles + DECOMPILES the requested addresses to
// <outDir>/<ADDR>.c (optionally following called helpers).
//
// ── Why decompilation kept misaligning (the trampoline ABI) ────────────────────────────
// These ROMs make far/cross-bank calls and spawn scheduler tasks through *trampoline*
// routines that read their arguments from INLINE DATA bytes placed right after the call
// site — execution resumes AFTER those bytes, not at them. e.g. on LOTR:
//     BD B3 E6        JSR  $B3E6          ; far-call trampoline
//     43 01 38        <inline: hi,lo,bank>; NOT code — args consumed by $B3E6
//     35 02           PULS A             ; real code resumes here
// Ghidra's recursive descent doesn't know $B3E6 swallows 3 bytes, so it decodes `43 01 38`
// as instructions (COMA / JMP <$38) and derails the whole routine ("Could not recover
// jumptable", `*0x100 + 0x38` indirect-call noise). The fix: declare each trampoline and
// its inline-arg width; this script then, for every call site, marks the inline bytes as
// data, overrides the call's fall-through to resume after them, re-disassembles, and
// annotates the decoded target as an EOL comment. This is the general WPC/Whitestar
// "call-with-inline-args" convention, so `trampoline=`/`abi=` are reusable across games.
//
// ── Config ──────────────────────────────────────────────────────────────────────────────
// Takes ONE positional arg: the path to a line-based config file. (A single path is used
// instead of many args because cmd/analyzeHeadless.bat re-split on space/comma/semicolon/
// equals, which mangles list args on Windows.) Config keys (repeatable ones noted):
//
//   rom=<absolute path to the CPU ROM image on disk>      (the .aNN/.bin file, NOT the zip)
//   out=<dir for the decompiled <ADDR>.c files>           (created if absent)
//   dp=<hex>            direct-page value (0 typical); omit / "none" to skip pinning DP
//   block=name:cpuHex:fileHex:sizeHex     [repeatable]   fileHex="none" => uninitialized RAM
//   label=cpuHex:name                     [repeatable]   makes the C read `name` not DAT_xxxx
//   trampoline=cpuHex:nbytes[:format]     [repeatable]   inline-arg call (see formats below)
//   abi=whitestar                                         preset: registers LOTR/Whitestar
//                                                         trampolines + a few SFR labels
//   follow=<depth>                                        also decompile called helpers, to
//                                                         this call-graph depth (default 0)
//   target=hexAddr                        [repeatable]   each: recursive disasm + decompile
//
// trampoline formats (how to decode + annotate the inline bytes; default raw):
//   farcall  [hi,lo,bank]      -> "far-call $hilo @ p<bank>"
//   spawn4   [id,bank,hi,lo]   -> "spawn id=$id @ p<bank> resumePC=$hilo"
//   spawn3   [id,hi,lo]        -> "spawn id=$id resumePC=$hilo (bank fixed)"
//   raw      <n bytes>         -> "inline: NN NN .."
//
// '#' lines and blanks are ignored. Geometry comes from the CALLER (it knows the platform):
//   Whitestar: banked window CPU $4000-$7FFF <- file page*0x4000; resident $8000-$FFFF <- file 0x18000.
//   WPC:       banked window CPU $4000-$7FFF; system/resident in the upper bank — derive per ROM.

import java.io.ByteArrayInputStream;
import java.io.PrintWriter;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.data.ArrayDataType;
import ghidra.program.model.data.ByteDataType;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.SourceType;

public class MapBankedRom extends GhidraScript {

    AddressSpace space;
    Address a(int off) { return space.getAddress(off & 0xFFFF); }
    static int h(String s) { return Integer.parseInt(s.replaceFirst("(?i)^0x", ""), 16); }

    // a declared inline-argument trampoline: how many bytes it consumes + how to annotate
    static class Tramp { int nbytes; String fmt; Tramp(int n, String f) { nbytes = n; fmt = f; } }
    Map<Integer, Tramp> tramps = new HashMap<>();

    @Override
    public void run() throws Exception {
        space = currentProgram.getAddressFactory().getDefaultAddressSpace();
        Memory mem = currentProgram.getMemory();
        String[] args = getScriptArgs();
        if (args.length < 1) { println("usage: MapBankedRom <configFile>"); return; }

        String rom = null, out = null, dp = "none";
        int follow = 0;
        List<String> blocks = new ArrayList<>(), labels = new ArrayList<>(), targets = new ArrayList<>();
        for (String raw : Files.readAllLines(Paths.get(args[0]))) {
            String line = raw.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            int hash = line.indexOf('#');                 // strip trailing inline comments
            if (hash >= 0) line = line.substring(0, hash).trim();
            if (line.isEmpty()) continue;
            int eq = line.indexOf('=');
            if (eq < 0) continue;
            String k = line.substring(0, eq).trim(), v = line.substring(eq + 1).trim();
            switch (k) {
                case "rom":        rom = v; break;
                case "out":        out = v; break;
                case "dp":         dp = v; break;
                case "block":      blocks.add(v); break;
                case "label":      labels.add(v); break;
                case "target":     targets.add(v); break;
                case "follow":     follow = Integer.parseInt(v.trim()); break;
                case "trampoline": addTramp(v); break;
                case "abi":        applyAbi(v, labels); break;
                default:           println("ignoring unknown key: " + k);
            }
        }
        if (rom == null || out == null) { println("config needs rom= and out="); return; }

        byte[] image = Files.readAllBytes(Paths.get(rom));
        println("ROM " + rom + " size=" + image.length);
        for (MemoryBlock b : mem.getBlocks()) mem.removeBlock(b, monitor);

        // --- memory blocks: name:cpuHex:fileHex:sizeHex (fileHex "none" => RAM) ---
        for (String spec : blocks) {
            String[] p = spec.split(":");
            String name = p[0];
            int cpu = h(p[1]), size = h(p[3]);
            if (p[2].equalsIgnoreCase("none")) {
                MemoryBlock blk = mem.createUninitializedBlock(name, a(cpu), size, false);
                blk.setRead(true); blk.setWrite(true); blk.setExecute(false);
                println("RAM block " + name + " $" + Integer.toHexString(cpu) + " +" + size);
            } else {
                int foff = h(p[2]);
                byte[] bytes = new byte[size];
                System.arraycopy(image, foff, bytes, 0, size);
                MemoryBlock blk = mem.createInitializedBlock(name, a(cpu),
                        new ByteArrayInputStream(bytes), size, monitor, false);
                blk.setRead(true); blk.setWrite(false); blk.setExecute(true);
                println("ROM block " + name + " $" + Integer.toHexString(cpu)
                        + " <- file 0x" + Integer.toHexString(foff) + " +" + size);
            }
        }

        // --- pin direct-page register (banked 6809 games run DP=0; otherwise the
        //     decompiler emits "in_DP*0x100+offs" noise for every direct access) ---
        if (!dp.equalsIgnoreCase("none") && !dp.isEmpty()) {
            Register dpr = currentProgram.getRegister("DP");
            if (dpr != null) {
                BigInteger val = new BigInteger(dp.replaceFirst("(?i)^0x", ""), 16);
                currentProgram.getProgramContext().setValue(dpr, a(0x0000), a(0xFFFF), val);
                println("DP=" + val + " pinned over $0000-$FFFF");
            } else println("no DP register in this language");
        }

        // --- label known RAM (cpuHex:name) for readable decompilation ---
        for (String kv : labels) {
            String[] p = kv.split(":");
            try { createLabel(a(h(p[0])), p[1], true, SourceType.USER_DEFINED); }
            catch (Exception e) { println("label fail " + kv + ": " + e.getMessage()); }
        }

        // --- disassemble every target, then fix inline-arg trampoline call sites to a
        //     fixpoint (each fix can reveal more code, which can contain more call sites) ---
        for (String t : targets) {
            Address addr = a(h(t));
            new DisassembleCommand(addr, null, true).applyTo(currentProgram, monitor);
        }
        if (!tramps.isEmpty()) {
            int fixes = fixTrampolinesToFixpoint();
            println("trampoline inline-arg fixes applied: " + fixes);
        }

        // --- pick the set of addresses to decompile (targets + followed helpers) ---
        Set<Integer> toDecompile = new LinkedHashSet<>();
        for (String t : targets) toDecompile.add(h(t));
        if (follow > 0) collectCalledHelpers(toDecompile, follow);

        // --- phase A: disassemble + create a function at every address we'll decompile,
        //     plus each trampoline routine, BEFORE decompiling any. This way cross-calls
        //     render as FUN_xxxx / the trampoline name instead of UNK_xxxx pointers. ---
        Set<Integer> funcs = new LinkedHashSet<>(toDecompile);
        for (Integer tAddr : tramps.keySet()) {
            MemoryBlock blk = mem.getBlock(a(tAddr));
            if (blk != null && blk.isExecute() && blk.isInitialized()) funcs.add(tAddr);
        }
        for (int off : funcs) {
            new DisassembleCommand(a(off), null, true).applyTo(currentProgram, monitor);
        }
        // mark `PULS/PULU ...,PC` as RETURN before building function bodies (below), so the
        // bodies close at the return instead of running off into a phantom computed jump.
        int rets = fixPulsPcReturns();
        if (rets > 0) println("PULS/PULU-PC return overrides applied: " + rets);
        for (int off : funcs) {
            new CreateFunctionCmd(a(off)).applyTo(currentProgram, monitor);
        }

        // --- phase B: decompile each requested target to <out>/<ADDR>.c ---
        Files.createDirectories(Paths.get(out));
        DecompInterface dec = new DecompInterface();
        // surface our inline-arg annotations (decoded far-call/spawn targets) in the C
        DecompileOptions opts = new DecompileOptions();
        opts.setPRECommentIncluded(true);
        opts.setEOLCommentIncluded(true);
        dec.setOptions(opts);
        dec.openProgram(currentProgram);
        for (int off : toDecompile) {
            Address addr = a(off);
            Function f = getFunctionAt(addr);
            String hx = Integer.toHexString(off).toUpperCase();
            String c;
            if (f == null) c = "(no function at $" + hx + ")";
            else {
                DecompileResults r = dec.decompileFunction(f, 120, monitor);
                c = (r != null && r.decompileCompleted())
                        ? r.getDecompiledFunction().getC()
                        : "(decompile failed: " + (r == null ? "null" : r.getErrorMessage()) + ")";
            }
            println("\n========== DECOMP $" + hx + " ==========\n" + c);
            try (PrintWriter pw = new PrintWriter(out + "/" + hx + ".c")) { pw.println(c); }
        }
        dec.dispose();
    }

    // ── trampoline registration ────────────────────────────────────────────────────────
    void addTramp(String v) {
        String[] p = v.split(":");
        int addr = h(p[0]), n = h(p[1]);
        String fmt = (p.length > 2) ? p[2].trim() : "raw";
        tramps.put(addr & 0xFFFF, new Tramp(n, fmt));
        println("trampoline $" + Integer.toHexString(addr) + " consumes " + n
                + " inline byte(s), fmt=" + fmt);
    }

    // Known presets so callers don't re-type the ABI for a supported platform.
    void applyAbi(String name, List<String> labels) {
        if (name.equalsIgnoreCase("whitestar")) {
            // LOTR / Stern Whitestar trampolines (confirmed against rom.py + live trace):
            // Each verified against rom.py: the routine does `LDX ,S` / `LDA[,LDB] ,X+`
            // (reads the inline byte[s] off the return address) then advances the return.
            addTramp("B3E6:3:farcall");  // bank-switching far-call: FCB hi,lo,bank
            addTramp("A233:4:spawn4");   // task spawn: FCB id,bank,pcHi,pcLo
            addTramp("A242:3:spawn3");   // task spawn (bank fixed $3A): FCB id,pcHi,pcLo
            addTramp("A45E:1:raw");      // 1-byte inline arg ($A45E: LDX,S / LDA,X+ / LEAS 2,S)
            labels.add("243:bankShadow");
            println("abi=whitestar: registered B3E6/A233/A242/A45E + bankShadow label");
        } else {
            println("unknown abi preset: " + name);
        }
    }

    // ── the misalignment fix ──────────────────────────────────────────────────────────
    // For every call site that targets a declared trampoline: mark the following N bytes as
    // data, override the call's fall-through to resume after them, re-disassemble there, and
    // annotate the decoded inline args. Repeats until no new fixes (fixes reveal more code).
    int fixTrampolinesToFixpoint() throws Exception {
        Listing listing = currentProgram.getListing();
        int total = 0;
        for (int pass = 0; pass < 64; pass++) {
            // Collect candidates first — applying fixes mutates the listing mid-iteration.
            List<Instruction> sites = new ArrayList<>();
            InstructionIterator it = listing.getInstructions(true);
            while (it.hasNext()) {
                Instruction ins = it.next();
                if (!ins.getFlowType().isCall()) continue;
                Tramp tr = trampFor(ins);
                if (tr == null) continue;
                Address resume = ins.getMaxAddress().add(1 + tr.nbytes);
                Address have = ins.getFallThrough();
                if (have != null && have.equals(resume)) continue; // already fixed
                sites.add(ins);
            }
            if (sites.isEmpty()) break;
            for (Instruction ins : sites) {
                if (fixOne(listing, ins)) total++;
            }
        }
        return total;
    }

    // On the 6809 `PULS regs,PC` / `PULU regs,PC` is the standard subroutine RETURN (it
    // pops the saved PC). Ghidra's 6809 model instead treats it as a computed jump and emits
    // a bogus "Could not recover jumptable ... (*UNRECOVERED_JUMPTABLE)()". Force the flow to
    // RETURN so the decompiler closes the function cleanly. Always valid for this ABI.
    int fixPulsPcReturns() throws Exception {
        Register pc = currentProgram.getRegister("PC");
        int n = 0;
        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext()) {
            Instruction ins = it.next();
            String m = ins.getMnemonicString();
            if (!m.equals("PULS") && !m.equals("PULU")) continue;
            if (ins.getFlowOverride() == ghidra.program.model.listing.FlowOverride.RETURN) continue;
            boolean hasPc = false;
            for (int op = 0; op < ins.getNumOperands() && !hasPc; op++)
                for (Object o : ins.getOpObjects(op))
                    if ((o instanceof Register && o == pc)
                            || (pc != null && o instanceof Register
                                && ((Register) o).getName().equalsIgnoreCase("PC"))) { hasPc = true; break; }
            if (!hasPc) continue;
            ins.setFlowOverride(ghidra.program.model.listing.FlowOverride.RETURN);
            n++;
        }
        return n;
    }

    Tramp trampFor(Instruction ins) {
        Integer t = callTargetOffset(ins);
        return (t == null) ? null : tramps.get(t);
    }

    // The static destination of a direct/extended/relative call, as a $0000-$FFFF offset.
    // We read the operand directly rather than getFlows()/getReferencesFrom(): in headless
    // post-script mode the reference analyzer hasn't run after our manual DisassembleCommand,
    // so flow references don't exist yet (getFlows() returns []). Indexed/indirect calls
    // (operand is a register, no Address) return null and are skipped.
    Integer callTargetOffset(Instruction ins) {
        Address[] flows = ins.getFlows();
        if (flows != null && flows.length > 0) return (int) (flows[0].getOffset() & 0xFFFF);
        for (int op = 0; op < ins.getNumOperands(); op++) {
            for (Object o : ins.getOpObjects(op)) {
                if (o instanceof Address) return (int) (((Address) o).getOffset() & 0xFFFF);
                // 6809 JSR-extended / BSR render the target as a Scalar (absolute address).
                if (o instanceof Scalar) return (int) (((Scalar) o).getUnsignedValue() & 0xFFFF);
            }
        }
        return null;
    }

    boolean fixOne(Listing listing, Instruction ins) throws Exception {
        Tramp tr = trampFor(ins);
        if (tr == null) return false;
        Address inlineStart = ins.getMaxAddress().add(1);
        Address inlineEnd = inlineStart.add(tr.nbytes - 1);
        Address resume = inlineEnd.add(1);
        // 1. wipe whatever (mis)disassembly covers the inline bytes, then lock them as data.
        clearListing(inlineStart, inlineEnd);
        try { createData(inlineStart, new ArrayDataType(ByteDataType.dataType, tr.nbytes, 1)); }
        catch (Exception e) { /* bytes already data / boundary clash — non-fatal */ }
        // 2. resumption is after the inline args, not at them.
        ins.setFallThrough(resume);
        // 3. annotate the decoded target (PRE so the decompiler renders it above the call),
        //    and disassemble the real continuation.
        setPreComment(ins.getAddress(), annotate(tr, inlineStart));
        new DisassembleCommand(resume, null, true).applyTo(currentProgram, monitor);
        return true;
    }

    String annotate(Tramp tr, Address p) throws Exception {
        int[] b = new int[tr.nbytes];
        for (int i = 0; i < tr.nbytes; i++) b[i] = getByte(p.add(i)) & 0xFF;
        switch (tr.fmt) {
            case "farcall":
                if (b.length >= 3) return String.format("far-call $%02X%02X @ p%02X", b[0], b[1], b[2]);
                break;
            case "spawn4":
                if (b.length >= 4) return String.format("spawn id=$%02X @ p%02X resumePC=$%02X%02X",
                        b[0], b[1], b[2], b[3]);
                break;
            case "spawn3":
                if (b.length >= 3) return String.format("spawn id=$%02X resumePC=$%02X%02X (bank fixed)",
                        b[0], b[1], b[2]);
                break;
            default: break;
        }
        StringBuilder sb = new StringBuilder("inline:");
        for (int x : b) sb.append(String.format(" %02X", x));
        return sb.toString();
    }

    // ── follow called helpers (so bodies decompile instead of showing UNK_xxxx stubs) ──
    // Direct JSR/BSR targets are always same-page or resident (cross-page goes via the
    // trampoline ABI), so any call target landing in an executable block is safe to follow.
    void collectCalledHelpers(Set<Integer> acc, int depth) {
        Memory mem = currentProgram.getMemory();
        Listing listing = currentProgram.getListing();
        Set<Integer> seen = new HashSet<>(acc);
        ArrayDeque<int[]> q = new ArrayDeque<>(); // {addr, depthRemaining}
        for (int a : new ArrayList<>(acc)) q.add(new int[]{a, depth});
        while (!q.isEmpty()) {
            int[] cur = q.poll();
            int off = cur[0], d = cur[1];
            if (d <= 0) continue;
            Function f = getFunctionContaining(a(off));
            Address start = (f != null) ? f.getEntryPoint() : a(off);
            // walk the instructions reachable from this entry, gathering call targets
            InstructionIterator it = listing.getInstructions(start, true);
            Address bodyEnd = (f != null) ? f.getBody().getMaxAddress() : null;
            while (it.hasNext()) {
                Instruction ins = it.next();
                if (bodyEnd != null && ins.getAddress().compareTo(bodyEnd) > 0) break;
                if (!ins.getFlowType().isCall()) continue;
                if (trampFor(ins) != null) continue; // never recurse into the ABI trampolines
                Integer to = callTargetOffset(ins);
                if (to == null) continue;
                MemoryBlock blk = mem.getBlock(a(to));
                if (blk == null || !blk.isExecute() || !blk.isInitialized()) continue;
                if (seen.add(to)) { acc.add(to); q.add(new int[]{to, d - 1}); }
            }
        }
    }
}
