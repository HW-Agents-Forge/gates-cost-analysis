"""FRONTEND.md 10.7: width-rule table for numeric_std (and legacy) operators and conversions."""
import unittest

from gatecost.frontend.vhdl import ieee

from tests.test_elab_vhdl_util import probe

DECL = """
signal a8, b8 : unsigned(7 downto 0);
signal a4 : unsigned(3 downto 0);
signal s8, t8 : signed(7 downto 0);
signal s5 : signed(4 downto 0);
signal v8 : std_logic_vector(7 downto 0);
signal v4 : std_logic_vector(3 downto 0);
signal i4 : integer range 0 to 15;
signal n : natural;
signal sl : std_logic;
"""

# expression -> (class, width)
TABLE = [
    ("a8 + b8", "u", 8), ("a8 + a4", "u", 8), ("a4 - a8", "u", 8), ("a8 + 1", "u", 8), ("1 + a8", "u", 8),
    ("a8 + i4", "u", 8), ("a4 + n", "u", 4),
    ("s8 + s5", "s", 8), ("s5 + t8", "s", 8), ("s8 + 1", "s", 8), ("s8 - i4", "s", 8),
    ("a8 * b8", "u", 16), ("a8 * a4", "u", 12), ("s8 * s5", "s", 13), ("a8 * 3", "u", 16), ("3 * a4", "u", 8),
    ("a8 * i4", "u", 16),
    ("a8 / a4", "u", 8), ("a8 / 2", "u", 8), ("a4 / a8", "u", 4),
    ("a8 mod a4", "u", 4), ("a8 rem a4", "u", 4), ("a8 mod 5", "u", 8), ("s8 mod s5", "s", 5),
    ("-s8", "s", 8), ("abs s8", "s", 8),
    ("a8 and b8", "u", 8), ("not a8", "u", 8), ("s8 xor t8", "s", 8), ("a8 or b8", "u", 8),
    ("v8 and v8", "slv", 8), ("not v4", "slv", 4),
    ("shift_left(a8, 2)", "u", 8), ("shift_right(s8, 1)", "s", 8), ("rotate_left(a8, 3)", "u", 8),
    ("a8 sll 2", "u", 8), ("a8 ror 1", "u", 8),
    ("resize(a4, 12)", "u", 12), ("resize(a8, 3)", "u", 3), ("resize(s5, 9)", "s", 9), ("resize(s8, 4)", "s", 4),
    ("to_unsigned(5, 10)", "u", 10), ("to_signed(-5, 9)", "s", 9), ("to_unsigned(i4, 6)", "u", 6),
    ("unsigned(v8)", "u", 8), ("signed(v4)", "s", 4), ("std_logic_vector(a8)", "slv", 8),
    ("std_logic_vector(resize(a4, 8))", "slv", 8), ("std_logic_vector(a8 + 1)", "slv", 8),
    ("a8 & a4", "u", 12), ("a8 & sl", "u", 9), ("sl & a4", "u", 5), ("v4 & v4", "slv", 8),
    ("a8(3 downto 0)", "u", 4), ("v8(7 downto 4)", "slv", 4), ("v8(7 downto 0) & sl", "slv", 9),
    ("a8(2)", "sl", 1), ("v8(i4 mod 8)", "sl", 1),
    ("unsigned(v8) + unsigned(v4)", "u", 8), ("unsigned(v8) * unsigned(v4)", "u", 12),
    ("maximum(a4, a8)", "u", 8),
]
BOOLS = ["a8 = b8", "a8 < a4", "a8 = 5", "s8 >= -3", "i4 /= 0", "a8 > i4", "a8 = to_unsigned(7, 8)", "sl = '1'",
         "v8 = x\"0f\"", "a8 /= b8 and sl = '0'"]


