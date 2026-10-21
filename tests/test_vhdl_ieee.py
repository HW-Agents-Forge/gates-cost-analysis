"""Built-in IEEE model: width-rule table (numeric_std and legacy packages), resolution, math_real, scoping."""
import math
import unittest

from gatecost.frontend.vhdl import ieee


class WidthRuleTable(unittest.TestCase):
    def test_every_row_of_the_table(self):
        for pkg, op, classes, widths, consts, rclass, rwidth in ieee.WIDTH_RULE_TABLE:
            with self.subTest(pkg=pkg, op=op, classes=classes):
                sig = ieee.resolve(op, classes, [pkg])
                self.assertIsNotNone(sig)
                self.assertEqual(sig.package, pkg)
                got_class = sig.result
                self.assertTrue(got_class == rclass or ieee.compatible(rclass, got_class), (got_class, rclass))
                self.assertEqual(ieee.result_width(sig.width, widths, consts), rwidth)

    def check(self, pkg, op, classes, widths, expect_class, expect_width, consts=()):
        sig = ieee.resolve(op, classes, [pkg])
        self.assertIsNotNone(sig, (pkg, op, classes))
        self.assertEqual(sig.result, expect_class)
        self.assertEqual(ieee.result_width(sig.width, widths, consts), expect_width)

    def test_numeric_std_addition_family(self):
        n = "numeric_std"
        self.check(n, "+", ("u", "u"), (8, 4), "u", 8)          # width of the wider operand
        self.check(n, "+", ("u", "nat"), (6, None), "u", 6)     # unsigned + natural: width of the vector
        self.check(n, "+", ("nat", "u"), (None, 7), "u", 7)
        self.check(n, "-", ("s", "int"), (9, None), "s", 9)
        self.check(n, "+", ("s", "s"), (3, 5), "s", 5)

    def test_numeric_std_multiplication_sums_widths(self):
        n = "numeric_std"
        self.check(n, "*", ("u", "u"), (8, 4), "u", 12)
        self.check(n, "*", ("s", "s"), (5, 5), "s", 10)
        self.check(n, "*", ("u", "nat"), (8, None), "u", 16)    # 2 * L'length
        self.check(n, "*", ("int", "s"), (None, 6), "s", 12)

    def test_numeric_std_division_and_remainder(self):
        n = "numeric_std"
        self.check(n, "/", ("u", "u"), (8, 4), "u", 8)
        self.check(n, "/", ("nat", "u"), (None, 4), "u", 4)
        self.check(n, "rem", ("u", "u"), (8, 4), "u", 4)
        self.check(n, "mod", ("s", "int"), (8, None), "s", 8)

    def test_resolution_prefers_exact_class_match_and_package_scope(self):
        sig = ieee.resolve("+", ("u", "u"), ["numeric_std", "std_logic_arith"])
        self.assertEqual(sig.package, "numeric_std")
        sig = ieee.resolve("+", ("u", "u"), ["std_logic_arith"])
        self.assertEqual(sig.package, "std_logic_arith")
        self.assertIsNone(ieee.resolve("+", ("u", "u"), ["std_logic_1164"]))
        self.assertIsNone(ieee.resolve("+", ("slv", "slv"), ["numeric_std"]))

    def test_slv_accepted_where_std_ulogic_vector_expected(self):
        sig = ieee.resolve("and", ("slv", "slv"), ["std_logic_1164"])
        self.assertEqual((sig.result, ieee.result_width(sig.width, (8, 8))), ("suv", 8))
        self.assertTrue(ieee.compatible("suv", "slv"))
        self.assertFalse(ieee.compatible("slv", "suv"))

    def test_legacy_signedness_conventions(self):
        self.check("std_logic_arith", "+", ("u", "s"), (8, 8), "s", 9)
        self.check("std_logic_arith", "+", ("s", "u"), (8, 8), "s", 9)
        self.check("std_logic_arith", "+", ("u", "u"), (8, 8), "u", 8)
        sigu = ieee.resolve("conv_integer", ("slv",), ["std_logic_unsigned"])
        sigs = ieee.resolve("conv_integer", ("slv",), ["std_logic_signed"])
        self.assertEqual((sigu.sem, sigs.sem), ("u", "s"))

    def test_unknown_widths_propagate_as_none(self):
        sig = ieee.resolve("+", ("u", "u"), ["numeric_std"])
        self.assertIsNone(ieee.result_width(sig.width, (8, None)))
        sig = ieee.resolve("resize", ("u", "nat"), ["numeric_std"])
        self.assertIsNone(ieee.result_width(sig.width, (8, None), (None, None)))
        self.assertEqual(ieee.result_width(sig.width, (8, None), (None, 12)), 12)

    def test_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            ieee.result_width(("bogus",), ())

    def test_conversions_and_comparisons(self):
        n = "numeric_std"
        self.check(n, "to_unsigned", ("nat", "nat"), (None, None), "u", 5, (3, 5))
        self.check(n, "to_signed", ("int", "nat"), (None, None), "s", 7, (-3, 7))
        self.check(n, "to_integer", ("u",), (8,), "nat", None)
        self.check(n, "to_integer", ("s",), (8,), "int", None)
        self.check(n, ">=", ("u", "nat"), (8, None), "bool", 1)
        self.check(n, "?=", ("s", "s"), (8, 8), "sl", 1)
        self.check("std_logic_1164", "rising_edge", ("sl",), (None,), "bool", 1)

    def test_shifts_keep_width(self):
        for op in ("shift_left", "shift_right", "rotate_left", "rotate_right"):
            self.check("numeric_std", op, ("u", "nat"), (12, None), "u", 12)
        for op in ("sll", "srl", "rol", "ror"):
            self.check("numeric_std", op, ("s", "int"), (12, None), "s", 12)

    def test_numeric_std_unsigned(self):
        self.check("numeric_std_unsigned", "+", ("slv", "nat"), (8, None), "suv", 8)
        self.check("numeric_std_unsigned", "*", ("slv", "slv"), (3, 5), "suv", 8)
        self.check("numeric_std_unsigned", "to_integer", ("slv",), (8,), "nat", None)

    def test_lookup_helpers(self):
        self.assertTrue(ieee.lookup("RESIZE"))
        self.assertEqual({s.package for s in ieee.lookup("conv_integer")},
                         {"std_logic_arith", "std_logic_unsigned", "std_logic_signed"})
        self.assertEqual(ieee.lookup("nonexistent_fn"), [])

    def test_type_table(self):
        self.assertEqual(ieee.type_class("Unsigned"), "u")
        self.assertEqual(ieee.type_class("std_logic_vector"), "slv")
        self.assertEqual(ieee.TYPES["integer"]["width"], 32)
        self.assertEqual(ieee.TYPES["natural"]["low"], 0)
        self.assertIsNone(ieee.type_class("my_type"))

    def test_scope_packages(self):
        use = [("ieee", "std_logic_1164", "all"), ("ieee", "numeric_std", "all"), ("work", "mine", "all"),
               ("ieee", "numeric_std", "to_integer")]
        self.assertEqual(ieee.scope_packages(use), ["standard", "std_logic_1164", "numeric_std"])

    def test_known_packages_cover_the_spec_list(self):
        for p in ("std_logic_1164", "numeric_std", "numeric_std_unsigned", "math_real", "math_complex",
                  "std_logic_arith", "std_logic_unsigned", "std_logic_signed"):
            self.assertIn(("ieee", p), ieee.KNOWN_PACKAGES)
        for p in ("standard", "textio", "env"):
            self.assertIn(("std", p), ieee.KNOWN_PACKAGES)


