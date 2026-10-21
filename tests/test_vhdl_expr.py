"""Expression parsing: precedence table, all operator families, attributes, aggregates, slices."""
import unittest

from tests._vhdl_helpers import expr, show


class ExprTests(unittest.TestCase):
    def test_precedence_levels(self):
        cases = {
            "a + b * c": "(a + (b * c))",
            "a * b + c": "((a * b) + c)",
            "a + b + c": "((a + b) + c)",
            "a & b & c": "((a & b) & c)",
            "a + b & c": "((a + b) & c)",
            "a = b and c = d": "((a = b) and (c = d))",
            "a and b or c": "((a and b) or c)",
            "a < b + 1": "(a < (b + 1))",
            "a sll 2 + 1": "(a sll (2 + 1))",
            "a + 1 sll 2 = b": "(((a + 1) sll 2) = b)",
            "2 ** 3 * 4": "((2 ** 3) * 4)",
            "a mod b rem c": "((a mod b) rem c)",
            "a xor b xnor c": "((a xor b) xnor c)",
            "a nand b": "(a nand b)",
            "a nor b": "(a nor b)",
        }
        for src, expect in cases.items():
            self.assertEqual(show(expr(src)), expect, src)

    def test_unary_operators_bind_correctly(self):
        cases = {
            "-a + b": "((- a) + b)",
            "-a * b": "(- (a * b))",
            "- a ** 2": "(- (a ** 2))",
            "not a and b": "((not a) and b)",
            "not a = b": "((not a) = b)",
            "abs a + 1": "((abs a) + 1)",
            "abs(a) ** 2": "((abs a) ** 2)",
            "+a": "(+ a)",
            "a - -b": "(a - (- b))",
        }
        for src, expect in cases.items():
            self.assertEqual(show(expr(src)), expect, src)

    def test_vhdl_2008_matching_and_reduction_operators(self):
        for op in ("?=", "?/=", "?<", "?<=", "?>", "?>="):
            self.assertEqual(show(expr("a %s b" % op)), "(a %s b)" % op)
        self.assertEqual(show(expr("and v")), "(and v)")
        self.assertEqual(show(expr("xor v or w")), "((xor v) or w)")
        self.assertEqual(show(expr("?? a")), "(?? a)")

    def test_shift_and_rotate_operators(self):
        for op in ("sll", "srl", "sla", "sra", "rol", "ror"):
            self.assertEqual(show(expr("a %s 2" % op)), "(a %s 2)" % op)

    def test_all_relational_operators(self):
        for op in ("=", "/=", "<", "<=", ">", ">="):
            self.assertEqual(show(expr("(a %s b)" % op)), "(a %s b)" % op)

    def test_literals(self):
        self.assertEqual(show(expr("16#FF#")), "255")
        self.assertEqual(show(expr("'1'")), "'1'")
        self.assertEqual(show(expr("\"0101\"")), "'0101'")
        self.assertEqual(show(expr('x"F0"')), "bits(None,x,None,F0)")
        self.assertEqual(show(expr('8UX"F0"')), "bits(8,x,u,F0)")
        self.assertEqual(show(expr("1.5")), "1.5")
        self.assertEqual(show(expr("5 ns")), "5 ns")

    def test_index_slice_call_and_conversion(self):
        self.assertEqual(show(expr("a(3)")), "a[3]")
        self.assertEqual(show(expr("a(i + 1)(2)")), "a[(i + 1)][2]")
        self.assertEqual(show(expr("a(7 downto 4)")), "a{7 downto 4}")
        self.assertEqual(show(expr("a(0 to n - 1)")), "a{0 to (n - 1)}")
        self.assertEqual(show(expr("f(x, y => 2)")), "f[x, y=>2]")
        self.assertEqual(show(expr("resize(unsigned(a), 8)")), "resize[unsigned[a], 8]")
        self.assertEqual(show(expr("a(3 downto 0) & b(1)")), "(a{3 downto 0} & b[1])")

    def test_selected_names_and_attributes(self):
        self.assertEqual(show(expr("rec.field(3).sub")), "rec.field[3].sub")
        self.assertEqual(show(expr("work.pkg.c")), "work.pkg.c")
        self.assertEqual(show(expr("a'length")), "a'length")
        self.assertEqual(show(expr("a'length - 1")), "(a'length - 1)")
        self.assertEqual(show(expr("a'range(2)")), "a'range(2)")
        self.assertEqual(show(expr("t'pos(c) + 1")), "(t'pos(c) + 1)")
        self.assertEqual(show(expr("clk'event and clk = '1'")), "(clk'event and (clk = '1'))")
        self.assertEqual(show(expr("a'high")), "a'high")

    def test_aggregates(self):
        self.assertEqual(show(expr("(others => '0')")), "agg(others=>'0')")
        self.assertEqual(show(expr("(a, b, c)")), "agg(a, b, c)")
        self.assertEqual(show(expr("(0 => '1', others => '0')")), "agg(0=>'1', others=>'0')")
        self.assertEqual(show(expr("(1 | 3 => x, others => y)")), "agg(1|3=>x, others=>y)")
        self.assertEqual(show(expr("(0 to 3 => x, others => y)")), "agg(0 to 3=>x, others=>y)")
        self.assertEqual(show(expr("(a => 1, b => (c => 2))")), "agg(a=>1, b=>agg(c=>2))")

    def test_parenthesized_expression_is_unwrapped(self):
        self.assertEqual(show(expr("(a + b) * c")), "((a + b) * c)")
        self.assertEqual(show(expr("((a))")), "a")

    def test_qualified_expressions(self):
        self.assertEqual(show(expr("unsigned'(a + b)")), "unsigned'((a + b))")
        self.assertEqual(show(expr("std_logic_vector'(\"01\")")), "std_logic_vector'('01')")
        self.assertEqual(show(expr("t'(others => '0')")), "t'(agg(others=>'0'))")

    def test_operator_symbol_call(self):
        self.assertEqual(show(expr('"+"(a, b)')), '"+"[a, b]')

    def test_long_operator_chain_is_iterative(self):
        e = expr(" + ".join(["a"] * 3000))
        depth = 0
        while hasattr(e, "left"):
            e = e.left
            depth += 1
        self.assertEqual(depth, 2999)

    def test_nesting_limit_gives_diagnostic_not_crash(self):
        from tests._vhdl_helpers import parse
        src = "architecture a of e is begin x <= %s1%s; y <= 2; end;" % ("(" * 3000, ")" * 3000)
        r = parse(src)
        self.assertTrue(any(d.code in ("LIMIT_REACHED", "PARSE_ERROR") for d in r.diagnostics))


if __name__ == "__main__":
    unittest.main()
