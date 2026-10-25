"""IR.md section 12: golden tests for the analysis passes (6.1 to 6.6)."""
import unittest

from gatecost.ir import Builder, Const, LowerBlock, SigSpec, analyze_ir

from tests.ir_dsl import E, Mod, cat, nodes_of, ops_of


def pat(net):
    """Known-bit pattern MSB first: 0 / 1 / x."""
    return "".join("x" if not (net.kmask >> i) & 1 else str((net.kval >> i) & 1)
                   for i in range(net.width - 1, -1, -1))


class Pat:
    """Operands described by patterns such as '01xx' (x = free input bit)."""

    def __init__(self, m: Mod) -> None:
        self.m = m
        self.n = 0

    def __call__(self, p: str, signed=False) -> E:
        w = len(p)
        self.n += 1
        port = self.m.inp(f"p{self.n}", w)
        bits = []
        for i, ch in enumerate(reversed(p)):
            bits.append(port.s[i] if ch == "x" else SigSpec.const(int(ch), 1))
        return E(self.m, SigSpec.concat(bits), signed)


class KnownBits(unittest.TestCase):
    def run_op(self, build, width=None):
        m = Mod("k")
        p = Pat(m)
        e = build(m, p)
        y = m.out("y", e)
        ir = m.done()
        return pat(ir.port("y"))

    def test_bitwise(self):
        self.assertEqual(self.run_op(lambda m, p: p("01xx") & p("x1x0")), "01x0")
        self.assertEqual(self.run_op(lambda m, p: p("01xx") | p("x1x0")), "x1xx")
        self.assertEqual(self.run_op(lambda m, p: p("01xx") | p("1x00")), "11xx")
        self.assertEqual(self.run_op(lambda m, p: p("01xx") ^ p("x110")), "x0xx")
        self.assertEqual(self.run_op(lambda m, p: ~p("01xx")), "10xx")
        r = self.run_op(lambda m, p: E(m, m.b.xnor(p("01xx").s, p("x110").s, 4)))
        self.assertEqual(r, "x1xx")

    def test_add_sub_low_known_run(self):
        self.assertEqual(self.run_op(lambda m, p: p("xx01") + p("xx10")), "xx11")
        self.assertEqual(self.run_op(lambda m, p: p("xx11") - p("xx01")), "xx10")
        self.assertEqual(self.run_op(lambda m, p: p("xx00") + p("xxxx")), "xxxx")
        # a gap in the known low bits stops the run
        self.assertEqual(self.run_op(lambda m, p: p("xx01") + p("x0x1")), "xxx0")

    def test_add_upper_zero_bound(self):
        self.assertEqual(self.run_op(lambda m, p: p("0000xxxx") + p("0000xxxx")), "000xxxxx")
        self.assertEqual(self.run_op(lambda m, p: p("0000xxxx") + p("00000xxx")), "000xxxxx")

    def test_neg_mul(self):
        self.assertEqual(self.run_op(lambda m, p: -p("xx10")), "xx10")
        self.assertEqual(self.run_op(lambda m, p: p("xxx0") * p("xx10")), "xx00")
        self.assertEqual(self.run_op(lambda m, p: p("0000xxxx").zext(16) * p("0000xxxx").zext(16)),
                         "00000000xxxxxxxx")

    def test_shifts_by_variable_amount(self):
        self.assertEqual(self.run_op(lambda m, p: p("xx00") << p("xx")), "xx00")
        self.assertEqual(self.run_op(lambda m, p: p("00xx") >> p("xx")), "00xx")
        self.assertEqual(self.run_op(lambda m, p: E(m, m.b.sar(p("00xx").s, p("xx").s, 4))), "00xx")
        self.assertEqual(self.run_op(lambda m, p: E(m, m.b.rol(p("1111").s, p("xx").s, 4))), "1111")

    def test_compare_and_reduce(self):
        self.assertEqual(self.run_op(lambda m, p: p("1xxx") == p("0xxx")), "0")
        self.assertEqual(self.run_op(lambda m, p: p("1xxx") != p("0xxx")), "1")
        self.assertEqual(self.run_op(lambda m, p: p("1xxx") == p("1xxx")), "x")
        self.assertEqual(self.run_op(lambda m, p: p("x0xx").red_and()), "0")
        self.assertEqual(self.run_op(lambda m, p: p("x1xx").red_or()), "1")
        self.assertEqual(self.run_op(lambda m, p: p("x1xx").red_and()), "x")

    def test_mux(self):
        self.assertEqual(self.run_op(lambda m, p: p("x").mux(p("01x1"), p("0x11"))), "0xx1")

    def test_mux_with_known_select_folds(self):
        m = Mod("k", const_ports={"s": Const(1, 1)})
        p = Pat(m)
        s = m.inp("s", 1)
        r = s.mux(p("01x1"), p("0x11"))
        m.out("y", r)
        ir = m.done()
        self.assertEqual(pat(ir.port("y")), "0x11")
        self.assertEqual(nodes_of(ir, "MUX"), [])           # folded to a wire

    def test_pmux_all_arms_agree(self):
        def build(m, p):
            return E(m, m.b.pmux(p("01x1").s, [p("0x11").s, p("0x01").s], [p("x").s, p("x").s]))
        self.assertEqual(self.run_op(build), "0xx1")

    def test_decode_and_rom(self):
        self.assertEqual(self.run_op(lambda m, p: E(m, m.b.decode(p("1x").s))), "xx00")
        self.assertEqual(self.run_op(lambda m, p: E(m, m.b.decode(p("x0").s))), "0x0x")
        self.assertEqual(self.run_op(lambda m, p: E(m, m.b.rom(p("xx").s, [3, 1, 3, 1], 2))), "x1")

    def test_register_with_constant_reset_value(self):
        m = Mod("r")
        p = Pat(m)
        clk = m.inp("clk", 1)
        rst = m.inp("rst", 1)
        d = p("00xx")
        q = m.b.net(4, "q")
        m.b.dff(d.s, clk.s, arst=rst.s, arst_value=0, q=q)
        m.out("y", E(m, q.sig))
        ir = m.done()
        self.assertEqual(pat(ir.port("y")), "00xx")

    def test_constant_port_marks_known(self):
        m = Mod("c", const_ports={"k": Const(0b1010, 4)})
        k = m.inp("k", 4)
        a = m.inp("a", 4)
        m.out("y", a & k)
        ir = m.done()
        self.assertEqual(pat(ir.port("k")), "1010")
        self.assertEqual(pat(ir.port("y")), "x0x0")


