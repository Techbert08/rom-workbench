// Diagnostic: find every instruction whose first operand is a specific
// immediate value. Useful for chasing constants when xrefs aren't created
// (e.g. a 16-bit pointer loaded with `LDX #$4E6D`, or a magic byte loaded
// with `LDB #$3C` for bank selection).
// @category    WPC
// @description Edit TARGETS_8 / TARGETS_16 below. For each value, walks the
//              entire program listing and prints every instruction whose
//              first operand is a Scalar matching that value, with
//              CTX_BEFORE/CTX_AFTER instructions of context and the
//              enclosing function name.
//
//              Currently configured for the Congo version-display chase:
//                - 8-bit: 0x02 (A=2), 0x10 (B=0x10), 0x3C (bank of fmt str)
//                - 16-bit: $4E6D (format string addr), $C0CA (display script
//                  passed to $D9A6), $03ED (struct base loaded into U)

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.scalar.Scalar;

public class WpcImmediateScan extends GhidraScript {

    private static final int CTX_BEFORE = 4;
    private static final int CTX_AFTER  = 2;

    // 8-bit immediate values worth flagging (LDA/LDB/CMPA/CMPB/etc #imm).
    private static final int[] TARGETS_8 = { 0x02, 0x10, 0x3C, 0x22, 0x21 };

    // 16-bit immediate values (LDD/LDX/LDU/LDY/CMPD/etc #imm).
    private static final int[] TARGETS_16 = { 0x4E6D, 0xC0CA, 0x03ED };

    // Hard cap on hits printed per target — chasing 0x02 will hit a LOT of
    // instructions, and we mostly want to spot-check.
    private static final int MAX_HITS_PER_TARGET = 30;

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        Memory mem = currentProgram.getMemory();
        FunctionManager fm = currentProgram.getFunctionManager();

        // Walk the entire instruction stream once, bucketing hits per target.
        // For each instruction with a scalar operand 0, compare to every
        // target value and add to that target's bucket.
        Set<Integer> set8  = new HashSet<>();
        Set<Integer> set16 = new HashSet<>();
        for (int v : TARGETS_8)  set8.add(v & 0xFF);
        for (int v : TARGETS_16) set16.add(v & 0xFFFF);

        java.util.Map<Integer, List<Address>> hits8  = new java.util.HashMap<>();
        java.util.Map<Integer, List<Address>> hits16 = new java.util.HashMap<>();
        for (int v : TARGETS_8)  hits8.put(v & 0xFF,   new ArrayList<>());
        for (int v : TARGETS_16) hits16.put(v & 0xFFFF, new ArrayList<>());

        InstructionIterator it = listing.getInstructions(true);
        long scanned = 0;
        while (it.hasNext()) {
            if (monitor.isCancelled()) return;
            Instruction in = it.next();
            scanned++;
            if (in.getNumOperands() < 1) continue;
            // Only flag operands tagged as immediate (mnemonic ends with
            // " #..." after Ghidra formatting). Cheap check: scan all
            // operands' scalars.
            for (int opIdx = 0; opIdx < in.getNumOperands(); opIdx++) {
                Object[] objs = in.getOpObjects(opIdx);
                for (Object o : objs) {
                    if (!(o instanceof Scalar)) continue;
                    long v = ((Scalar) o).getUnsignedValue();
                    // 8-bit
                    int v8 = (int)(v & 0xFF);
                    if (v <= 0xFF && set8.contains(v8)) {
                        hits8.get(v8).add(in.getAddress());
                    }
                    // 16-bit (only when value uses the high byte too, to avoid
                    // showering 0x02 etc onto every $02xx address)
                    if (v > 0xFF) {
                        int v16 = (int)(v & 0xFFFF);
                        if (set16.contains(v16)) {
                            hits16.get(v16).add(in.getAddress());
                        }
                    }
                }
            }
        }
        println("WpcImmediateScan: scanned " + scanned + " instructions");
        println("");

        // 16-bit targets first (more specific, smaller hit lists)
        for (int v : TARGETS_16) {
            if (monitor.isCancelled()) return;
            List<Address> hs = hits16.get(v & 0xFFFF);
            println("=================================================================");
            println(String.format("16-bit TARGET $%04X : %d hit(s)", v, hs.size()));
            int shown = 0;
            for (Address a : hs) {
                if (shown++ >= MAX_HITS_PER_TARGET) {
                    println("  ... " + (hs.size() - MAX_HITS_PER_TARGET) + " more truncated ...");
                    break;
                }
                printHit(listing, mem, fm, a);
            }
            println("");
        }

        // 8-bit targets
        for (int v : TARGETS_8) {
            if (monitor.isCancelled()) return;
            List<Address> hs = hits8.get(v & 0xFF);
            println("=================================================================");
            println(String.format("8-bit TARGET $%02X : %d hit(s)", v, hs.size()));
            int shown = 0;
            for (Address a : hs) {
                if (shown++ >= MAX_HITS_PER_TARGET) {
                    println("  ... " + (hs.size() - MAX_HITS_PER_TARGET) + " more truncated ...");
                    break;
                }
                printHit(listing, mem, fm, a);
            }
            println("");
        }
    }

    private void printHit(Listing listing, Memory mem, FunctionManager fm, Address a) {
        Instruction in = listing.getInstructionAt(a);
        if (in == null) return;
        Function f = fm.getFunctionContaining(a);
        String fname = (f == null) ? "<no_fn>" : f.getName();
        println("  --- " + a + "  fn=" + fname + " ---");
        // CTX_BEFORE..CTX_AFTER around this hit
        List<Instruction> prevs = new ArrayList<>();
        Instruction back = in;
        for (int i = 0; i < CTX_BEFORE; i++) {
            back = back.getPrevious();
            if (back == null) break;
            prevs.add(0, back);
        }
        for (Instruction p : prevs) println(formatInstr(p, mem));
        println(formatInstr(in, mem) + "    <<<");
        Instruction nx = in;
        for (int i = 0; i < CTX_AFTER; i++) {
            nx = nx.getNext();
            if (nx == null) break;
            println(formatInstr(nx, mem));
        }
    }

    private String formatInstr(Instruction in, Memory mem) {
        StringBuilder sb = new StringBuilder("      ");
        sb.append(in.getAddress().toString());
        sb.append("  ");
        StringBuilder bytes = new StringBuilder();
        int len = in.getLength();
        for (int i = 0; i < len && i < 5; i++) {
            try {
                bytes.append(String.format("%02X ",
                    mem.getByte(in.getAddress().add(i)) & 0xFF));
            } catch (Exception e) {
                bytes.append("?? ");
            }
        }
        while (bytes.length() < 15) bytes.append(' ');
        sb.append(bytes);
        sb.append("  ");
        sb.append(in.toString());
        return sb.toString();
    }
}
