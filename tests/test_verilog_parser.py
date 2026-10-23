"""Parser: one fixture per construct row of FRONTEND.md section 3."""
import unittest

from gatecost.frontend.verilog import ast as A
from gatecost.frontend.verilog import parse_text
from tests.verilog_fixtures import CONSTRUCTS


def parse(name):
    d = parse_text(CONSTRUCTS[name], name + ".sv")
    bad = [x for x in d.diagnostics if x.severity != "info"]
    assert not bad, [(x.code, x.line, x.column, x.message) for x in bad]
    return d


def items(m, cls):
    return [i for i in m.items if isinstance(i, cls)]


def walk_all(m):
    return list(A.walk(m))


def kinds(m, cls):
    return [n for n in walk_all(m) if isinstance(n, cls)]


class Constructs(unittest.TestCase):
    def test_every_fixture_parses_cleanly(self):
        for name in CONSTRUCTS:
            d = parse(name)
            for m in d.modules + d.interfaces + d.packages:
                self.assertFalse(m.incomplete, f"{name}: {m.name}")

    def test_module_ansi(self):
        m = parse("module_ansi").modules[0]
        self.assertTrue(m.ansi)
        self.assertEqual([p.name for p in m.params], ["W", "D"])
        self.assertEqual(m.params[1].dtype.kind, "int")
        self.assertEqual([(p.direction, p.name) for p in m.ports],
                         [("input", "clk"), ("input", "a"), ("input", "b"), ("output", "q"), ("inout", "io")])
        self.assertEqual(m.ports[0].net_type, "wire")
        self.assertIsInstance(m.ports[2].dtype, A.ImplicitType)   # b inherits [W-1:0] from a
        self.assertEqual(m.ports[3].dtype.kind, "reg")
        self.assertTrue(items(m, A.ParamDecl)[0].local)

    def test_module_nonansi(self):
        m = parse("module_nonansi").modules[0]
        self.assertFalse(m.ansi)
        self.assertEqual([(p.direction, p.name) for p in m.ports], [(None, "a"), (None, "b"), (None, "q")])
        decl = [(p.direction, p.name) for p in items(m, A.Port)]
        self.assertEqual(decl, [("input", "a"), ("input", "b"), ("output", "q")])
        self.assertEqual(items(m, A.Port)[2].dtype.kind, "reg")

    def test_data_types(self):
        m = parse("data_types").modules[0]
        by = {i.name: i for i in m.items}
        self.assertIsInstance(by["w"], A.NetDecl)
        self.assertTrue(by["ws"].dtype.signed)
        self.assertEqual(by["r"].dtype.kind, "reg")
        self.assertTrue(by["rs"].dtype.signed)
        self.assertEqual(len(by["pk"].dtype.dims), 2)
        self.assertEqual([by[n].dtype.kind for n in ("b", "by", "si", "i", "li", "ig", "t")],
                         ["bit", "byte", "shortint", "int", "longint", "integer", "time"])
        self.assertEqual([by[n].net_type for n in ("tr", "wa", "g0", "g1")], ["tri", "wand", "supply0", "supply1"])

    def test_typedef_enum_struct(self):
        d = parse("typedef_enum_struct")
        p = d.packages[0]
        td = {t.name: t for t in items(p, A.Typedef)}
        e = td["st_t"].dtype
        self.assertIsInstance(e, A.EnumType)
        self.assertEqual([m.name for m in e.members], ["A", "B", "C"])
        self.assertEqual(e.members[1].init.value, 2)
        self.assertEqual(e.base.kind, "logic")
        self.assertIsNone(td["anon_t"].dtype.base)
        s = td["byte_t"].dtype
        self.assertTrue(s.packed and not s.is_union)
        self.assertEqual([m.name for m in s.members], ["hi", "lo"])
        self.assertTrue(td["u_t"].dtype.is_union)
        self.assertTrue(td["sp_t"].dtype.signed)
        m = d.modules[0]
        self.assertIsInstance(items(m, A.VarDecl)[0].dtype, A.NamedType)
        self.assertIsInstance(items(m, A.VarDecl)[0].dtype.name, A.ScopedName)

    def test_unpacked_arrays(self):
        m = parse("unpacked_arrays").modules[0]
        by = {i.name: i for i in m.items if hasattr(i, "name")}
        self.assertEqual(by["mem"].dims[0].kind, "range")
        self.assertEqual(by["rf"].dims[0].kind, "size")
        self.assertEqual(len(by["two"].dims), 2)
        asg = kinds(m, A.ProcAssign)
        self.assertIsInstance(asg[0].lhs, A.Index)

    def test_assign_and_always(self):
        m = parse("assign_always").modules[0]
        self.assertEqual(len(items(m, A.ContAssign)), 3)
        al = items(m, A.Always)
        self.assertEqual([a.kind for a in al],
                         ["always", "always", "always", "always", "always_ff", "always_comb", "always_latch"])
        self.assertFalse(al[0].event.star)
        self.assertEqual((al[0].event.items[0].edge, al[0].event.items[0].expr.name), ("posedge", "clk"))
        self.assertTrue(al[1].event.star and al[2].event.star)
        self.assertEqual([i.edge for i in al[3].event.items], [None, None])
        self.assertEqual([i.edge for i in al[4].event.items], ["posedge", "negedge"])
        self.assertIsNone(al[5].event)
        self.assertFalse(al[0].body.blocking)

    def test_control_flow(self):
        m = parse("control").modules[0]
        blk = items(m, A.Always)[0].body
        stmts = blk.items
        self.assertIsInstance(stmts[0], A.If)
        self.assertIsInstance(stmts[0].else_, A.If)                    # else-if chain
        self.assertIsInstance(stmts[0].else_.else_, A.ProcAssign)
        self.assertEqual([s.qualifier for s in stmts[1:3]], ["unique", "priority"])
        cases = [s for s in stmts if isinstance(s, A.Case)]
        self.assertEqual([c.kind for c in cases], ["case", "casez", "casex", "case", "case", "case"])
        self.assertEqual([c.qualifier for c in cases], [None, None, None, "unique", "priority", None])
        self.assertEqual(len(cases[0].items), 3)
        self.assertEqual(len(cases[0].items[1].labels), 2)
        self.assertIsNone(cases[0].items[2].labels)
        self.assertEqual(cases[1].items[0].labels[0].digits, "1??")
        self.assertTrue(cases[5].inside)
        self.assertIsInstance(cases[5].items[0].labels[0], A.ValueRange)

    def test_loops_and_generate(self):
        m = parse("loops_generate").modules[0]
        gf = items(m, A.GenFor)
        self.assertEqual(len(gf), 2)
        self.assertEqual(gf[0].body.label, "g")
        self.assertIsInstance(gf[0].init[0], A.ProcAssign)
        self.assertIsInstance(gf[1].init[0], A.Genvar)
        gi = items(m, A.GenIf)
        self.assertEqual(len(gi), 2)
        self.assertEqual(gi[0].then.label, "big")
        self.assertEqual(gi[0].else_.label, "small")
        self.assertIsNone(gi[1].else_)
        self.assertIsInstance(gi[1].then.items[0], A.ContAssign)
        gc = items(m, A.GenCase)[0]
        self.assertEqual([i.body.label for i in gc.items], ["c1", "c2", "cd"])
        self.assertIsNone(gc.items[2].labels)
        al = items(m, A.Always)
        fl = al[0].body
        self.assertIsInstance(fl, A.For)
        self.assertEqual(fl.init[0].name, "k")
        self.assertEqual(fl.step[0].op, "+=")                          # k++ normalised
        self.assertTrue(any(isinstance(s, A.While) for s in al[1].body.items))
        self.assertTrue(any(isinstance(s, A.Repeat) for s in al[1].body.items))

    def test_functions_and_tasks(self):
        m = parse("functions_tasks").modules[0]
        f = {x.name: x for x in items(m, A.Function)}
        self.assertTrue(f["f"].automatic)
        self.assertEqual([p.name for p in f["f"].ports], ["x", "y"])
        self.assertEqual([p.direction for p in f["f"].ports], ["input", "input"])
        self.assertIsInstance(f["f"].body[0], A.ProcAssign)
        self.assertIsInstance(f["clog"].body[0], A.VarDecl)
        self.assertEqual(f["g"].ports[0].direction, "input")
        self.assertIsInstance(f["g"].body[0], A.Return)
        self.assertEqual(f["h"].rettype.kind, "void")
        self.assertIsInstance(f["k"].rettype.name, A.ScopedName)
        self.assertTrue(f["tk"].is_task)
        self.assertEqual([p.direction for p in f["tk"].ports], ["input", "output"])
        self.assertEqual([p.name for p in f["old"].ports], ["a"])      # 1995-style port declaration
        self.assertFalse(any(x.nonsynth for x in f.values()))

    def test_packages_import(self):
        d = parse("packages_import")
        pk = d.packages[0]
        self.assertEqual(pk.name, "pk")
        self.assertEqual([(p.name, p.local) for p in items(pk, A.ParamDecl)], [("W", True), ("P", False)])
        unit = [p for p in d.packages if p.name == "$unit"][0]
        self.assertEqual([(i.pkg, i.name) for i in items(unit, A.Import)], [("pk", "*")])
        m = d.modules[0]
        self.assertEqual([(i.pkg, i.name) for i in items(m, A.Import)], [("pk", "*"), ("pk", "W"), ("pk", "f"), ("pk", "P")])
        sn = [n for n in walk_all(m) if isinstance(n, A.ScopedName)]
        self.assertTrue(all(n.scope == "pk" for n in sn))
        self.assertIn("f", [n.name for n in sn])

    def test_system_calls(self):
        m = parse("system_calls").modules[0]
        calls = {n.name: n for n in kinds(m, A.SysCall)}
        self.assertEqual(set(calls), {"$clog2", "$bits", "$signed", "$unsigned", "$countones", "$max", "$min"})
        bits = [n for n in kinds(m, A.SysCall) if n.name == "$bits"]
        self.assertIsInstance(bits[0].args[0], A.IntType)               # type argument
        self.assertIsInstance(bits[1].args[0], A.Ident)

    def test_streaming_inside_patterns(self):
        m = parse("streaming_inside_patterns").modules[0]
        st = kinds(m, A.Streaming)
        self.assertEqual([s.op for s in st], ["<<", ">>", "<<"])
        self.assertEqual(st[0].size.value, 8)
        self.assertIsNone(st[1].size)
        self.assertIsInstance(st[2].items[0], A.StreamWith)
        ins = kinds(m, A.Inside)[0]
        self.assertEqual([type(i).__name__ for i in ins.items], ["Number", "Number", "ValueRange"])
        pats = kinds(m, A.AssignPattern)
        self.assertEqual(pats[0].items[0].key, "default")
        self.assertEqual(len(pats[1].items), 4)
        self.assertIsInstance(pats[2].items[0], A.Replicate)
        self.assertEqual(pats[3].items[0].key.name, "a")
        self.assertEqual(pats[4].prefix.name, "s_t")
        self.assertEqual(kinds(m, A.Fill)[-1].bit, "1")

    def test_casts(self):
        m = parse("casts").modules[0]
        casts = kinds(m, A.Cast)
        self.assertEqual(len(casts), 9)
        t = [type(c.target).__name__ + ":" + (getattr(c.target, "kind", None) or getattr(c.target, "name", None) or
                                                str(getattr(c.target, "value", ""))) for c in casts[:5]]
        self.assertEqual(t, ["Number:8", "OtherType:signed", "OtherType:unsigned", "IntType:int", "Ident:my_t"])
        self.assertIsInstance(casts[6].target, A.Binary)               # (W+1)'(x)
        self.assertIsInstance(casts[8].target, A.ScopedName)

    def test_interfaces_modports(self):
        d = parse("interfaces_modports")
        i = d.interfaces[0]
        self.assertEqual(i.name, "bus_if")
        self.assertEqual([p.name for p in i.params], ["AW"])
        mp = {x.name: x for x in items(i, A.Modport)}
        self.assertEqual([(p.direction, p.name) for p in mp["mst"].ports],
                         [("output", "addr"), ("output", "valid"), ("input", "ready"), ("input", "clk")])
        self.assertEqual(len(items(i, A.Function)), 1)
        top = [m for m in d.modules if m.name == "top"][0]
        inst = {x.name: x for x in items(top, A.Instance)}
        self.assertEqual(inst["b"].module, "bus_if")
        self.assertEqual(inst["b"].params[0].name, "AW")
        self.assertEqual(inst["u"].conns[1].kind, "implicit")
        self.assertIsInstance(inst["u"].conns[0].expr, A.Member)
        child = [m for m in d.modules if m.name == "child"][0]
        self.assertEqual([(p.iface, p.modport, p.name) for p in child.ports[:3]],
                         [("bus_if", "mst", "bus"), ("bus_if", None, "other"), ("interface", "slv", "gen")])

    def test_parameterized_types(self):
        m = parse("parameterized_types").modules[0]
        self.assertTrue(m.params[0].is_type)
        self.assertIsInstance(m.params[0].init, A.IntType)
        self.assertIsInstance(m.params[1].dtype, A.NamedType)
        self.assertEqual(m.ports[0].dtype.name.name, "T")
        td = items(m, A.Typedef)[0]
        self.assertEqual(td.dtype.name.name, "T")
        inst = items(m, A.Instance)
        self.assertIsInstance(inst[0].params[0].value, A.IntType)
        self.assertIsInstance(inst[1].params[0].value, A.IntType)

    def test_tristate(self):
        m = parse("tristate").modules[0]
        ca = items(m, A.ContAssign)
        self.assertIsInstance(ca[0].rhs, A.Ternary)
        self.assertEqual(ca[0].rhs.else_.digits, "z")
        self.assertIsInstance(ca[1].rhs.else_, A.Replicate)
        self.assertEqual(items(m, A.GateInst)[0].gate, "bufif1")
        self.assertEqual(m.ports[0].direction, "inout")

    def test_defparam(self):
        m = parse("defparam").modules[0]
        dp = items(m, A.Defparam)
        self.assertEqual(len(dp), 3)
        self.assertIsInstance(dp[0].path, A.Member)
        self.assertEqual(dp[1].path.base.base.name, "u1")

    def test_gates(self):
        m = parse("gates").modules[0]
        g = items(m, A.GateInst)
        self.assertEqual([x.gate for x in g], ["and", "nand", "or", "or", "xor", "not", "buf", "notif0", "nmos", "pullup", "and"])
        self.assertEqual([x.name for x in g[:4]], ["a1", None, "o1", "o2x"])
        self.assertEqual(len(g[1].terminals), 4)
        self.assertEqual(g[-1].dims[0].kind, "range")

    def test_instances(self):
        m = parse("instances").modules[0]
        ins = items(m, A.Instance)
        self.assertEqual([i.name for i in ins], ["u0", "u1", "u2", "u3", "u4", "u5", "u6"])
        self.assertEqual([c.kind for c in ins[0].conns], ["positional"] * 3)
        self.assertEqual([c.kind for c in ins[1].conns], ["named"] * 3)
        self.assertIsNone(ins[1].conns[2].expr)
        self.assertIsNone(ins[1].params[0].name)
        self.assertEqual([p.name for p in ins[2].params], ["W", "D"])
        self.assertEqual([c.kind for c in ins[2].conns], ["wildcard"])
        self.assertEqual([c.kind for c in ins[3].conns], ["implicit", "implicit"])
        self.assertEqual(ins[4].dims[0].kind, "range")
        self.assertEqual([c.kind for c in ins[5].conns], ["positional"] * 3)
        self.assertIsNone(ins[5].conns[1].expr)

    def test_hierarchical_references(self):
        m = parse("hier_refs").modules[0]
        a = items(m, A.ContAssign)
        self.assertIsInstance(a[0].rhs, A.Member)
        self.assertIsInstance(a[1].rhs, A.Member)
        self.assertIsInstance(a[1].rhs.base, A.Index)

    def test_skipped_constructs(self):
        d = parse_text(CONSTRUCTS["skipped"], "s.sv")
        self.assertEqual([x for x in d.diagnostics if x.severity != "info"], [])
        info = [x.message for x in d.diagnostics]
        for needle in ("initial block ignored", "final block ignored", "property declaration ignored",
                       "sequence declaration ignored", "assert ignored", "assume ignored", "cover ignored",
                       "covergroup declaration ignored", "clocking declaration ignored", "specify block ignored",
                       "class declaration ignored", "program declaration ignored", "checker declaration ignored",
                       "DPI import ignored", "bind ignored", "always with delay ignored",
                       "system task $display ignored",
                       "assert statement ignored"):
            self.assertTrue(any(needle in m for m in info), needle)
        self.assertTrue(all(x.code == "IGNORED_REGION" and not x.affects_cost for x in d.diagnostics))
        self.assertEqual(d.udps, ())                 # lowered to a module (kind "primitive")
        self.assertEqual([(x.name, x.kind) for x in d.modules[1:]], [("udp", "primitive")])
        m = d.modules[0]
        self.assertEqual([type(i).__name__ for i in m.items], ["Always"])
        self.assertEqual(len(m.items[0].body.items), 1)                  # only q <= #1 d remains
        self.assertFalse(m.items[0].body.items[0].blocking)

    def test_attributes_and_pragmas(self):
        m = parse("attributes").modules[0]
        cases = kinds(m, A.Case)
        self.assertEqual([(c.full_case, c.parallel_case) for c in cases], [(False, True), (True, True)])

    def test_numbers(self):
        m = parse("numbers").modules[0]
        vals = {p.name: p.init for p in items(m, A.ParamDecl)}
        self.assertEqual((vals["A"].size, vals["A"].base, vals["A"].value), (8, "h", 255))
        self.assertEqual(vals["B"].value, 10)
        self.assertEqual((vals["C"].size, vals["C"].base, vals["C"].value), (None, "o", 63))
        self.assertEqual(vals["D"].value, 1000)
        self.assertTrue(vals["E"].signed)
        self.assertIsNone(vals["F"].value)
        self.assertIsInstance(vals["H"], A.Real)
        self.assertEqual(vals["I"].value, 2000.0)
        w = [i for i in m.items if isinstance(i, A.NetDecl)][0]
        self.assertEqual(w.init.digits, "1?0z")

    def test_operator_precedence(self):
        d = parse_text("module m; assign y = a + b * c ** d << 1 == e & f ^ g | h && i || j ? k : l; endmodule")
        e = d.modules[0].items[0].rhs

        def shape(n):
            if isinstance(n, A.Ternary):
                return ("?", shape(n.cond), shape(n.then), shape(n.else_))
            if isinstance(n, A.Binary):
                return (n.op, shape(n.left), shape(n.right))
            return getattr(n, "name", None) or str(n.value)
        self.assertEqual(shape(e), ("?", ("||", ("&&", ("|", ("^", ("&", ("==", ("<<", ("+", "a", ("*", "b", ("**", "c", "d"))), "1"), "e"), "f"), "g"), "h"), "i"), "j"), "k", "l"))

    def test_unary_and_ternary_right_assoc(self):
        d = parse_text("module m; assign y = a ? b : c ? d : e; assign z = - ~ & a; assign w = a - -b; endmodule")
        a, z, w = d.modules[0].items
        self.assertIsInstance(a.rhs.else_, A.Ternary)
        self.assertEqual([z.rhs.op, z.rhs.operand.op, z.rhs.operand.operand.op], ["-", "~", "&"])
        self.assertEqual(w.rhs.right.op, "-")

    def test_select_modes(self):
        d = parse_text("module m; assign y = a[3:0] + a[i +: 4] + a[j -: 2] + a[k][1]; endmodule")
        sel = kinds(d.modules[0], A.Select)
        self.assertEqual([s.mode for s in sel], [":", "+:", "-:"])
        self.assertIsInstance(kinds(d.modules[0], A.Index)[0].base, A.Index)

    def test_misc_statements(self):
        m = parse("stmts_misc").modules[0]
        blk = items(m, A.Always)[0].body
        self.assertEqual(blk.label, "named")
        ops = [s.op for s in blk.items if isinstance(s, A.ProcAssign)]
        self.assertEqual(ops[:8], ["+=", "-=", "*=", "<<=", ">>>=", "|=", "&=", "^="][:8])
        self.assertTrue(any(isinstance(s, A.Foreach) for s in blk.items))
        self.assertTrue(any(isinstance(s, A.DoWhile) for s in blk.items))
        self.assertTrue(any(isinstance(s, A.Forever) for s in blk.items))
        self.assertTrue(any(isinstance(s, A.CallStmt) for s in blk.items))
        self.assertIsInstance([s for s in blk.items if isinstance(s, A.ProcAssign)][-1].lhs, A.Concat)

    def test_nonsynth_function_marked(self):
        d = parse_text("module m; function f(input a); begin #5 f = a; end endfunction "
                       "task t; $display(\"x\"); endtask endmodule")
        fs = items(d.modules[0], A.Function)
        self.assertTrue(all(f.nonsynth and f.body == () for f in fs))
        self.assertEqual(len([x for x in d.diagnostics if "non-synthesizable" in x.message]), 2)

    def test_legacy_verilog_and_labels(self):
        m = parse("legacy_verilog").modules[0]
        gf = items(m, A.GenFor)
        self.assertEqual([g.body.label for g in gf], ["g", "lbl"])
        self.assertEqual(len(m.ports), 4)
        blk = [a for a in items(m, A.Always) if isinstance(a.body, A.Block)][0]
        self.assertEqual(blk.body.label, "blk")
        self.assertIsInstance(blk.body.items[0], A.VarDecl)

    def test_default_nettype(self):
        d = parse_text("`default_nettype none\nmodule a; endmodule\n`default_nettype wire\nmodule b; endmodule\n")
        self.assertEqual([m.default_nettype for m in d.modules], ["none", "wire"])

    def test_duplicate_module(self):
        d = parse_text("module a; wire x; endmodule\nmodule a; wire y; endmodule\n")
        self.assertEqual([(x.code, x.line) for x in d.diagnostics], [("DUPLICATE_MODULE", 2)])
        self.assertEqual(len(d.modules), 2)
        self.assertEqual(d.module_map()["a"].items[0].name, "y")

    def test_deep_expression_does_not_overflow(self):
        d = parse_text("module m; assign y = " + "a + " * 20000 + "a; endmodule")
        self.assertEqual([x for x in d.diagnostics if x.severity != "info"], [])
        d = parse_text("module m; assign y = " + "(" * 5000 + "a" + ")" * 5000 + "; endmodule")
        self.assertEqual([x.code for x in d.diagnostics], ["PARSE_ERROR"])
        self.assertIn("nesting too deep", d.diagnostics[0].message)


