// Debug: dump every STA/STB $3FFC site and the 4 instructions preceding it.
// @category    WPC

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Listing;

public class WpcBankDebug extends GhidraScript {

    private static final long BANK_REG = 0x3FFCL;

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        InstructionIterator iter = listing.getInstructions(true);
        int count = 0;

        while (iter.hasNext()) {
            Instruction sta = iter.next();
            String mnem = sta.getMnemonicString();
            if (!"STA".equals(mnem) && !"STB".equals(mnem)) continue;
            if (sta.getNumOperands() < 1) continue;
            Object[] ops = sta.getOpObjects(0);
            if (ops.length != 1 || !(ops[0] instanceof Address)) continue;
            if (((Address) ops[0]).getOffset() != BANK_REG) continue;

            count++;
            println("== site #" + count + " ==");
            // Walk back 4 instructions
            Instruction[] context = new Instruction[5];
            context[4] = sta;
            Instruction cur = sta;
            for (int k = 3; k >= 0; k--) {
                cur = (cur == null) ? null : cur.getPrevious();
                context[k] = cur;
            }
            // Also look at the NEXT instruction
            Instruction nxt = sta.getNext();
            for (Instruction c : context) {
                if (c == null) {
                    println("  (no prev)");
                } else {
                    println(String.format("  %s  %s", c.getAddress(), c.toString()));
                }
            }
            println(String.format("  -> next: %s",
                    nxt == null ? "(none)" : (nxt.getAddress() + "  " + nxt.toString())));
        }
        println("Total STA/STB $3FFC sites: " + count);
    }
}