class WidthTable(unittest.TestCase):
    def test_numeric_std_table(self):
        for expr, cls, w in TABLE:
            with self.subTest(expr=expr):
                v, _ = probe(DECL, expr)
                self.assertEqual(v.t.cls, cls, expr)
                self.assertEqual(self.width(v), w, expr)

    @staticmethod
    def width(v):
        return v.sig.width if v.sig is not None else v.t.width

    def test_boolean_results(self):
        for expr in BOOLS:
            with self.subTest(expr=expr):
                v, _ = probe(DECL, expr)
                self.assertEqual(v.t.cls, "bool", expr)
                self.assertEqual(v.sig.width, 1)

    def test_integer_conversions_have_range_widths(self):
        v, _ = probe(DECL, "to_integer(a8)")
        self.assertEqual((v.t.low, v.t.high, v.sig.width, v.t.signed), (0, 255, 8, False))
        v, _ = probe(DECL, "to_integer(s5)")
        self.assertEqual((v.t.low, v.t.high, v.sig.width, v.t.signed), (-16, 15, 5, True))
        v, _ = probe(DECL, "to_integer(a4) + 1")
        self.assertEqual((v.t.low, v.t.high, v.sig.width), (1, 16, 5))
        v, _ = probe(DECL, "to_integer(a4) * to_integer(a4)")
        self.assertEqual((v.t.low, v.t.high, v.sig.width), (0, 225, 8))
        v, _ = probe(DECL, "to_integer(s5) - 20")
        self.assertTrue(v.t.signed)
        self.assertEqual(v.sig.width, 7)

    def test_constant_evaluation(self):
        for expr, val in [("2 ** 10", 1024), ("17 mod 5", 2), ("(-17) mod 5", 3), ("-17 mod 5", -2), ("-17 rem 5", -2), ("7 / 2", 3), ("-7 / 2", -3),
                          ("abs(-4)", 4), ("integer(ceil(log2(real(100))))", 7), ("integer(floor(sqrt(real(50))))", 7),
                          ("a8'length", 8), ("a8'high", 7), ("v4'low", 0), ("s5'left", 4), ("a8'length + 1", 9),
                          ("integer'high", 2 ** 31 - 1), ("natural'low", 0), ("boolean'pos(true)", 1)]:
            with self.subTest(expr=expr):
                v, _ = probe(DECL, expr, use="use ieee.math_real.all;\n")
                self.assertEqual(int(v.c), val)

    def test_table_of_the_ieee_model_is_honoured(self):
        """Every operator row of ieee.WIDTH_RULE_TABLE is reproduced by the elaborator."""
        cls_decl = {"u": "unsigned(%d downto 0)", "s": "signed(%d downto 0)", "slv": "std_logic_vector(%d downto 0)"}
        n = 0
        for pkg, op, classes, widths, consts, rcls, rw in ieee.WIDTH_RULE_TABLE:
            if not (op in ("+", "-", "*", "/", "mod", "rem") and len(classes) == 2):
                continue
            decls, names = [], []
            for i, (c, w) in enumerate(zip(classes, widths)):
                if c in cls_decl:
                    decls.append("signal x%d : %s;" % (i, cls_decl[c] % (w - 1)))
                elif c in ("nat", "int"):
                    decls.append("signal x%d : %s;" % (i, "natural" if c == "nat" else "integer"))
                names.append("x%d" % i)
            use = {"numeric_std": "", "numeric_std_unsigned": "use ieee.numeric_std_unsigned.all;\n",
                   "std_logic_arith": "use ieee.std_logic_arith.all;\n",
                   "std_logic_unsigned": "use ieee.std_logic_unsigned.all;\n",
                   "std_logic_signed": "use ieee.std_logic_signed.all;\n"}[pkg]
            if pkg == "numeric_std_unsigned" or pkg.startswith("std_logic_"):
                use = "use ieee.%s.all;\n" % pkg
                src_use = use
            else:
                src_use = ""
            v, _ = probe("\n".join(decls), "%s %s %s" % (names[0], op, names[1]), use=src_use)
            with self.subTest(pkg=pkg, op=op, classes=classes, widths=widths):
                self.assertEqual(v.sig.width, rw)
            n += 1
        self.assertGreater(n, 10)


class LegacyPackages(unittest.TestCase):
    def test_std_logic_unsigned_arithmetic_keeps_slv(self):
        v, _ = probe("signal a, b : std_logic_vector(7 downto 0);", "a + b", use="use ieee.std_logic_unsigned.all;\n")
        self.assertEqual((v.t.cls, v.sig.width), ("slv", 8))
        v, _ = probe("signal a : std_logic_vector(7 downto 0); signal b : std_logic_vector(3 downto 0);", "a * b",
                     use="use ieee.std_logic_unsigned.all;\n")
        self.assertEqual(v.sig.width, 12)

    def test_std_logic_arith_mixed_signedness(self):
        d = "signal u : unsigned(7 downto 0); signal s : signed(7 downto 0);"
        v, _ = probe(d, "u + s", use="use ieee.std_logic_arith.all;\n")
        self.assertEqual((v.t.cls, v.sig.width), ("s", 9))
        v, _ = probe(d, "u + u", use="use ieee.std_logic_arith.all;\n")
        self.assertEqual((v.t.cls, v.sig.width), ("u", 8))
        v, _ = probe(d, "conv_std_logic_vector(5, 12)", use="use ieee.std_logic_arith.all;\n")
        self.assertEqual((v.t.cls, v.sig.width), ("slv", 12))

    def test_signed_package_makes_vectors_signed(self):
        from tests.test_elab_vhdl_util import module, unit
        m, r, d = module(unit("a, b : in std_logic_vector(7 downto 0); y : out std_logic", body="y <= '1' when a < b else '0';",
                              use="use ieee.std_logic_signed.all;\n"))
        lt = [n for n in m.nodes if n.op == "LT"]
        self.assertEqual(len(lt), 1)
        self.assertTrue(lt[0].attrs["sa"])


if __name__ == "__main__":
    unittest.main()