class ElaborationTasks(unittest.TestCase):
    def test_elaboration_system_tasks_are_legal_generate_items(self):
        src = """module a #(parameter int N = 4)(input logic x, output logic y);
  if (N < 2) begin : g_chk
    $error("N must be >= 2, got %0d", N);
  end
  $info("elaborating");
  $warning("w"); $fatal(1, "f");
  assign y = x;
endmodule
"""
        d = parse_text(src, "a.sv")
        self.assertEqual([x.code for x in d.diagnostics if x.severity != "info"], [])
        self.assertEqual(len(d.modules), 1)


class TypedPorts(unittest.TestCase):
    def ports(self, src):
        d = parse_text(src, "a.sv")
        self.assertEqual([x.code for x in d.diagnostics if x.severity != "info"], [])
        return d

    def test_function_arguments_each_with_a_type(self):
        d = self.ports("package p; function automatic int max(int a, int b); return a > b ? a : b; endfunction endpackage\n")
        (pk,) = [x for x in d.packages if x.name == "p"]
        (f,) = [x for x in pk.items if isinstance(x, A.Function)]
        self.assertEqual([p.name for p in f.ports], ["a", "b"])

    def test_module_port_with_new_type_inherits_direction(self):
        d = self.ports("module m(input logic a, logic b, input my_t c, my_t e, output [3:0] y, z); endmodule\n")
        got = [(p.name, p.direction, p.iface) for p in d.modules[0].ports]
        self.assertEqual(got, [("a", "input", None), ("b", "input", None), ("c", "input", None),
                               ("e", "input", None), ("y", "output", None), ("z", "output", None)])


if __name__ == "__main__":
    unittest.main()
