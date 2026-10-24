"""Whole unpacked arrays (port connections, array-to-array assignment) and the net-width limit."""
import unittest

from tests.elab_helpers import codes, comb, elab, top_module

SWAP = """module sw(input [3:0] i [2], output [3:0] o [2]);
  assign o[0] = i[1];
  assign o[1] = i[0];
endmodule
"""


def no_array_diag(r):
    return [d.message for d in r.diagnostics if "whole" in d.message and "array" in d.message]


class WholeArrays(unittest.TestCase):
    def test_instance_connection_is_wired(self):
        r = elab(SWAP + """module t(input [3:0] x0, x1, output [3:0] y0, y1);
  logic [3:0] a [2];
  logic [3:0] c [2];
  assign a[0] = x0;
  assign a[1] = x1;
  sw u(.i(a), .o(c));
  assign y0 = c[0];
  assign y1 = c[1];
endmodule""", top="t")
        self.assertEqual(no_array_diag(r), [])
        inst = [n for n in top_module(r).nodes if n.op == "INST"]
        self.assertEqual(len(inst), 1)

    def test_array_ports_flatten_first_element_high_ascending(self):
        r = elab("""module t(input [3:0] x [2], output [3:0] y [2], output [3:0] z0, z1);
  logic [3:0] b [2];
  assign b = x;
  assign y = b;
  assign z0 = b[0];
  assign z1 = b[1];
endmodule""", top="t")
        self.assertEqual(no_array_diag(r), [])
        out = comb(top_module(r), x=(3 << 4) | 9)
        self.assertEqual((out["y"], out["z0"], out["z1"]), ((3 << 4) | 9, 3, 9))

    def test_descending_array_and_array_assignment(self):
        r = elab("""module t(input [3:0] x0, x1, output [3:0] y0, y1, output [7:0] w);
  logic [3:0] a [1:0];
  logic [3:0] b [1:0];
  assign a[0] = x0;
  assign a[1] = x1;
  assign b = a;
  assign y0 = b[0];
  assign y1 = b[1];
  assign w = {b[1], b[0]};
endmodule""", top="t")
        self.assertEqual(no_array_diag(r), [])
        out = comb(top_module(r), x0=5, x1=12)
        self.assertEqual((out["y0"], out["y1"], out["w"]), (5, 12, (12 << 4) | 5))

    def test_descending_array_port_first_element_is_left_index(self):
        r = elab("""module t(input [3:0] x [1:0], output [3:0] z0, z1);
  logic [3:0] b [1:0];
  assign b = x;
  assign z0 = b[0];
  assign z1 = b[1];
endmodule""", top="t")
        out = comb(top_module(r), x=(3 << 4) | 9)      # x[1] is the left (high) element
        self.assertEqual((out["z1"], out["z0"]), (3, 9))

    def test_array_in_always_block(self):
        r = elab("""module t(input [3:0] x [2], input s, output logic [3:0] y [2]);
  logic [3:0] b [2];
  always_comb begin
    b = x;
    if (s) b[0] = 4'd1;
    y = b;
  end
endmodule""", top="t")
        self.assertEqual(no_array_diag(r), [])
        out = comb(top_module(r), x=(3 << 4) | 9, s=1)
        self.assertEqual(out["y"], (1 << 4) | 9)


class NetWidthLimit(unittest.TestCase):
    def test_indexed_struct_array_past_the_width_limit_is_not_internal(self):
        # a field offset past the (clamped) width of the outer array used to raise ValueError in SigSpec.slice
        src = """package p;
  typedef struct packed { logic [4:0] c; logic [4:0] b; logic [1048575:0] a; } s_t;
endpackage
module t import p::*; #(parameter int unsigned N = 32'd0)(
  input s_t [N-1:0] reqs, input logic [1:0] sel, output logic v, output logic [4:0] o);
  assign o = (sel[0]) ? reqs[sel].c : '0;
  assign v = reqs[sel].c[0];
endmodule"""
        r = elab(src, top="t")
        self.assertNotIn("INTERNAL", codes(r))
        self.assertIn("LIMIT_REACHED", codes(r))


class HugeAggregate(unittest.TestCase):
    def test_vhdl_aggregate_wider_than_the_net_limit_is_not_built_element_by_element(self):
        import time
        src = """library ieee; use ieee.std_logic_1164.all;
entity e is generic (N : positive := 6000000);
  port (a : in std_logic_vector(N-1 downto 0); y : out std_logic_vector(N-1 downto 0)); end entity;
architecture r of e is begin
  y <= (others => '0');
end architecture;"""
        t0 = time.time()
        r = elab_vhdl(src)
        self.assertLess(time.time() - t0, 20)
        self.assertNotIn("INTERNAL", codes(r))
        self.assertIn("LIMIT_REACHED", codes(r))


def elab_vhdl(text):
    import tempfile
    from pathlib import Path
    from gatecost.elab import elaborate_project
    with tempfile.TemporaryDirectory() as td:
        Path(td, "e.vhd").write_text(text)
        return elaborate_project([Path("e.vhd")], root=Path(td), top="e")


if __name__ == "__main__":
    unittest.main()