class DeadLogic(unittest.TestCase):
    def test_unreachable_cone_removed_exactly(self):
        m = Mod("d")
        a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
        live = (a + b).trunc(8)
        m.out("y", live)
        dead = ((a * c).trunc(8) + b).trunc(8)         # MUL -> ADD, unused
        _ = ~dead
        ir = m.done()
        self.assertEqual(ops_of(ir), ["ADD"])
        self.assertEqual(len(ir.nodes), 1)

    def test_register_not_reaching_output_is_removed(self):
        m = Mod("d")
        d, clk = m.inp("d", 4), m.inp("clk", 1)
        q = m.b.net(4, "q")
        m.b.dff(d.s, clk.s, q=q)
        q2 = m.b.net(4, "q2")
        m.b.dff(E(m, q.sig).s, clk.s, q=q2)           # feeds nothing observable
        m.out("y", d)
        ir = m.done()
        self.assertEqual(nodes_of(ir, "DFF"), [])

    def test_register_reaching_output_is_kept(self):
        m = Mod("d")
        d, clk = m.inp("d", 4), m.inp("clk", 1)
        q = m.b.net(4, "q")
        m.b.dff(d.s, clk.s, q=q)
        q2 = m.b.net(4, "q2")
        m.b.dff(q.sig, clk.s, q=q2)
        m.out("y", E(m, q2.sig))
        ir = m.done()
        self.assertEqual(len(nodes_of(ir, "DFF")), 2)

    def test_constant_register_replaced_by_constant(self):
        m = Mod("d")
        clk, rst = m.inp("clk", 1), m.inp("rst", 1)
        a = m.inp("a", 4)
        q = m.b.net(4, "q")
        m.b.dff(SigSpec.const(5, 4), clk.s, arst=rst.s, arst_value=5, q=q)
        m.out("y", E(m, q.sig) + a)
        ir = m.done()
        self.assertEqual(nodes_of(ir, "DFF"), [])
        self.assertTrue(any(d.code == "CONSTANT_REGISTER" for d in ir.diagnostics))
        self.assertEqual(pat(ir.port("y")), "xxxx")
        # but the constant now feeds the adder as a constant operand
        add = nodes_of(ir, "ADD")[0]
        self.assertEqual(add.eff["ops"]["A"]["const"] if add.eff["ops"]["A"]["const"] is not None
                         else add.eff["ops"]["B"]["const"], 5)

    def test_register_with_other_reset_value_is_kept(self):
        m = Mod("d")
        clk, rst = m.inp("clk", 1), m.inp("rst", 1)
        q = m.b.net(4, "q")
        m.b.dff(SigSpec.const(5, 4), clk.s, arst=rst.s, arst_value=0, q=q)
        m.out("y", E(m, q.sig))
        ir = m.done()
        self.assertEqual(len(nodes_of(ir, "DFF")), 1)

    def test_instances_and_memory_writes_are_roots(self):
        m = Mod("d")
        a = m.inp("a", 8)
        clk = m.inp("clk", 1)
        t = a + m.inp("b", 8)
        m.b.blackbox("pll", "u_pll", {"x": t.s}, {})
        mem = m.b.memory("ram", 16, 8)
        mem.write(a.s[0:4], t.s, en=SigSpec.const(1, 1), clk=clk.s)
        m.out("q", E(m, mem.read(a.s[0:4])))        # a memory is live only when a read port is observed
        ir = m.done()
        self.assertEqual(len(nodes_of(ir, "ADD")), 1)
        self.assertEqual(len(nodes_of(ir, "BLACKBOX")), 1)
        self.assertEqual(len(nodes_of(ir, "MEM")), 1)
        self.assertEqual(nodes_of(ir, "MEM")[0].named_key, ("d", "ram", "", "MEM"))


