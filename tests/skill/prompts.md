# Trigger test set for the `gatecost` skill

Used when the `description` in `SKILL.md` is revised: read each prompt against the new description and check
that the skill would (or would not) be selected. `tests/test_docs_sync.py` checks that this file keeps both
lists populated; the judgement itself is manual (or by an evaluation run with the target agent).

## Should activate

1. How many gates does this ALU module cost?
2. I added a 16x16 multiplier to `alu.sv`. How much bigger did the design get?
3. Estimate the area of this Verilog module in NAND2 equivalents.
4. Is a 32-bit barrel shifter much more expensive than a rotate-by-constant in gates?
5. Compare the gate cost of a 4:1 mux tree against a one-hot AND-OR select for 64 bits.
6. Did my last commit increase the gate count of the core?
7. What is the GE count of the register file in `rtl/regfile.vhd`?
8. Before I write it: roughly how many gates is a 24x8 multiplier plus a 32-bit accumulator?
9. Check that the refactor of the decoder did not grow the logic.
10. We have a 50k GE budget for the top level; are we still under after this change?
11. How expensive in hardware is widening the data path from 16 to 32 bits?
12. Give me an area estimate for this SystemVerilog FSM without running synthesis.
13. Which module contributes most to the gate count of this design?
14. What does adding an enable to these 128 flops cost, in gates?

## Should not activate

1. Run Yosys on this design and give me the exact post-synthesis area.
2. What is the critical path delay of this pipeline?
3. Estimate the power consumption of the multiplier.
4. Write a testbench for the FIFO module.
5. Fix the syntax error in `uart_tx.v`.
6. Why does my simulation show X on `data_out`?
7. How many lines of code does this repository have?
8. Explain the difference between blocking and non-blocking assignments.
9. Reduce the memory footprint of my Python program.
10. How many transistors are in a modern GPU?
11. Convert this VHDL entity to SystemVerilog.
12. Place and route this design for an FPGA.
13. Lint the RTL for latches and unconnected ports.
14. Optimize the runtime of my C++ build.
