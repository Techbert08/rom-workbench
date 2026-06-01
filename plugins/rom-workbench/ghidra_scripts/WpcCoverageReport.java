// Report per-block byte coverage: instruction vs defined-data vs undefined.
// @category    WPC

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.CodeUnit;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.Listing;
import ghidra.program.model.mem.MemoryBlock;

public class WpcCoverageReport extends GhidraScript {

    private static class Row {
        String name;
        long total, insn, data, undef;
    }

    @Override
    public void run() throws Exception {
        Listing listing = currentProgram.getListing();
        List<Row> rows = new ArrayList<>();

        long totT = 0, totI = 0, totD = 0, totU = 0;

        for (MemoryBlock blk : currentProgram.getMemory().getBlocks()) {
            if (!blk.isInitialized()) continue;
            // Only count blocks that hold actual ROM bytes.
            String name = blk.getName();
            if (!name.equals("ROM_SYSTEM") && !name.startsWith("ROM_PAGE_")) continue;

            Row r = new Row();
            r.name = name;
            r.total = blk.getSize();

            Address a = blk.getStart();
            Address end = blk.getEnd();
            while (a.compareTo(end) <= 0) {
                if (monitor.isCancelled()) return;
                CodeUnit cu = listing.getCodeUnitAt(a);
                long len;
                if (cu instanceof Instruction) {
                    len = cu.getLength();
                    r.insn += len;
                } else if (cu instanceof Data) {
                    Data d = (Data) cu;
                    len = d.getLength();
                    if (d.isDefined()) r.data += len;
                    else r.undef += len;
                } else {
                    len = 1;
                    r.undef += 1;
                }
                try { a = a.add(len); }
                catch (Exception e) { break; }
            }

            rows.add(r);
            totT += r.total; totI += r.insn; totD += r.data; totU += r.undef;
        }

        // Stable sort: ROM_SYSTEM first, then ROM_PAGE_XX by name.
        Collections.sort(rows, (x, y) -> {
            if (x.name.equals("ROM_SYSTEM")) return -1;
            if (y.name.equals("ROM_SYSTEM")) return  1;
            return x.name.compareTo(y.name);
        });

        println("");
        println(String.format("%-13s  %7s  %7s  %7s  %7s   %5s",
                "block", "size", "insn", "data", "undef", "code%"));
        for (Row r : rows) {
            double codePct = r.total == 0 ? 0.0 : 100.0 * r.insn / r.total;
            println(String.format("%-13s  %7d  %7d  %7d  %7d   %5.1f",
                    r.name, r.total, r.insn, r.data, r.undef, codePct));
        }
        double totPct = totT == 0 ? 0.0 : 100.0 * totI / totT;
        println(String.format("%-13s  %7d  %7d  %7d  %7d   %5.1f",
                "TOTAL", totT, totI, totD, totU, totPct));
    }
}