class MathRealTests(unittest.TestCase):
    def test_constants(self):
        self.assertAlmostEqual(ieee.MATH_REAL_CONSTANTS["math_pi"], math.pi)
        self.assertAlmostEqual(ieee.MATH_REAL_CONSTANTS["math_log_of_2"], math.log(2))
        self.assertAlmostEqual(ieee.MATH_REAL_CONSTANTS["math_deg_to_rad"], math.pi / 180)

    def test_constant_functions(self):
        self.assertEqual(ieee.eval_math_real("ceil", 2.1), 3.0)
        self.assertEqual(ieee.eval_math_real("floor", -2.1), -3.0)
        self.assertEqual(ieee.eval_math_real("round", 2.5), 3.0)
        self.assertEqual(ieee.eval_math_real("round", -2.5), -3.0)
        self.assertEqual(ieee.eval_math_real("trunc", -2.9), -2.0)
        self.assertEqual(ieee.eval_math_real("log2", 1024.0), 10.0)
        self.assertEqual(ieee.eval_math_real("sqrt", 16.0), 4.0)
        self.assertEqual(ieee.eval_math_real("**", 2.0, 10.0), 1024.0)
        self.assertAlmostEqual(ieee.eval_math_real("log", 8.0, 2.0), 3.0)
        self.assertEqual(ieee.eval_math_real("sign", -4.0), -1.0)
        self.assertEqual(ieee.eval_math_real("realmax", 1.0, 2.0), 2.0)
        self.assertIsNone(ieee.eval_math_real("log2", -1.0))
        self.assertIsNone(ieee.eval_math_real("uniform", 1.0))


if __name__ == "__main__":
    unittest.main()
