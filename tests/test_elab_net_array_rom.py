"""Net arrays filled by constant continuous assignments read as ROMs (module.py ``net_array_rom``).

Regression (secworks/aes ``aes_sbox``): ``wire [7:0] sbox [0:255]`` with every element driven by
``assign sbox[i] = 8'hXX;`` and read with a signal index used to elaborate as a 255-MUX tree per read
(constant leaves), ~2.6x the Yosys area.  When every element is driven exactly once by a constant
continuous assignment and nothing else writes the array, a variable-index read is now a ``ROM``
node with the table as contents; constant-index reads stay nets.
"""
import random
import unittest

from gatecost.cost import build_cost_tree, load_profile
from tests.elab_helpers import codes, comb, elab, ops, top_module
from tests.test_elab_vhdl_util import module as vmodule
from tests.test_elab_vhdl_util import unit as vunit

P = load_profile()


def table(n: int, w: int = 8) -> list:
    rnd = random.Random(n * 31 + w)
    return [rnd.randrange(1 << w) for _ in range(n)]


def wire_table(vals: list, w: int = 8, reads: int = 1, extra: str = "", skip: tuple = (),
               lo: int = 0, elem: dict | None = None) -> str:
    n = len(vals)
    ab = max(1, (n - 1).bit_length())
    outs = ", ".join(f"output wire [{w - 1}:0] y{k}" for k in range(reads))
    ins = ", ".join(f"input wire [{ab - 1}:0] a{k}" for k in range(reads))
    lines = [f"module t({ins}, input wire [{w - 1}:0] d, {outs}, output wire [{w - 1}:0] yc);",
             f"  wire [{w - 1}:0] tb [{lo}:{lo + n - 1}];"]
    for k in range(reads):
        lines.append(f"  assign y{k} = tb[a{k}];")
    lines.append(f"  assign yc = tb[{lo + 1}];")
    for i, v in enumerate(vals):
        if i in skip:
            continue
        rhs = (elem or {}).get(i, f"{w}'h{v:x}")
        lines.append(f"  assign tb[{lo + i}] = {rhs};")
    lines.append(extra)
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def case_table(vals: list, w: int = 8) -> str:
    n = len(vals)
    ab = max(1, (n - 1).bit_length())
    arms = "\n".join(f"      {ab}'d{i}: y0 = {w}'h{v:x};" for i, v in enumerate(vals))
    return f"""module t(input wire [{ab - 1}:0] a0, output reg [{w - 1}:0] y0);
  always @* begin
    case (a0)
{arms}
    endcase
  end
endmodule
"""


def cost(res) -> float:
    return build_cost_tree(res.design, P).total().typ


def roms(res) -> list:
    return [n for n in top_module(res).nodes if n.op == "ROM"]


class NetArrayRom(unittest.TestCase):
    def test_full_constant_table_is_rom(self):
        vals = table(256)
        res = elab(wire_table(vals))
        m = top_module(res)
        self.assertEqual(len(roms(res)), 1)
        self.assertEqual(roms(res)[0].attrs["contents"], tuple(vals))
        self.assertEqual(ops(m).get("MUX", 0), 0)
        self.assertEqual(codes(res, True), [])
        ref = elab(case_table(vals))
        self.assertEqual(len(roms(ref)), 1)
        self.assertLess(abs(cost(res) - cost(ref)), 0.15 * cost(ref))

    def test_values_read_back(self):
        vals = table(256)
        m = top_module(elab(wire_table(vals, reads=2)))
        for a in (0, 1, 2, 17, 128, 254, 255):
            out = comb(m, a0=a, a1=255 - a, d=0)
            self.assertEqual(out["y0"], vals[a])
            self.assertEqual(out["y1"], vals[255 - a])
            self.assertEqual(out["yc"], vals[1])

    def test_several_reads_and_constant_index_net(self):
        vals = table(64)
        res = elab(wire_table(vals, reads=3))
        self.assertEqual(len(roms(res)), 3)
        self.assertEqual(ops(top_module(res)).get("MUX", 0), 0)

    def test_offset_range_and_non_power_of_two(self):
        vals = table(40)
        res = elab(wire_table(vals, lo=8))
        self.assertEqual(len(roms(res)), 1)
        m = top_module(res)
        for a in (8, 9, 30, 47):
            self.assertEqual(comb(m, a0=a, d=0)["y0"], vals[a - 8])

    def test_incomplete_table_unchanged(self):
        vals = table(32)
        res = elab(wire_table(vals, skip=(5,)))
        self.assertEqual(roms(res), [])
        self.assertGreater(ops(top_module(res)).get("MUX", 0), 0)

    def test_non_constant_element_unchanged(self):
        vals = table(32)
        res = elab(wire_table(vals, elem={7: "d"}))
        self.assertEqual(roms(res), [])
        m = top_module(res)
        self.assertGreater(ops(m).get("MUX", 0), 0)
        self.assertEqual(comb(m, a0=7, d=0x5a)["y0"], 0x5a)

    def test_double_drive_unchanged(self):
        vals = table(32)
        res = elab(wire_table(vals, extra="  assign tb[3] = 8'h11;"))
        self.assertEqual(roms(res), [])

    def test_other_writer_unchanged(self):
        vals = table(32)
        res = elab(wire_table(vals, extra="  assign tb[2][0] = d[0];"))
        self.assertEqual(roms(res), [])

    def test_procedural_or_instance_writer_unchanged(self):
        vals = table(32)
        leaf = "module leaf(output wire [7:0] o); assign o = 8'h0; endmodule\n"
        for extra in ("  always @* tb[4][1] = d[1];", "  leaf u(.o(tb[9]));"):
            res = elab({"t.v": wire_table(vals, extra=extra), "leaf.v": leaf}, top="t")
            self.assertEqual(roms(res), [], extra)

    def test_read_in_always_block(self):
        vals = table(16)
        src = wire_table(vals).replace("  assign y0 = tb[a0];", "  reg [7:0] r;\n  always @* r = tb[a0];\n"
                                       "  assign y0 = r;")
        res = elab(src)
        self.assertEqual(len(roms(res)), 1)
        m = top_module(res)
        for a in (0, 5, 15):
            self.assertEqual(comb(m, a0=a, d=0)["y0"], vals[a])

    def test_small_table_unchanged(self):
        res = elab(wire_table(table(4)))
        self.assertEqual(roms(res), [])

    def test_parameterised_index_and_value(self):
        src = """module t #(parameter B = 4) (input wire [2:0] a, output wire [7:0] y);
  wire [7:0] tb [0:7];
  assign y = tb[a];
  genvar unused;
  assign tb[0] = B;      assign tb[1] = B + 1;  assign tb[2] = 8'd2;  assign tb[3] = 8'd3;
  assign tb[B] = 8'd40;  assign tb[B + 1] = 8'd50; assign tb[B + 2] = -8'sd1; assign tb[B + 3] = 8'd70;
endmodule
"""
        res = elab(src)
        self.assertEqual(len(roms(res)), 1)
        self.assertEqual(roms(res)[0].attrs["contents"], (4, 5, 2, 3, 40, 50, 255, 70))