class DemandedBits(unittest.TestCase):
    def demand(self, build, name):
        m = Mod("dm")
        e = build(m)
        m.out("y", e)
        ir = m.done()
        return ir.port(name).demand

    def test_slice_demands_only_sliced_bits(self):
        d = self.demand(lambda m: m.inp("a", 8)[4:8], "a")
        self.assertEqual(d, 0xF0)

    def test_add_demands_low_bits_only(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return (a + b).trunc(16)[0:4]
        self.assertEqual(self.demand(build, "a"), 0xF)
        self.assertEqual(self.demand(build, "b"), 0xF)

    def test_comparison_demands_all_bits(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return a < b
        self.assertEqual(self.demand(build, "a"), 0xFFFF)

    def test_and_with_known_zero_operand_bits(self):
        def build(m):
            a = m.inp("a", 8)
            return a & m.c(0x0F, 8)
        self.assertEqual(self.demand(build, "a"), 0x0F)

    def test_or_with_known_one_operand_bits(self):
        def build(m):
            a = m.inp("a", 8)
            return a | m.c(0xF0, 8)
        self.assertEqual(self.demand(build, "a"), 0x0F)

    def test_shift_left_demands_low_data_bits_and_whole_amount(self):
        def build(m):
            a, s = m.inp("a", 16), m.inp("s", 4)
            return (a << s)[0:8]
        self.assertEqual(self.demand(build, "a"), 0xFF)
        self.assertEqual(self.demand(build, "s"), 0xF)

    def test_shift_right_demands_all_data_bits(self):
        def build(m):
            a, s = m.inp("a", 16), m.inp("s", 4)
            return (a >> s)[0:8]
        self.assertEqual(self.demand(build, "a"), 0xFFFF)

    def test_mul_demands_low_bits(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return a.mul(b, 16)[0:6]
        self.assertEqual(self.demand(build, "a"), 0x3F)

    def test_register_inputs_fully_demanded_even_if_q_partly_used(self):
        m = Mod("dm")
        d, clk = m.inp("d", 8), m.inp("clk", 1)
        q = m.b.net(8, "q")
        m.b.dff(d.s, clk.s, q=q)
        m.out("y", E(m, q.sig)[0:1])
        ir = m.done()
        self.assertEqual(ir.port("d").demand, 0xFF)
        self.assertEqual(nodes_of(ir, "DFF")[0].outputs[0].demand, 0x01)

    def test_mux_select_known_demands_only_selected_arm(self):
        m = Mod("dm", const_ports={"s": Const(0, 1)})
        s, a, b = m.inp("s", 1), m.inp("a", 8), m.inp("b", 8)
        m.out("y", s.mux(a, b))
        ir = m.done()
        self.assertEqual(ir.port("a").demand, 0xFF)
        self.assertEqual(ir.port("b").demand, 0)

    def test_output_ports_fully_demanded(self):
        m = Mod("dm")
        a = m.inp("a", 8)
        m.out("y", a)
        ir = m.done()
        self.assertEqual(ir.port("y").demand, 0xFF)


class EffectiveWidths(unittest.TestCase):
    """The examples of COST_MODEL.md section 7 'Width handling'."""

    def eff(self, build, op):
        m = Mod("ew")
        e = build(m)
        m.out("y", e)
        ir = m.done(fusion=False)
        ns = nodes_of(ir, op)
        self.assertEqual(len(ns), 1, ops_of(ir))
        return ns[0].eff

    def test_truncated_add(self):
        def build(m):
            a, b = m.inp("a", 32), m.inp("b", 32)
            return (a + b)[0:16]
        e = self.eff(build, "ADD")
        self.assertEqual(e["w"], 16)
        self.assertEqual(e["declared"], 32)

    def test_full_add(self):
        def build(m):
            a, b = m.inp("a", 32), m.inp("b", 32)
            return a + b
        self.assertEqual(self.eff(build, "ADD")["w"], 32)

    def test_zero_extended_add_costs_one_extra_bit(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return a.zext(32).add(b.zext(32), 32)
        e = self.eff(build, "ADD")
        self.assertEqual(e["w"], 17)
        self.assertEqual((e["wa"], e["wb"]), (16, 16))

    def test_zero_extended_add_result_declared_17(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return a.zext(17) + b.zext(17)
        self.assertEqual(self.eff(build, "ADD")["w"], 17)

    def test_sign_extended_operands_cost_one_extra_bit(self):
        def build(m):
            a, b = m.inp("a", 8), m.inp("b", 8)
            return a.sg().sext(16).sg().add(b.sg().sext(16).sg(), 16, )
        e = self.eff(build, "ADD")
        self.assertEqual((e["wa"], e["wb"]), (8, 8))
        self.assertEqual(e["w"], 9)

    def test_sign_extension_through_the_builder(self):
        def build(m):
            a, b = m.inp("a", 8, signed=True), m.inp("b", 8, signed=True)
            return a.add(b, 16)
        e = self.eff(build, "ADD")
        self.assertEqual(e["w"], 9)

    def test_low_half_only_mul_is_truncated(self):
        def build(m):
            a, b = m.inp("a", 16), m.inp("b", 16)
            return a.mul(b, 32)[0:16]
        e = self.eff(build, "MUL")
        self.assertEqual((e["wa"], e["wb"]), (16, 16))
        self.assertTrue(e["truncated"])
        self.assertEqual(e["out"], 16)

    def test_full_mul_not_truncated(self):
        def build(m):
            a, b = m.inp("a", 8), m.inp("b", 8)
            return a.mul(b, 16)
        e = self.eff(build, "MUL")
        self.assertFalse(e["truncated"])
        self.assertEqual((e["wa"], e["wb"], e["out"]), (8, 8, 16))

    def test_mul_strips_trailing_zeros_and_known_zero_msbs(self):
        def build(m):
            a, b = m.inp("a", 8), m.inp("b", 5)
            return a.mul(cat(b, m.c(0, 3)), 16)
        e = self.eff(build, "MUL")
        self.assertEqual(e["shift"], 3)
        self.assertEqual(sorted((e["wa"], e["wb"])), [5, 8])

    def test_mul_zero_extended_operands(self):
        def build(m):
            a, b = m.inp("a", 6), m.inp("b", 4)
            return a.zext(16).mul(b.zext(16), 16)
        e = self.eff(build, "MUL")
        self.assertEqual((e["wa"], e["wb"]), (6, 4))

    def test_constant_operand_is_reported(self):
        def build(m):
            a = m.inp("a", 8)
            return a.mul(m.c(10, 8), 8)
        m = Mod("ew")
        a = m.inp("a", 8)
        m.out("y", a.mul(m.c(10, 8), 8))
        ir = m.done()
        n = nodes_of(ir, "MUL")[0]
        consts = sorted(v["const"] for v in n.eff["ops"].values() if v["const"] is not None)
        self.assertEqual(consts, [10])

    def test_demand_zero_node_costs_nothing(self):
        # operands demanded only above the node's useful bits
        m = Mod("ew")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", cat(m.c(0, 8), (a + b))[8:16])           # slice of the constant part only
        ir = m.done()
        self.assertEqual(ir.nodes, [])

    def test_fully_known_output_marks_const_folded(self):
        m = Mod("ew", const_ports={"k": Const(3, 4)})
        k = m.inp("k", 4)
        a = m.inp("a", 4)
        m.out("y", (k & k) + a)
        ir = m.done()
        self.assertEqual(ops_of(ir), ["ADD"])

    def test_bitwise_width_counts_only_active_bits(self):
        m = Mod("ew")
        a = m.inp("a", 8)
        b = m.inp("b", 8)
        m.out("y", a & cat(m.c(0xF, 4), b[0:4]))
        ir = m.done()
        n = nodes_of(ir, "AND")[0]
        # upper nibble is `a & 1111` (a wire), lower nibble is a real AND
        self.assertEqual(n.eff["w"], 4)


class Fusion(unittest.TestCase):
    def fused(self, build):
        m = Mod("fu")
        e = build(m)
        m.out("y", e)
        return m.done()

    def test_multiply_add_fuses(self):
        def build(m):
            a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 16)
            return a.mul(b, 16) + c
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["SUM"])
        s = ir.nodes[0]
        self.assertEqual(s.attrs["k"], 2)
        self.assertEqual(sorted(x.op for x in s.members), ["ADD", "MUL"])
        self.assertIsNotNone(s.eff["mul"])

    def test_four_operand_add_chain_fuses(self):
        def build(m):
            a, b, c, d = (m.inp(n, 8) for n in "abcd")
            return ((a + b) + c) + d
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["SUM"])
        self.assertEqual(ir.nodes[0].attrs["k"], 4)
        self.assertEqual(len(ir.nodes[0].members), 3)

    def test_balanced_add_tree_fuses(self):
        def build(m):
            a, b, c, d = (m.inp(n, 8) for n in "abcd")
            return (a + b) + (c + d)
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["SUM"])
        self.assertEqual(ir.nodes[0].attrs["k"], 4)

    def test_signs_of_sub_and_neg(self):
        def build(m):
            a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
            return (a - b) - (-c)
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["SUM"])
        s = ir.nodes[0]
        terms = {dict(s.inputs)[f"T{i}"].chunks[0].net.name: sg for i, sg in enumerate(s.attrs["signs"])}
        self.assertEqual(terms, {"a": 1, "b": -1, "c": 1})

    def test_fanout_two_blocks_fusion(self):
        m = Mod("fu")
        a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
        t = a + b
        m.out("y", t + c)
        m.out("z", t)
        ir = m.done()
        self.assertEqual(ops_of(ir), ["ADD", "ADD"])

    def test_narrower_intermediate_is_not_fused(self):
        def build(m):
            a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 16)
            return (a + b).trunc(8).zext(16) + c      # inner wraps at 8 bits
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["ADD", "ADD"])

    def test_wider_intermediate_is_fused(self):
        def build(m):
            a, b, c = m.inp("a", 16), m.inp("b", 16), m.inp("c", 8)
            return (a + b).trunc(8) + c               # outer uses low bits only
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["SUM"])

    def test_only_one_multiplier_per_cluster(self):
        def build(m):
            a, b, c, d = (m.inp(n, 8) for n in "abcd")
            return a.mul(b, 16) + c.mul(d, 16).add(m.inp("e", 16), 16)
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["MUL", "SUM"])
        s = nodes_of(ir, "SUM")[0]
        self.assertIsNotNone(s.eff["mul"])
        self.assertEqual(s.attrs["k"], 3)           # product pair + the other MUL's output + e

    def test_mul_alone_or_mul_into_mul_is_not_a_cluster(self):
        def build(m):
            a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
            return a.mul(b, 8).mul(c, 8)
        ir = self.fused(build)
        self.assertEqual(ops_of(ir), ["MUL", "MUL"])

    def test_fusion_disabled(self):
        m = Mod("fu")
        a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
        m.out("y", (a + b) + c)
        ir = m.done(fusion=False)
        self.assertEqual(ops_of(ir), ["ADD", "ADD"])

    def test_members_keep_origins_and_effective_widths(self):
        from gatecost.ir import Origin
        m = Mod("fu")
        a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
        t = m.b.add(a.s, b.s, 8, origin=Origin("f.v", 3, 1))
        y = m.b.add(t, c.s, 8, origin=Origin("f.v", 4, 1))
        m.out("y", E(m, y))
        ir = m.done()
        s = ir.nodes[0]
        self.assertEqual(sorted(x.origin.line for x in s.members), [3, 4])
        self.assertTrue(all(x.eff["w"] == 8 for x in s.members))


