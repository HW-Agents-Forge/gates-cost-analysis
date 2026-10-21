"""IR.md section 9: the textual dump is stable and golden-testable; plain-data export."""
import json
import unittest

from gatecost.ir import Const, Origin, SigSpec, dump, export_module

from tests.ir_dsl import E, Mod


def alu():
    m = Mod("alu", {"W": 32})
    a, b, sel = m.inp("a", 32), m.inp("b", 32), m.inp("sel", 1)
    n1 = m.b.add(a.s, b.s, 32, origin=Origin("alu.sv", 12, 3))
    n2 = m.b.mux(sel.s, a.s, n1, origin=Origin("alu.sv", 14, 3), group=m.b.new_group())
    m.out("y", E(m, n2))
    return m.done()


GOLDEN = """\
module alu (W=32)
  port in  a[31:0]
  port in  b[31:0]
  port in  sel[0:0]
  port out y[31:0]
  net n1[31:0] = ADD(b[31:0], a[31:0]) sa=0 sb=0   @alu.sv:12
  net n2[31:0] = MUX(a[31:0], n1[31:0], sel[0]) group=g1   @alu.sv:14
  y[31:0] <- n2[31:0]
  known n1: none  demand n1: 0xffffffff  eff_w(ADD)=32
  known n2: none  demand n2: 0xffffffff  eff_w(MUX)=32
"""


class DumpTests(unittest.TestCase):
    def test_golden_dump(self):
        self.assertEqual(dump(alu()), GOLDEN)

    def test_dump_without_analysis_has_no_fact_lines(self):
        m = Mod("alu", {"W": 32})
        a = m.inp("a", 4)
        m.out("y", a + a.sg(False))
        ir = m.done(analyze=False)
        text = dump(ir)
        self.assertNotIn("known", text)

    def test_normalized_dump_drops_names_and_locations(self):
        m = Mod("n")
        a, b = m.inp("a", 8), m.inp("b", 8)
        t = m.wire("internal_name", 8)
        m.b.connect(t.s.chunks[0].net, (a + b).s)
        m.out("y", t)
        ir = m.done()
        self.assertIn("internal_name", dump(ir))
        norm = dump(ir, normalize=True)
        self.assertNotIn("internal_name", norm)
        self.assertNotIn("@", norm)

    def test_constants_and_concatenation_formatting(self):
        m = Mod("f")
        a = m.inp("a", 4)
        m.out("y", E(m, SigSpec.concat([a.s, SigSpec.const(0b101, 3, xmask=0b010), a.s[1]])))
        text = dump(m.done())
        self.assertIn("{a[1], 3'b1x1, a[3:0]}", text)

    def test_register_dump_lists_control_inputs(self):
        m = Mod("r")
        d, clk, en, rst = m.inp("d", 4), m.inp("clk", 1), m.inp("en", 1), m.inp("rst", 1)
        q = m.b.net(4, "q")
        m.b.dff(d.s, clk.s, en=en.s, arst=rst.s, arst_value=3, q=q)
        m.out("y", E(m, q.sig))
        text = dump(m.done())
        self.assertIn("net q[3:0] = DFF(D=d[3:0], CLK=clk[0], EN=en[0], ARST=rst[0])", text)
        self.assertIn("reset_kind=async", text)
        self.assertIn("reset_value=4'h3", text)


class ExportTests(unittest.TestCase):
    def test_export_is_plain_json_data(self):
        ir = alu()
        data = export_module(ir)
        text = json.dumps(data, sort_keys=True)
        back = json.loads(text)
        self.assertEqual(back["name"], "alu")
        self.assertEqual(back["canonical_hash"], ir.canonical_hash.hex())
        ops = [n["op"] for n in back["nodes"]]
        self.assertEqual(sorted(ops), ["ADD", "MUX"])

    def test_node_record_contents(self):
        ir = alu()
        data = export_module(ir)
        add = next(n for n in data["nodes"] if n["op"] == "ADD")
        self.assertEqual(add["origin"], {"file": "alu.sv", "line": 12, "column": 3})
        self.assertEqual(add["eff"]["w"], 32)
        self.assertEqual(add["eff"]["declared"], 32)
        self.assertEqual(add["named_key"], ["alu", "y", "then", "ADD"])
        self.assertEqual(len(add["shape_key"]), 32)
        self.assertEqual([i["name"] for i in add["inputs"]], ["A", "B"])
        self.assertTrue(all(i["width"] == 32 for i in add["inputs"]))
        mux = next(n for n in data["nodes"] if n["op"] == "MUX")
        self.assertEqual(mux["group"], 1)
        self.assertEqual(mux["tags"], {"group": 1})
        self.assertEqual(mux["fused_into"], None)
        self.assertEqual(add["merge_count"], 1)

    def test_sum_exports_members(self):
        m = Mod("s")
        a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 16)
        m.out("y", a.mul(b, 16) + c)
        ir = m.done()
        data = export_module(ir)
        (s,) = data["nodes"]
        self.assertEqual(s["op"], "SUM")
        self.assertEqual(sorted(x["op"] for x in s["members"]), ["ADD", "MUL"])
        self.assertTrue(all(x["fused_into"] == s["id"] for x in s["members"]))
        self.assertEqual(s["eff"]["k"], 2)

    def test_instance_and_register_fields(self):
        m = Mod("top")
        a, clk = m.inp("a", 4), m.inp("clk", 1)
        outs = m.b.inst("child(W=4)", "u0", {"x": a.s}, {"y": 4}, count=3,
                        origin=Origin("t.v", 5, 1))
        q = m.b.net(4, "q")
        m.b.dff(outs["y"], clk.s, q=q)
        m.out("o", E(m, q.sig))
        data = export_module(m.done())
        inst = next(n for n in data["nodes"] if n["op"] == "INST")
        self.assertEqual(inst["instance"]["module_key"], "child(W=4)")
        self.assertEqual(inst["instance"]["count"], 3)
        self.assertEqual(inst["instance"]["name"], "u0")
        dff = next(n for n in data["nodes"] if n["op"] == "DFF")
        self.assertEqual(dff["attrs"]["reset_kind"], "none")

    def test_export_order_is_deterministic(self):
        a = json.dumps(export_module(alu()), sort_keys=True)
        b = json.dumps(export_module(alu()), sort_keys=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
