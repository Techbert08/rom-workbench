// Resolve cross-bank calls that go through the FUN_90c4 bank-switch thunk.
// @category    WPC
// @description Congo's main bank-switch thunk lives at $90C4 — it copies B
//              into BANK_SHADOW ($11) and the bank register ($3FFC), then
//              returns. Call sites look like:
//
//                  LDB #<new_bank>     ; or LDB <fixed_rom_addr>
//                  JSR $90C4           ; switch bank
//                  JSR $4xxx           ; call into new bank
//
//              We find every JSR/BSR to $90C4, walk back for the LDB to recover
//              the bank constant (literal or static-ROM-resident byte), walk
//              forward for the JSR/JMP $4xxx that follows, and add a cross-bank
//              reference + function in the appropriate ROM_PAGE_XX overlay.

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSpace;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.RefType;
import ghidra.program.model.symbol.SourceType;

public class WpcThunkResolve extends GhidraScript {

    private static final long THUNK_90C4   = 0x90C4L;
    private static final long SYSTEM_LO    = 0x8000L;
    private static final long SYSTEM_HI    = 0xFFFFL;
    private static final long BANK_LO      = 0x4000L;
    private static final long BANK_HI      = 0x7FFFL;
    private static final int  MAX_LOOKBACK  = 5;
    private static final int  MAX_LOOKAHEAD = 3;

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        Memory mem = currentProgram.getMemory();
        ReferenceManager refMgr = currentProgram.getReferenceManager();
        FunctionManager fm = currentProgram.getFunctionManager();

        AddressSpace defSpace = currentProgram.getAddressFactory().getDefaultAddressSpace();
        Address thunkAddr = defSpace.getAddress(THUNK_90C4);

        // Flush pending analysis so that JSRs in newly-disassembled banked code
        // are visible via getReferencesTo() before we walk the call sites.
        analyzeChanges(currentProgram);

        int callers = 0;
        int bankResolved = 0;
        int targetResolved = 0;
        int newFns = 0;

        // Enumerate every reference INTO the thunk via the reference manager,
        // then look up the calling instruction for each. (Operand-scanning via
        // instruction iteration misses most call sites because Ghidra represents
        // many cross-space JSRs through references only, not via operand Address
        // objects.)
        for (Reference r : refMgr.getReferencesTo(thunkAddr)) {
            if (monitor.isCancelled()) break;
            RefType rt = r.getReferenceType();
            if (!rt.isCall() && !rt.isJump()) continue;
            Address callSite = r.getFromAddress();
            Instruction callIn = listing.getInstructionAt(callSite);
            if (callIn == null) continue;
            callers++;

            Integer bank = lookBackForBank(callIn, mem);
            if (bank == null) continue;
            if (bank < 0 || bank > 0x3D) continue;
            bankResolved++;

            Long targetOff = lookAheadForJsr(callIn);
            if (targetOff == null) continue;
            targetResolved++;

            // Find the matching overlay.
            String overlayName = String.format("ROM_PAGE_%02X", bank);
            MemoryBlock ov = mem.getBlock(overlayName);
            if (ov == null) continue;

            AddressSpace ovSpace = ov.getStart().getAddressSpace();
            Address realTarget = ovSpace.getAddress(targetOff);

            // The JSR instruction that follows the thunk call.
            Instruction jsr = callIn.getNext();
            for (int i = 0; jsr != null && i < MAX_LOOKAHEAD; i++) {
                String jmn = jsr.getMnemonicString();
                if (jmn.equals("JSR") || jmn.equals("BSR")
                        || jmn.equals("JMP") || jmn.equals("BRA")) break;
                jsr = jsr.getNext();
            }
            if (jsr == null) continue;

            // Has this exact xref already been added?
            boolean already = false;
            for (Reference existing : refMgr.getReferencesFrom(jsr.getAddress())) {
                if (existing.getToAddress().equals(realTarget)) { already = true; break; }
            }
            if (already) continue;

            boolean isCall = "JSR".equals(jsr.getMnemonicString())
                          || "BSR".equals(jsr.getMnemonicString());
            refMgr.addMemoryReference(jsr.getAddress(), realTarget,
                    isCall ? RefType.UNCONDITIONAL_CALL : RefType.UNCONDITIONAL_JUMP,
                    SourceType.ANALYSIS, 0);

            if (listing.getInstructionAt(realTarget) == null) disassemble(realTarget);
            if (fm.getFunctionAt(realTarget) == null) {
                try {
                    String fn = String.format("FUN_%02X_%04X", bank, targetOff.intValue());
                    createFunction(realTarget, fn);
                    newFns++;
                } catch (Exception e) {
                    // ignore
                }
            }
        }

        println(String.format(
            "WpcThunkResolve done: callers=%d bank_resolved=%d target_resolved=%d new_functions=%d",
            callers, bankResolved, targetResolved, newFns));
    }

    /** Walk back up to MAX_LOOKBACK instructions for an LDB that defines the bank. */
    private Integer lookBackForBank(Instruction call, Memory mem) {
        Instruction p = call;
        for (int i = 0; i < MAX_LOOKBACK; i++) {
            p = p.getPrevious();
            if (p == null) return null;
            String mn = p.getMnemonicString();
            if (!"LDB".equals(mn)) continue;
            Object[] ops = p.getOpObjects(0);
            if (ops.length != 1) return null;

            if (ops[0] instanceof Scalar) {
                long v = ((Scalar) ops[0]).getUnsignedValue();
                if (v < 0 || v > 0xFF) return null;
                return (int) v;
            }
            if (ops[0] instanceof Address) {
                Address src = (Address) ops[0];
                long off = src.getOffset();
                // Only resolve if the source is in the FIXED system ROM
                // ($8000-$FFFF in the default space); banked/data ROM is
                // not statically known.
                if (off >= SYSTEM_LO && off <= SYSTEM_HI
                        && !src.getAddressSpace().isOverlaySpace()) {
                    try {
                        return mem.getByte(src) & 0xFF;
                    } catch (Exception e) {
                        return null;
                    }
                }
                return null;
            }
            return null;
        }
        return null;
    }

    /** Walk forward up to MAX_LOOKAHEAD instructions for a JSR/JMP into $4000-$7FFF. */
    private Long lookAheadForJsr(Instruction call) {
        Instruction n = call;
        for (int i = 0; i < MAX_LOOKAHEAD; i++) {
            n = n.getNext();
            if (n == null) return null;
            String mn = n.getMnemonicString();
            boolean isJsr = "JSR".equals(mn) || "BSR".equals(mn)
                         || "JMP".equals(mn) || "BRA".equals(mn);
            if (!isJsr) continue;
            if (n.getNumOperands() < 1) continue;
            Object[] ops = n.getOpObjects(0);
            if (ops.length != 1) continue;
            long tgt;
            if (ops[0] instanceof Address)      tgt = ((Address) ops[0]).getOffset();
            else if (ops[0] instanceof Scalar)  tgt = ((Scalar) ops[0]).getUnsignedValue();
            else continue;
            if (tgt < BANK_LO || tgt > BANK_HI) continue;   // wrong range; keep looking
            return tgt;
        }
        return null;
    }
}
