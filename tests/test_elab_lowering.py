"""Elaborator: procedural lowering to MUX/PMUX/PRIORITY, state elements, memories, loops, functions."""
import random
import unittest

from tests.elab_helpers import Sim, codes, comb, elab, ops, top_module


def dffs(m):
    return [n for n in m.nodes if n.op == "DFF"]


class MuxStructures(unittest.TestCase):
    def test_if_else_is_one_mux(self):
        m = top_module(elab("""
module t(input s, input [7:0] a, b, output reg [7:0] y);
  always @* if (s) y = a; else y = b;
endmodule"""))
        self.assertEqual(ops(m), {"MUX": 1})

    def test_if_elseif_chain_is_priority_and_shares_one_group(self):
        m = top_module(elab("""
module t(input [2:0] s, input [7:0] a, b, c, d, output reg [7:0] y);
  always @* begin
    if (s[0]) y = a; else if (s[1]) y = b; else if (s[2]) y = c; else y = d;
  end
endmodule"""))
        self.assertEqual(ops(m), {"PRIORITY": 1})
        self.assertEqual(m.nodes[0].attrs["k"], 3)

    def test_case_with_constant_labels_is_pmux(self):
        m = top_module(elab("""
module t(input [1:0] s, input [7:0] a, b, c, output reg [7:0] y);
  always @* case (s)
    2'd0: y = a;
    2'd1: y = b;
    2'd2: y = c;
    default: y = 8'h0;
  endcase
endmodule"""))
        self.assertEqual(ops(m).get("PMUX"), 1)
        self.assertNotIn("PRIORITY", ops(m))
        pm = [n for n in m.nodes if n.op == "PMUX"][0]
        self.assertTrue("group" in pm.tags)

    def test_case_semantics(self):
        m = top_module(elab("""
module t(input [1:0] s, input [7:0] a, b, c, output reg [7:0] y);
  always @* case (s)
    2'd0: y = a;
    2'd1: y = b;
    2'd2: y = c;
    default: y = 8'h5A;
  endcase
endmodule"""))
        for s, exp in ((0, 1), (1, 2), (2, 3), (3, 0x5A)):
            self.assertEqual(comb(m, s=s, a=1, b=2, c=3)["y"], exp)

    def test_casez_casex_dont_cares_and_priority(self):
        m = top_module(elab("""
module t(input [3:0] r, output reg [1:0] z, output reg [1:0] x);
  always @* casez (r)
    4'b1???: z = 3;
    4'b01??: z = 2;
    4'b001?: z = 1;
    default: z = 0;
  endcase
  always @* casex (r)
    4'b1xxx: x = 3;
    4'bx1xx: x = 2;
    default: x = 0;
  endcase
endmodule"""))
        for r in range(16):
            o = comb(m, r=r)
            self.assertEqual(o["z"], 3 if r & 8 else 2 if r & 4 else 1 if r & 2 else 0)
            self.assertEqual(o["x"], 3 if r & 8 else 2 if r & 4 else 0)

    def test_ternary_chain_and_unique_priority_qualifiers(self):
        m = top_module(elab("""
module t(input [1:0] s, input [7:0] a, b, output reg [7:0] y, output reg [7:0] w);
  always_comb begin
    unique case (s)
      2'd0: y = a;
      2'd1: y = b;
      default: y = 0;
    endcase
    priority if (s == 2) w = a; else if (s == 3) w = b; else w = 0;
  end
endmodule"""))
        self.assertEqual(comb(m, s=1, a=4, b=9)["y"], 9)
        self.assertEqual(comb(m, s=3, a=4, b=9)["w"], 9)

    def test_blocking_assignments_within_a_comb_block_respect_order(self):
        m = top_module(elab("""
module t(input [7:0] a, output reg [7:0] y, output reg [7:0] z);
  always @* begin
    y = a; y = y + 1; z = y; y = y + 1;
  end
endmodule"""))
        o = comb(m, a=10)
        self.assertEqual((o["y"], o["z"]), (12, 11))


