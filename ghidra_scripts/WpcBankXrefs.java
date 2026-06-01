// Discover code in WPC banked ROM pages by harvesting dangling references.
// @category    WPC
// @description When the 6809 disassembler encounters JSR/JMP/LDA into the bank
//              window ($4000-$7FFF) it adds a reference to the DEFAULT address
//              space at that offset. No block lives there (banked code is in
//              ROM_PAGE_XX overlays), so the references dangle. Each one is
//              evidence that "some bank's code at offset O is reached". We
//              collect every such offset and probe (offset, page) across every
//              ROM_PAGE_XX overlay: disassemble + createFunction. Iterates to a
//              fixpoint as newly-disassembled banked code adds more references.

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.RefType;

public class WpcBankXrefs extends GhidraScript {

    private static final long BANK_LO = 0x4000L;
    private static final long BANK_HI = 0x7FFFL;
    private static final int MAX_PASSES = 4;

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        Memory mem = currentProgram.getMemory();
        ReferenceManager refMgr = currentProgram.getReferenceManager();

        // Snapshot the ROM_PAGE_XX overlays once.
        List<MemoryBlock> overlays = new ArrayList<>();
        for (MemoryBlock blk : mem.getBlocks()) {
            if (blk.getName().startsWith("ROM_PAGE_")) overlays.add(blk);
        }
        println("Found " + overlays.size() + " banked-ROM overlays");

        AddressSpace defaultSpace =
            currentProgram.getAddressFactory().getDefaultAddressSpace();
        AddressSet bankWindow = new AddressSet(
            defaultSpace.getAddress(BANK_LO),
            defaultSpace.getAddress(BANK_HI));

        Set<Long> probedOffsets = new HashSet<>();
        int totalProbes = 0;
        int totalDisasm = 0;
        int totalFuncs = 0;

        for (int pass = 1; pass <= MAX_PASSES; pass++) {
            if (monitor.isCancelled()) break;

            // Collect every default-space destination in $4000-$7FFF.
            // We treat code refs (call/jump) and data refs (LDA/LDU/etc.) as
            // both useful: a data ref often points at a function-pointer table
            // entry in the bank window, whose 16-bit value is another entry.
            Set<Long> codeOffsets = new HashSet<>();
            Set<Long> dataOffsets = new HashSet<>();
            AddressIterator destIter =
                refMgr.getReferenceDestinationIterator(bankWindow, true);
            while (destIter.hasNext()) {
                if (monitor.isCancelled()) break;
                Address dest = destIter.next();
                long off = dest.getOffset();
                for (Reference ref : refMgr.getReferencesTo(dest)) {
                    RefType rt = ref.getReferenceType();
                    if (rt.isCall() || rt.isJump()) {
                        codeOffsets.add(off);
                        break;
                    }
                    if (rt.isData() || rt.isRead() || rt.isWrite()) {
                        dataOffsets.add(off);
                    }
                }
            }

            // Pointer-chase: for each data-ref offset, read the 16-bit value at
            // (overlay, offset). If it points into $4000-$7FFF, treat it as a
            // candidate code entry too (typical for jump tables and function
            // pointer arrays packed into banked pages).
            Set<Long> chased = new HashSet<>();
            for (Long off : dataOffsets) {
                if (monitor.isCancelled()) break;
                for (MemoryBlock ov : overlays) {
                    try {
                        AddressSpace sp = ov.getStart().getAddressSpace();
                        Address a = sp.getAddress(off);
                        // Read big-endian 16-bit value (6809 byte order).
                        int hi = mem.getByte(a) & 0xFF;
                        int lo = mem.getByte(a.add(1)) & 0xFF;
                        int val = (hi << 8) | lo;
                        if (val >= BANK_LO && val <= BANK_HI) {
                            chased.add((long) val);
                        }
                    } catch (Exception e) {
                        // unreadable bytes — skip
                    }
                }
            }
            int chasedNew = 0;
            for (Long o : chased) {
                if (codeOffsets.add(o)) chasedNew++;
            }
            println(String.format(
                "  data-ref offsets: %d ; pointer-chased %d candidate code targets (%d new)",
                dataOffsets.size(), chased.size(), chasedNew));

            int newOffsets = 0;
            for (Long o : codeOffsets) if (!probedOffsets.contains(o)) newOffsets++;
            println(String.format(
                "Pass %d: %d distinct code-ref offsets in $4000-$7FFF (%d new)",
                pass, codeOffsets.size(), newOffsets));

            if (newOffsets == 0) break;

            int probed = 0, disasm = 0, funcs = 0;
            for (Long off : codeOffsets) {
                if (monitor.isCancelled()) break;
                if (probedOffsets.contains(off)) continue;
                probedOffsets.add(off);

                for (MemoryBlock ov : overlays) {
                    if (monitor.isCancelled()) break;
                    AddressSpace ovSpace = ov.getStart().getAddressSpace();
                    Address target = ovSpace.getAddress(off);
                    probed++;

                    boolean hadInsn = listing.getInstructionAt(target) != null;
                    if (!hadInsn) {
                        disassemble(target);
                        if (listing.getInstructionAt(target) != null) disasm++;
                    }
                    if (listing.getInstructionAt(target) != null
                            && currentProgram.getFunctionManager()
                                .getFunctionAt(target) == null) {
                        try {
                            String pageHex = ov.getName().substring("ROM_PAGE_".length());
                            String name = String.format("FUN_%s_%04X", pageHex, off.intValue());
                            createFunction(target, name);
                            funcs++;
                        } catch (Exception e) {
                            // Function creation can fail if the disassembly
                            // didn't produce a clean entry. Leave it.
                        }
                    }
                }
            }
            println(String.format("  probed=%d new_disasm=%d new_functions=%d",
                                  probed, disasm, funcs));

            totalProbes += probed;
            totalDisasm += disasm;
            totalFuncs += funcs;
        }

        println(String.format(
            "WpcBankXrefs done: probes=%d disassembled=%d functions=%d offsets=%d",
            totalProbes, totalDisasm, totalFuncs, probedOffsets.size()));
    }
}
