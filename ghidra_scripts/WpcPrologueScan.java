// Function-prologue scan for code-dense WPC banked pages.
// @category    WPC
// @description Find more entry points by scanning code-dense overlays for the
//              canonical 6809 function prologue: PSHS <reglist> (opcode 0x34,
//              non-zero reglist). Skip pages with few existing functions (those
//              are likely pure data). Disassemble + create function at each
//              candidate. False positives are filtered out later by
//              DecompileAllScript's halt_baddata filter.

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.CodeUnit;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;

public class WpcPrologueScan extends GhidraScript {

    // Minimum existing-function count for an overlay to be considered worth
    // scanning. Below this, the page is almost certainly pure data and
    // probing PSHS bytes would only generate noise.
    private static final int MIN_FUNCS_FOR_SCAN = 5;

    @Override
    public void run() throws Exception {
        Memory mem = currentProgram.getMemory();
        Listing listing = currentProgram.getListing();
        FunctionManager fm = currentProgram.getFunctionManager();

        List<MemoryBlock> overlays = new ArrayList<>();
        for (MemoryBlock blk : mem.getBlocks()) {
            if (blk.getName().startsWith("ROM_PAGE_")) overlays.add(blk);
        }

        int totalCreated = 0;
        int totalSkippedPages = 0;
        int totalCandidates = 0;

        for (MemoryBlock blk : overlays) {
            if (monitor.isCancelled()) break;
            String name = blk.getName();

            int existing = 0;
            AddressSet blkRange = new AddressSet(blk.getStart(), blk.getEnd());
            Iterator<Function> it = fm.getFunctions(blkRange, true);
            while (it.hasNext()) { it.next(); existing++; if (existing >= MIN_FUNCS_FOR_SCAN) break; }
            if (existing < MIN_FUNCS_FOR_SCAN) {
                totalSkippedPages++;
                continue;
            }

            int candidates = 0;
            int created = 0;
            Address a = blk.getStart();
            Address end = blk.getEnd();

            while (a.compareTo(end) < 0) {
                if (monitor.isCancelled()) break;

                // Skip past any address that's already part of an instruction.
                CodeUnit cu = listing.getCodeUnitContaining(a);
                if (cu instanceof Instruction) {
                    Address cuEnd = cu.getMaxAddress();
                    a = safeAdd(cuEnd, 1, end);
                    if (a == null) break;
                    continue;
                }

                int b0;
                int b1;
                try {
                    b0 = mem.getByte(a) & 0xFF;
                    b1 = mem.getByte(a.add(1)) & 0xFF;
                } catch (Exception e) {
                    a = safeAdd(a, 1, end);
                    if (a == null) break;
                    continue;
                }

                // Candidate: PSHS reg_list with non-trivial reg list.
                // Skip 0x00 (empty push -> rarely a real entry) and 0xFF
                // (push-everything -> often coincidence in data).
                boolean candidate = (b0 == 0x34) && (b1 != 0x00) && (b1 != 0xFF);
                if (!candidate) {
                    a = safeAdd(a, 1, end);
                    if (a == null) break;
                    continue;
                }

                candidates++;
                disassemble(a);
                Instruction insn = listing.getInstructionAt(a);
                if (insn == null) {
                    a = safeAdd(a, 1, end);
                    if (a == null) break;
                    continue;
                }

                if (fm.getFunctionAt(a) == null) {
                    try {
                        String pageHex = name.substring("ROM_PAGE_".length());
                        String fname = String.format("FUN_%s_%04X",
                                pageHex, (int)(a.getOffset() & 0xFFFF));
                        createFunction(a, fname);
                        created++;
                    } catch (Exception e) {
                        // ignore
                    }
                }
                a = safeAdd(a, insn.getLength(), end);
                if (a == null) break;
            }

            totalCandidates += candidates;
            totalCreated += created;
            println(String.format("PrologueScan %s: candidates=%d created=%d",
                    name, candidates, created));
        }

        println(String.format(
            "WpcPrologueScan done: scanned_pages=%d skipped_data_pages=%d candidates=%d functions_created=%d",
            overlays.size() - totalSkippedPages, totalSkippedPages,
            totalCandidates, totalCreated));
    }

    /** Safely advance an address by delta; return null if we'd cross past end. */
    private static Address safeAdd(Address a, int delta, Address end) {
        try {
            Address next = a.add(delta);
            if (next.compareTo(end) > 0) return null;
            return next;
        } catch (Exception e) {
            return null;
        }
    }
}