class Storage(unittest.TestCase):
    def test_latch_inference(self):
        r = elab("""
module t(input en, input [3:0] d, output reg [3:0] q);
  always @* if (en) q = d;
endmodule""")
        m = top_module(r)
        self.assertEqual(ops(m), {"LATCH": 1})
        self.assertIn("INFERRED_LATCH", codes(r))
        self.assertTrue([n for n in m.nodes if n.op == "LATCH"][0].attrs["inferred"])

    def test_default_assignment_avoids_latch(self):
        r = elab("""
module t(input en, input [3:0] d, output reg [3:0] q);
  always @* begin q = 0; if (en) q = d; end
endmodule""")
        self.assertNotIn("INFERRED_LATCH", codes(r))
        self.assertNotIn("LATCH", ops(top_module(r)))

    def test_always_latch(self):
        r = elab("""
module t(input en, input [3:0] d, output reg [3:0] q);
  always_latch if (en) q <= d;
endmodule""")
        self.assertEqual(ops(top_module(r)), {"LATCH": 1})

    def test_enable_sync_reset_async_reset_recognition(self):
        r = elab("""
module t(input clk, input rst, input arst_n, input en, input [7:0] d,
         output reg [7:0] plain, output reg [7:0] enab, output reg [7:0] srst, output reg [7:0] arst, output reg [7:0] both);
  always @(posedge clk) plain <= d;
  always @(posedge clk) if (en) enab <= d;
  always @(posedge clk) if (rst) srst <= 8'h3; else srst <= d;
  always @(posedge clk or negedge arst_n) if (!arst_n) arst <= 8'hA5; else arst <= d;
  always @(posedge clk or negedge arst_n) begin
    if (!arst_n) both <= 0;
    else if (rst) both <= 1;
    else if (en) both <= d;
  end
endmodule""")
        m = top_module(r)
        by = {n.outputs[0].name: n for n in dffs(m)}
        self.assertEqual(by["plain"].attrs["reset_kind"], "none")
        self.assertFalse(by["plain"].has_input("EN"))
        self.assertTrue(by["enab"].has_input("EN"))
        self.assertTrue(by["enab"].attrs["enable_recognized"])
        self.assertEqual(by["srst"].attrs["reset_kind"], "sync")
        self.assertEqual(by["srst"].attrs["reset_value"].value, 3)
        self.assertEqual((by["arst"].attrs["reset_kind"], by["arst"].attrs["arst_polarity"]), ("async", 0))
        self.assertEqual(by["arst"].attrs["reset_value"].value, 0xA5)
        self.assertEqual(by["both"].attrs["reset_kind"], "both")
        self.assertTrue(by["both"].has_input("EN"))
        self.assertFalse(any(n.op in ("MUX", "PRIORITY") for n in m.nodes))

    def test_active_high_async_reset_and_negedge_clock(self):
        r = elab("""
module t(input clk, input rst, input [3:0] d, output reg [3:0] q);
  always @(negedge clk or posedge rst) if (rst) q <= 0; else q <= d;
endmodule""")
        n = dffs(top_module(r))[0]
        self.assertEqual((n.attrs["clk_edge"], n.attrs["arst_polarity"], n.attrs["reset_kind"]), ("neg", 1, "async"))

    def test_counter_simulation(self):
        m = top_module(elab("""
module t(input clk, input rst, input en, output reg [3:0] c);
  always @(posedge clk) if (rst) c <= 0; else if (en) c <= c + 1;
endmodule"""))
        s = Sim(m)
        exp = 0
        rnd = random.Random(5)
        for _ in range(60):
            rst, en = rnd.randrange(2), rnd.randrange(2)
            out = s.step({"rst": rst, "en": en})
            self.assertEqual(out["c"], exp)
            exp = 0 if rst else (exp + 1) & 15 if en else exp

    def test_blocking_temporary_in_clocked_block_is_not_a_register(self):
        m = top_module(elab("""
module t(input clk, input [7:0] a, b, output reg [7:0] q);
  reg [7:0] tmp;
  always @(posedge clk) begin tmp = a + b; q <= tmp ^ a; end
endmodule"""))
        self.assertEqual(len(dffs(m)), 1)

    def test_blocking_loop_carried_variable_is_a_register(self):
        m = top_module(elab("""
module t(input clk, output reg [7:0] c);
  always @(posedge clk) c = c + 1;
endmodule"""))
        self.assertEqual(len(dffs(m)), 1)

    def test_function_local_and_early_return(self):
        m = top_module(elab("""
module t(input [7:0] a, output [7:0] y);
  function automatic [7:0] clamp(input [7:0] x);
    reg [7:0] t;
    begin
      if (x > 100) return 100;
      t = x + 5;
      if (t > 100) return 100;
      return t;
    end
  endfunction
  assign y = clamp(a);
endmodule"""))
        for a in range(0, 256, 7):
            e = 100 if a > 100 else min(a + 5, 100)
            self.assertEqual(comb(m, a=a)["y"], e)

    def test_task_with_output_argument(self):
        m = top_module(elab("""
module t(input [3:0] a, b, output reg [3:0] s, output reg c);
  task add(input [3:0] x, input [3:0] y, output [3:0] r, output co);
    reg [4:0] t;
    begin t = x + y; r = t[3:0]; co = t[4]; end
  endtask
  always @* add(a, b, s, c);
endmodule"""))
        for a in range(16):
            for b in (0, 7, 15):
                o = comb(m, a=a, b=b)
                self.assertEqual((o["s"], o["c"]), ((a + b) & 15, (a + b) >> 4))

    def test_for_loop_unrolling_break_continue(self):
        m = top_module(elab("""
module t(input [7:0] a, output reg [3:0] ones, output reg [3:0] first);
  integer i;
  always @* begin
    ones = 0; first = 8;
    for (i = 0; i < 8; i = i + 1) begin
      if (!a[i]) continue;
      ones = ones + 1;
      if (first == 8) first = i;
    end
  end
endmodule"""))
        for a in (0, 1, 0x80, 0xFF, 0x24, 0x10):
            o = comb(m, a=a)
            self.assertEqual(o["ones"], bin(a).count("1"))
            self.assertEqual(o["first"], (a & -a).bit_length() - 1 if a else 8)

    def test_while_repeat_and_foreach(self):
        m = top_module(elab("""
module t(input [7:0] a, output reg [3:0] n, output reg [7:0] r, output reg [3:0] s);
  integer i;
  logic [3:0] arr [0:2];
  always @* begin
    n = 0; i = 0;
    while (i < 8) begin if (a[i]) n = n + 1; i = i + 1; end
    r = 0;
    repeat (3) r = r + a[3:0];
    s = 0;
    arr[0] = 1; arr[1] = 2; arr[2] = 3;
    foreach (arr[k]) s = s + arr[k];
  end
endmodule"""))
        o = comb(m, a=0xB7)
        self.assertEqual((o["n"], o["r"], o["s"]), (bin(0xB7).count("1"), (3 * 7) & 255, 6))


