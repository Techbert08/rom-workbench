// Force-disassemble WPC display "scripts" passed inline to display utilities.
// @category    WPC
// @description The WPC OS display utility at $D9A6 uses an "inline-parameter"
//              protocol: after a JSR $D9A6, the next 2 bytes form a 16-bit
//              pointer to a display script body, followed by additional
//              script-defined argument bytes. The runtime adjusts the return
//              PC past those args, so the bytes look like data to Ghidra and
//              the script bodies never get disassembled.
//
//              Empirically (Congo, 2026-05-28) the Congo "version display"
//              script at $C0CA starts with byte 0x05 — an illegal 6809
//              opcode that's likely a script-type tag — followed by valid
//              6809 starting at $C0CB (LDU #$03ED, BSR +$20, PULS PC,B,A,
//              and two parallel LDA $0401 / LDA $03F5 routines that
//              ultimately populate the registers consumed by the
//              format-string parser).
//
//              This script:
//                1. Scans every initialised ROM block for the pattern
//                   `BD D9 A6` (JSR $D9A6).
//                2. For each hit, reads the next 2 bytes as a candidate
//                   script address.
//                3. Restricts to system-ROM targets ($8000-$FFFF). RAM and
//                   banked-page targets are out of scope here.
//                4. Force-disassembles at target. If the byte at target is
//                   in the standard-6809 illegal-opcode set, falls back to
//                   target+1 (handles the leading-type-byte case).
//                5. Creates a function entry there.
//
//              Run AFTER WpcThunkResolve and BEFORE DecompileAllScript so
//              the newly-disassembled scripts and their callees end up in
//              the decompile output.

import java.util.HashSet;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;

public class WpcDisplayScripts extends GhidraScript {

    private static final int  JSR_EXTENDED = 0xBD;
    private static final long SYSTEM_LO    = 0x8000L;
    private static final long SYSTEM_HI    = 0xFFFFL;

    // Known WPC OS display-utility entry points. Add more as identified.
    private static final int[] DISPLAY_ENTRIES = { 0xD9A6 };

    // Standard 6809 illegal/undefined opcodes. When seen as the first byte of
    // a candidate script body, fall back to body+1 (the byte was likely a
    // script-type tag the inline-protocol interpreter consumes itself).
    // Note: 0x10 and 0x11 (page-2 / page-3 prefixes) are LEGAL — they extend
    // the opcode to 2 bytes — and are NOT included here.
    private static final Set<Integer> ILLEGAL_6809 = new HashSet<>();
    static {
        int[] ill = {
            0x01, 0x02, 0x05, 0x0B, 0x14, 0x15, 0x18, 0x1B, 0x38,
            0x41, 0x42, 0x45, 0x4B, 0x4E, 0x51, 0x52, 0x55, 0x5B, 0x5E,
            0x61, 0x62, 0x65, 0x6B, 0x71, 0x72, 0x75, 0x7B,
            0x87, 0x8F, 0xC7, 0xCD, 0xCF
        };
        for (int op : ill) ILLEGAL_6809.add(op);
    }

    @Override
    public void run() throws Exception {
        Memory mem = currentProgram.getMemory();
        Listing listing = currentProgram.getListing();
        FunctionManager fm = currentProgram.getFunctionManager();
        AddressSpace defSpace = currentProgram.getAddressFactory().getDefaultAddressSpace();

        // Scan system ROM (default space, $8000-$FFFF) plus every ROM_PAGE_XX
        // overlay — JSR $D9A6 calls can live in either.
        java.util.List<MemoryBlock> blocks = new java.util.ArrayList<>();
        for (MemoryBlock b : mem.getBlocks()) {
            if (!b.isInitialized()) continue;
            String n = b.getName();
            boolean isBanked = n.startsWith("ROM_PAGE_");
            boolean isSystem = !b.getStart().getAddressSpace().isOverlaySpace()
                               && b.getStart().getOffset() >= SYSTEM_LO;
            if (isBanked || isSystem) blocks.add(b);
        }
        println("WpcDisplayScripts: scanning " + blocks.size()
                + " ROM blocks for JSR <display-utility>");

        Set<Long> seen = new HashSet<>();
        int hits = 0, disassembled = 0, functions = 0, skippedIllegal = 0;

        for (MemoryBlock blk : blocks) {
            if (monitor.isCancelled()) break;
            AddressSpace blkSpace = blk.getStart().getAddressSpace();
            long lo = blk.getStart().getOffset();
            long hi = blk.getEnd().getOffset();

            for (long off = lo; off <= hi - 4; off++) {
                if (monitor.isCancelled()) break;

                int b0;
                try { b0 = mem.getByte(blkSpace.getAddress(off)) & 0xFF; }
                catch (Exception e) { continue; }
                if (b0 != JSR_EXTENDED) continue;

                int eHi, eLo;
                try {
                    eHi = mem.getByte(blkSpace.getAddress(off + 1)) & 0xFF;
                    eLo = mem.getByte(blkSpace.getAddress(off + 2)) & 0xFF;
                } catch (Exception e) { continue; }
                int entry = (eHi << 8) | eLo;

                boolean isDisplayEntry = false;
                for (int de : DISPLAY_ENTRIES) {
                    if (entry == de) { isDisplayEntry = true; break; }
                }
                if (!isDisplayEntry) continue;

                int tHi, tLo;
                try {
                    tHi = mem.getByte(blkSpace.getAddress(off + 3)) & 0xFF;
                    tLo = mem.getByte(blkSpace.getAddress(off + 4)) & 0xFF;
                } catch (Exception e) { continue; }
                int target = (tHi << 8) | tLo;
                hits++;

                // Only handle system-ROM targets here.
                if (target < SYSTEM_LO || target > SYSTEM_HI) continue;
                if (seen.contains((long) target)) continue;

                // Two-shot: try target, fall back to target+1 if first byte
                // is illegal-6809 (likely a script-type tag the interpreter
                // consumes before handing control to the body).
                int[] tryAddrs = { target, target + 1 };
                boolean placed = false;
                for (int ta : tryAddrs) {
                    if (ta > SYSTEM_HI) break;
                    Address taAddr;
                    try { taAddr = defSpace.getAddress(ta); }
                    catch (Exception e) { continue; }

                    int firstByte;
                    try { firstByte = mem.getByte(taAddr) & 0xFF; }
                    catch (Exception e) { continue; }
                    if (ILLEGAL_6809.contains(firstByte)) {
                        if (ta == target) { skippedIllegal++; continue; }  // try +1
                        break;                                              // give up
                    }

                    if (listing.getInstructionAt(taAddr) == null) {
                        disassemble(taAddr);
                        if (listing.getInstructionAt(taAddr) != null) disassembled++;
                    }
                    if (listing.getInstructionAt(taAddr) != null
                            && fm.getFunctionAt(taAddr) == null) {
                        try {
                            String name = String.format("FUN_displayscript_%04X", ta);
                            createFunction(taAddr, name);
                            functions++;
                        } catch (Exception e) {
                            // Function creation can fail if disassembly didn't
                            // produce a clean entry. Leave it.
                        }
                    }
                    seen.add((long) target);
                    placed = true;
                    break;
                }
                if (!placed) {
                    // Both target and target+1 were illegal — leave for manual review.
                }
            }
        }

        println(String.format(
            "WpcDisplayScripts done: hits=%d unique_targets=%d disassembled=%d functions=%d (skipped_illegal_first_byte=%d)",
            hits, seen.size(), disassembled, functions, skippedIllegal));
    }
}
