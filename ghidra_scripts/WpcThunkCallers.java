// Discovery report: for every function containing a STA/STB $3FFC instruction,
// enumerate its inbound xrefs and print enough context around each call site
// to figure out the bank/target calling convention by inspection.
// @category    WPC

import java.util.ArrayList;
import java.util.Collections;
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
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.program.model.symbol.RefType;

public class WpcThunkCallers extends GhidraScript {

    private static final long BANK_REG = 0x3FFCL;
    private static final int  CTX_BEFORE = 4;   // instructions to print before each call
    private static final int  CTX_AFTER  = 2;   // instructions to print after each call

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        Memory mem = currentProgram.getMemory();
        ReferenceManager refMgr = currentProgram.getReferenceManager();
        FunctionManager fm = currentProgram.getFunctionManager();

        // 1) Find every function whose body contains an STA/STB $3FFC.
        //    Map them to their entry-point addresses.
        Set<Address> thunkEntries = new HashSet<>();
        List<Address> staSites = new ArrayList<>();
        InstructionIterator iter = listing.getInstructions(true);
        while (iter.hasNext()) {
            if (monitor.isCancelled()) return;
            Instruction in = iter.next();
            String mn = in.getMnemonicString();
            if (!"STA".equals(mn) && !"STB".equals(mn)) continue;
            if (in.getNumOperands() < 1) continue;
            Object[] ops = in.getOpObjects(0);
            if (ops.length != 1 || !(ops[0] instanceof Address)) continue;
            if (((Address) ops[0]).getOffset() != BANK_REG) continue;
            staSites.add(in.getAddress());
            Function f = fm.getFunctionContaining(in.getAddress());
            if (f != null) thunkEntries.add(f.getEntryPoint());
        }

        List<Address> sortedEntries = new ArrayList<>(thunkEntries);
        Collections.sort(sortedEntries);

        println(String.format(
            "Found %d STA/STB $3FFC sites across %d enclosing functions.",
            staSites.size(), sortedEntries.size()));
        println("");

        // 2) For each thunk function, enumerate inbound xrefs.
        for (Address entry : sortedEntries) {
            if (monitor.isCancelled()) return;
            Function f = fm.getFunctionAt(entry);
            String fname = (f == null) ? "<no_fn>" : f.getName();
            println("=================================================================");
            println("THUNK: " + fname + " @ " + entry);

            List<Reference> callers = new ArrayList<>();
            for (Reference r : refMgr.getReferencesTo(entry)) {
                RefType rt = r.getReferenceType();
                if (rt.isCall() || rt.isJump()) callers.add(r);
            }
            println("  inbound call/jump xrefs: " + callers.size());

            int shown = 0;
            for (Reference r : callers) {
                if (shown >= 12) {
                    println(String.format("  ... %d more callers (truncated) ...",
                            callers.size() - shown));
                    break;
                }
                shown++;
                Address from = r.getFromAddress();
                println("  --- caller @ " + from + " (" + r.getReferenceType() + ") ---");
                printContext(listing, mem, from, CTX_BEFORE, CTX_AFTER);
            }
            println("");
        }
    }

    private void printContext(Listing listing, Memory mem, Address callSite,
                              int before, int after) {
        // Walk back up to `before` instructions.
        List<Instruction> prevs = new ArrayList<>();
        Instruction cur = listing.getInstructionAt(callSite);
        if (cur == null) {
            println("    (no instruction at call site — possibly indirect)");
            return;
        }
        Instruction back = cur;
        for (int i = 0; i < before; i++) {
            back = back.getPrevious();
            if (back == null) break;
            prevs.add(0, back);
        }
        for (Instruction in : prevs) {
            println(formatInstr(in, mem));
        }
        println(formatInstr(cur, mem) + "    <<< call site");
        Instruction nx = cur;
        for (int i = 0; i < after; i++) {
            nx = nx.getNext();
            if (nx == null) break;
            println(formatInstr(nx, mem));
        }
    }

    private String formatInstr(Instruction in, Memory mem) {
        StringBuilder sb = new StringBuilder("    ");
        sb.append(in.getAddress().toString());
        sb.append("  ");
        // Raw bytes
        int len = in.getLength();
        StringBuilder bytes = new StringBuilder();
        for (int i = 0; i < len && i < 5; i++) {
            try {
                bytes.append(String.format("%02X ", mem.getByte(in.getAddress().add(i)) & 0xFF));
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