class Memories(unittest.TestCase):
    def test_ram_with_sync_read_and_write_port(self):
        r = elab("""
module t(input clk, input we, input [3:0] wa, ra, input [7:0] d, output reg [7:0] q);
  reg [7:0] mem [0:15];
  always @(posedge clk) begin
    if (we) mem[wa] <= d;
    q <= mem[ra];
  end
endmodule""")
        m = top_module(r)
        mems = [n for n in m.nodes if n.op == "MEM"]
        self.assertEqual(len(mems), 1)
        a = mems[0].attrs
        self.assertEqual((a["depth"], a["width"], a["rports"], a["wports"], a["read_style"]), (16, 8, 1, 1, "sync"))
        self.assertEqual(len(dffs(m)), 0)
        self.assertTrue(mems[0].has_input("w0_en"))

    def test_regfile_with_async_reads_and_two_read_ports(self):
        r = elab("""
module t(input clk, input we, input [2:0] wa, ra, rb, input [7:0] d, output [7:0] qa, qb);
  reg [7:0] rf [0:7];
  always @(posedge clk) if (we) rf[wa] <= d;
  assign qa = rf[ra];
  assign qb = rf[rb];
endmodule""")
        mem = [n for n in top_module(r).nodes if n.op == "MEM"][0]
        self.assertEqual((mem.attrs["rports"], mem.attrs["wports"], mem.attrs["read_style"]), (2, 1, "async"))

    def test_memory_write_port_connections(self):
        m = top_module(elab("""
module t(input clk, input we, input [1:0] a, input [7:0] d, output [7:0] q);
  reg [7:0] mem [0:3];
  always @(posedge clk) if (we) mem[a] <= d;
  assign q = mem[a];
endmodule"""))
        mem = [n for n in m.nodes if n.op == "MEM"][0]
        ins = dict(mem.inputs)
        self.assertEqual(str(ins["w0_en"]), str(m.port("we").sig))
        self.assertEqual(ins["w0_data"], m.port("d").sig)
        self.assertEqual(ins["w0_addr"], m.port("a").sig)
        self.assertEqual(mem.attrs["read_style"], "async")

    def test_array_with_constant_indices_is_registers_not_memory(self):
        r = elab("""
module t(input clk, input [7:0] d, output reg [7:0] q0, q1);
  reg [7:0] arr [0:1];
  always @(posedge clk) begin arr[0] <= d; arr[1] <= arr[0]; end
  always @* begin q0 = arr[0]; q1 = arr[1]; end
endmodule""")
        m = top_module(r)
        self.assertNotIn("MEM", ops(m))
        self.assertEqual(len(dffs(m)), 2)


if __name__ == "__main__":
    unittest.main()


class MemoryPorts(unittest.TestCase):
    def test_identical_reads_share_one_port(self):
        r = elab("""
module t(input clk, input we, input [3:0] a, input [7:0] d, output [7:0] y);
  reg [7:0] mem [0:15];
  always @(posedge clk) if (we) mem[a] <= d;
  assign y = mem[a] + mem[a] + mem[a+1];
endmodule""")
        mem = [n for n in top_module(r).nodes if n.op == "MEM"][0]
        self.assertEqual(mem.attrs["rports"], 2)

    def test_partial_word_write_is_flagged_and_approximated(self):
        r = elab("""
module t(input clk, input [3:0] a, input [7:0] d, output [7:0] y);
  reg [15:0] mem [0:15];
  always @(posedge clk) mem[a][7:0] <= d;
  assign y = mem[a][7:0];
endmodule""")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, True))
        self.assertEqual(ops(top_module(r)).get("MEM"), 1)
