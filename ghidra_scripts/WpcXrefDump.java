// Diagnostic: dump all xrefs to a fixed list of addresses, with surrounding
// instruction context. Useful for chasing a specific RAM cell or ROM byte
// across the whole program when you already know the address you care about
// and want every reader/writer in one place.
// @category    WPC
// @description Edit TARGETS below to choose what to dump. For each target,
//              prints every inbound xref (from any space, default + overlays)
//              with CTX_BEFORE/CTX_AFTER instructions of context around the
//              referencing site, plus the enclosing function name.
//
//              Currently configured for the Congo version-display chase:
//                - RAM $03ED, $03EF, $03F5, $03F6, $0401, $0405 (the structure
//                  the version display script $C0CB operates on via LDU)
//                - The format string at $4E6D in page $1C (REV. %XA.%OXB%MY)
//
//              An address is checked in *every* memory space (default + each
//              ROM_PAGE_XX overlay) so banked code references hit even when
//              the offset is in $4000-$7FFF.

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;

public class WpcXrefDump extends GhidraScript {

    private static final int CTX_BEFORE = 4;
    private static final int CTX_AFTER  = 2;

    // Targets to dump xrefs for. Offsets are 16-bit; the script probes every
    // memory space (default + each ROM_PAGE_XX overlay) at each offset.
    private static final int[] TARGETS = {
        // Version-display structure base + observed offsets:
        0x03ED, 0x03EF, 0x03F5, 0x03F6, 0x0401, 0x0405,
        // Format string "REV. %XA.%OXB%MY" address (banked, page $1C):
        0x4E6D
    };

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        Memory mem = currentProgram.getMemory();
        ReferenceManager refMgr = currentProgram.getReferenceManager();
        FunctionManager fm = currentProgram.getFunctionManager();

        // Collect every space we should probe.
        List<AddressSpace> spaces = new ArrayList<>();
        AddressSpace def = currentProgram.getAddressFactory().getDefaultAddressSpace();
        spaces.add(def);
        for (MemoryBlock b : mem.getBlocks()) {
            if (!b.getName().startsWith("ROM_PAGE_")) continue;
            AddressSpace s = b.getStart().getAddressSpace();
            if (!spaces.contains(s)) spaces.add(s);
        }
        println("WpcXrefDump: probing " + TARGETS.length
                + " targets across " + spaces.size() + " address spaces");
        println("");

        for (int tgt : TARGETS) {
            if (monitor.isCancelled()) return;

            println("=================================================================");
            println(String.format("TARGET $%04X", tgt));

            // Dedupe by FROM address globally — RAM in particular is mirrored
            // into every ROM_PAGE_XX overlay, so without dedup the same xref
            // gets printed once per overlay space.
            Set<Address> shown = new HashSet<>();
            int totalUnique = 0;
            for (AddressSpace sp : spaces) {
                Address a;
                try { a = sp.getAddress(tgt); }
                catch (Exception e) { continue; }

                for (Reference r : refMgr.getReferencesTo(a)) {
                    Address from = r.getFromAddress();
                    if (!shown.add(from)) continue;
                    totalUnique++;
                    Function f = fm.getFunctionContaining(from);
                    String fname = (f == null) ? "<no_fn>" : f.getName();
                    println("  --- from " + from + "  [" + r.getReferenceType()
                            + "]  fn=" + fname + " ---");
                    printContext(listing, mem, from, CTX_BEFORE, CTX_AFTER);
                }
            }
            if (totalUnique == 0) println("  (no xrefs in any space)");
            else println("  (total unique xrefs: " + totalUnique + ")");
            println("");
        }
    }

    private void printContext(Listing listing, Memory mem, Address site,
                              int before, int after) {
        Instruction cur = listing.getInstructionAt(site);
        if (cur == null) {
            println("    (no instruction at " + site + " — referenced as data only)");
            return;
        }
        List<Instruction> prevs = new ArrayList<>();
        Instruction back = cur;
        for (int i = 0; i < before; i++) {
            back = back.getPrevious();
            if (back == null) break;
            prevs.add(0, back);
        }
        for (Instruction in : prevs) println(formatInstr(in, mem));
        println(formatInstr(cur, mem) + "    <<<");
        Instruction nx = cur;
        for (int i = 0; i < after; i++) {
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
