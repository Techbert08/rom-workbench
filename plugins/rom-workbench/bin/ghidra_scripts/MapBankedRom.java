// MapBankedRom.java — generic Ghidra headless post-script for banked 6809 pinball ROMs
// (Williams WPC / Stern Whitestar). The stock raw-binary loader maps a flat image and
// clips a >64KB ROM; this rebuilds the real 6809 memory map (RAM + one banked page in
// the banked window + the fixed resident bank), pins the direct-page register, labels
// known RAM, then recursive-descent disassembles + DECOMPILES the requested addresses to
// <outDir>/<ADDR>.c. Recursive descent follows control flow, so it survives the inline-
// data spots where rom.py's linear sweep (and dispatch-driven 0-xref tasks) misalign.
//
// Takes ONE positional arg: the path to a line-based config file. (A single path is used
// instead of many args because cmd/analyzeHeadless.bat re-split on space/comma/semicolon/
// equals, which mangles list args on Windows.) Config keys (repeatable ones noted):
//
//   rom=<absolute path to the CPU ROM image on disk>      (the .aNN/.bin file, NOT the zip)
//   out=<dir for the decompiled <ADDR>.c files>           (created if absent)
//   dp=<hex>            direct-page value (0 typical); omit / "none" to skip pinning DP
//   block=name:cpuHex:fileHex:sizeHex     [repeatable]   fileHex="none" => uninitialized RAM
//   label=cpuHex:name                     [repeatable]   makes the C read `name` not DAT_xxxx
//   target=hexAddr                        [repeatable]   each: recursive disasm + decompile
//
// '#' lines and blanks are ignored. Geometry comes from the CALLER (it knows the platform):
//   Whitestar: banked window CPU $4000-$7FFF <- file page*0x4000; resident $8000-$FFFF <- file 0x18000.
//   WPC:       banked window CPU $4000-$7FFF; system/resident in the upper bank — derive per ROM.

import java.io.ByteArrayInputStream;
import java.io.PrintWriter;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;

public class MapBankedRom extends GhidraScript {

    AddressSpace space;
    Address a(int off) { return space.getAddress(off & 0xFFFF); }
    static int h(String s) { return Integer.parseInt(s.replaceFirst("(?i)^0x", ""), 16); }

    @Override
    public void run() throws Exception {
        space = currentProgram.getAddressFactory().getDefaultAddressSpace();
        Memory mem = currentProgram.getMemory();
        String[] args = getScriptArgs();
        if (args.length < 1) { println("usage: MapBankedRom <configFile>"); return; }

        String rom = null, out = null, dp = "none";
        List<String> blocks = new ArrayList<>(), labels = new ArrayList<>(), targets = new ArrayList<>();
        for (String raw : Files.readAllLines(Paths.get(args[0]))) {
            String line = raw.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            int eq = line.indexOf('=');
            if (eq < 0) continue;
            String k = line.substring(0, eq).trim(), v = line.substring(eq + 1).trim();
            switch (k) {
                case "rom":    rom = v; break;
                case "out":    out = v; break;
                case "dp":     dp = v; break;
                case "block":  blocks.add(v); break;
                case "label":  labels.add(v); break;
                case "target": targets.add(v); break;
                default:       println("ignoring unknown key: " + k);
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

        // --- disassemble + decompile each target to <out>/<ADDR>.c ---
        Files.createDirectories(Paths.get(out));
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        for (String t : targets) {
            int off = h(t);
            Address addr = a(off);
            new DisassembleCommand(addr, null, true).applyTo(currentProgram, monitor);
            new CreateFunctionCmd(addr).applyTo(currentProgram, monitor);
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
}