class Sharing(unittest.TestCase):
    def build_case(self, op, n=4, width=16, extra=None):
        b = Builder("sh")
        sel = b.port("sel", "in", 2)
        ops = [(b.port(f"a{i}", "in", width), b.port(f"b{i}", "in", width)) for i in range(n)]
        y = b.port("y", "out", width)
        blk = LowerBlock(b, "comb")
        items = []
        for i, (x, z) in enumerate(ops):
            r = b.op(op, x.sig, z.sig, width=width)
            items.append(([SigSpec.const(i, 2)], lambda r=r: blk.assign(y, r)))
        blk.case_(sel.sig, items, lambda: blk.assign(y, SigSpec.const(0, width)))
        blk.finish()
        m = b.finalize()
        analyze_ir(m)
        return m

    def test_case_of_four_muls_marked_shared(self):
        m = self.build_case("MUL")
        muls = nodes_of(m, "MUL")
        self.assertEqual(len(muls), 4)
        groups = {n.tags.get("shared") for n in muls}
        self.assertEqual(len(groups), 1)
        self.assertNotIn(None, groups)
        self.assertEqual(len(m.share_sets), 1)
        self.assertEqual(m.share_sets[0]["class"], "MUL")
        self.assertEqual(len(m.share_sets[0]["nodes"]), 4)

    def test_wide_adds_share_narrow_adds_do_not(self):
        wide = self.build_case("ADD", 3, 16)
        self.assertEqual(len(wide.share_sets), 1)
        narrow = self.build_case("ADD", 3, 4)
        self.assertEqual(narrow.share_sets, [])

    def test_if_else_two_dividers(self):
        b = Builder("sh")
        c = b.port("c", "in", 1)
        a1, a2, d1, d2 = (b.port(n, "in", 16) for n in ("a1", "a2", "d1", "d2"))
        y = b.port("y", "out", 16)
        blk = LowerBlock(b, "comb")
        blk.if_(c.sig, lambda: blk.assign(y, b.div(a1.sig, d1.sig, 16)),
                lambda: blk.assign(y, b.div(a2.sig, d2.sig, 16)))
        blk.finish()
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual([s["class"] for s in m.share_sets], ["DIV"])

    def test_no_sharing_across_always_blocks(self):
        b = Builder("sh")
        c = b.port("c", "in", 1)
        a1, a2, d1, d2 = (b.port(n, "in", 16) for n in ("a1", "a2", "d1", "d2"))
        y1, y2 = b.port("y1", "out", 16), b.port("y2", "out", 16)
        for y, a, d in ((y1, a1, d1), (y2, a2, d2)):
            blk = LowerBlock(b, "comb")
            blk.if_(c.sig, lambda y=y, a=a, d=d: blk.assign(y, b.mul(a.sig, d.sig, 16)),
                    lambda y=y: blk.assign(y, SigSpec.const(0, 16)))
            blk.finish()
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual(m.share_sets, [])

    def test_div_and_mod_of_the_same_operands_are_paired(self):
        m = Mod("dm")
        a, b = m.inp("a", 16), m.inp("b", 16)
        m.out("q", a // b)
        m.out("r", a % b)
        m.out("r2", (a % m.inp("c", 16)))
        ir = m.done()
        divs = nodes_of(ir, "DIV")
        mods = sorted(nodes_of(ir, "MOD"), key=lambda n: n.id)
        self.assertEqual(len(divs), 1)
        paired = [n for n in mods if n.tags.get("divmod_with") == divs[0].id]
        self.assertEqual(len(paired), 1)

    def test_shared_result_used_elsewhere_is_not_a_candidate(self):
        b = Builder("sh")
        c = b.port("c", "in", 1)
        a1, a2, d1, d2 = (b.port(n, "in", 16) for n in ("a1", "a2", "d1", "d2"))
        y, z = b.port("y", "out", 16), b.port("z", "out", 16)
        blk = LowerBlock(b, "comb")
        m1 = b.mul(a1.sig, d1.sig, 16)
        m2 = b.mul(a2.sig, d2.sig, 16)
        blk.if_(c.sig, lambda: blk.assign(y, m1), lambda: blk.assign(y, m2))
        blk.finish()
        b.connect(z, m1)
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual(m.share_sets, [])

    def test_concatenated_leaves_are_not_exclusive(self):
        b = Builder("sh")
        c = b.port("c", "in", 1)
        a1, a2, d1, d2 = (b.port(n, "in", 8) for n in ("a1", "a2", "d1", "d2"))
        y = b.port("y", "out", 16)
        blk = LowerBlock(b, "comb")
        m1 = b.mul(a1.sig, d1.sig, 8)
        m2 = b.mul(a2.sig, d2.sig, 8)
        blk.if_(c.sig, lambda: blk.assign(y, SigSpec.concat([m1, m2])),
                lambda: blk.assign(y, SigSpec.const(0, 16)))
        blk.finish()
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual(m.share_sets, [])


if __name__ == "__main__":
    unittest.main()