def vhdl_table(vals: list, w: int = 8, reads: int = 1, extra: str = "", skip: tuple = (), lo: int = 0,
               elem: dict | None = None) -> str:
    n = len(vals)
    ab = max(1, (n - 1).bit_length())
    ports = "; ".join([f"a{k} : in unsigned({ab - 1} downto 0)" for k in range(reads)]
                      + [f"d : in std_logic_vector({w - 1} downto 0)"]
                      + [f"y{k} : out std_logic_vector({w - 1} downto 0)" for k in range(reads)]
                      + [f"yc : out std_logic_vector({w - 1} downto 0)"])
    decls = (f"type tab_t is array({lo} to {lo + n - 1}) of std_logic_vector({w - 1} downto 0);\n"
             "signal tb : tab_t;")
    body = [f"y{k} <= tb(to_integer(a{k}) + {lo});" if lo else f"y{k} <= tb(to_integer(a{k}));"
            for k in range(reads)]
    body.append(f"yc <= tb({lo + 1});")
    for i, v in enumerate(vals):
        if i in skip:
            continue
        rhs = (elem or {}).get(i, f'std_logic_vector(to_unsigned({v}, {w}))')
        body.append(f"tb({lo + i}) <= {rhs};")
    body.append(extra)
    return vunit(ports, decls, "\n".join(body))


def vroms(m) -> list:
    return [n for n in m.nodes if n.op == "ROM"]


class VhdlSignalArrayRom(unittest.TestCase):
    def test_full_constant_table_is_rom(self):
        vals = table(256)
        m, r, _ = vmodule(vhdl_table(vals))
        self.assertEqual(len(vroms(m)), 1)
        self.assertEqual(vroms(m)[0].attrs["contents"], tuple(vals))
        self.assertEqual(ops(m).get("MUX", 0), 0)
        for a in (0, 1, 77, 255):
            out = comb(m, a0=a, d=0)
            self.assertEqual(out["y0"], vals[a])
            self.assertEqual(out["yc"], vals[1])

    def test_matches_verilog_cost(self):
        vals = table(256)
        m, _, d = vmodule(vhdl_table(vals, reads=2))
        rv = elab(wire_table(vals, reads=2))
        vc = build_cost_tree(d, P).total().typ
        self.assertLess(abs(vc - cost(rv)), 0.05 * cost(rv))

    def test_incomplete_or_non_constant_unchanged(self):
        vals = table(32)
        for src in (vhdl_table(vals, skip=(4,)), vhdl_table(vals, elem={3: "d"}),
                    vhdl_table(vals, extra="process(d) begin tb(2) <= d; end process;")):
            m, _, _ = vmodule(src)
            self.assertEqual(vroms(m), [])
            self.assertGreater(ops(m).get("MUX", 0), 0)

    def test_offset_range(self):
        vals = table(40)
        m, _, _ = vmodule(vhdl_table(vals, lo=8))
        self.assertEqual(len(vroms(m)), 1)


if __name__ == "__main__":
    unittest.main()
