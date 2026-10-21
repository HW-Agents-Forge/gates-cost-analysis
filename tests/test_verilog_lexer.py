"""Lexer: token streams for every literal form (FRONTEND.md section 9)."""
import unittest

from gatecost.frontend.verilog.lexer import lex, parse_number


def toks(text):
    return lex(text).stream()


class NumberLiterals(unittest.TestCase):
    def assertNum(self, text, expect):
        self.assertEqual(toks(text), [("NUM", text.strip())] if expect is None else expect)

    def test_plain_decimal(self):
        self.assertEqual(toks("42 1_000"), [("NUM", "42"), ("NUM", "1_000")])

    def test_sized_bases(self):
        for t in ("8'hFF", "4'b1010", "6'o77", "10'd999", "16'hDEAD_BEEF", "8'HaB", "3'B101"):
            self.assertEqual(toks(t), [("NUM", t)], t)

    def test_signed_forms(self):
        for t in ("4'sb1010", "8'sd5", "'sh7F", "16'SH10"):
            self.assertEqual(toks(t), [("NUM", t)], t)

    def test_unsized_based(self):
        for t in ("'h0", "'b1", "'d12", "'o7"):
            self.assertEqual(toks(t), [("NUM", t)])

    def test_xz_digits(self):
        for t in ("4'bx", "8'hz", "4'b1?0z", "8'hx_z", "'bx", "4'bXZ01"):
            self.assertEqual(toks(t), [("NUM", t)], t)

    def test_space_between_parts(self):
        self.assertEqual(toks("8 'hFF"), [("NUM", "8 'hFF")])
        self.assertEqual(toks("8'h FF"), [("NUM", "8'h FF")])

    def test_fill_literals(self):
        self.assertEqual(toks("'0 '1 'x 'z 'X 'Z"),
                         [("FILL", "'0"), ("FILL", "'1"), ("FILL", "'x"), ("FILL", "'z"), ("FILL", "'X"), ("FILL", "'Z")])

    def test_fill_vs_based(self):
        self.assertEqual(toks("'b1 '1"), [("NUM", "'b1"), ("FILL", "'1")])

    def test_real_and_time(self):
        self.assertEqual(toks("1.5 2.0e-3 3e8 1_0.5"), [("REAL", "1.5"), ("REAL", "2.0e-3"), ("REAL", "3e8"), ("REAL", "1_0.5")])
        self.assertEqual(toks("10ns 1.5us"), [("TIME", "10ns"), ("TIME", "1.5us")])

    def test_cast_ticks(self):
        self.assertEqual(toks("8'(x)"), [("NUM", "8"), ("'(", "'("), ("ID", "x"), (")", ")")])
        self.assertEqual(toks("signed'(x)"), [("signed", "signed"), ("'(", "'("), ("ID", "x"), (")", ")")])
        self.assertEqual(toks("'{1}"), [("'{", "'{"), ("NUM", "1"), ("}", "}")])

    def test_parse_number_fields(self):
        self.assertEqual(parse_number("8'hFF"), (8, "h", False, "ff", 255, True))
        self.assertEqual(parse_number("4'sb1_010"), (4, "b", True, "1010", 10, True))
        self.assertEqual(parse_number("'o17"), (None, "o", False, "17", 15, True))
        self.assertEqual(parse_number("4'b1x0z"), (4, "b", False, "1x0z", None, True))
        self.assertEqual(parse_number("8 'h F_F"), (8, "h", False, "ff", 255, True))
        self.assertEqual(parse_number("1_000"), (None, "d", True, "1000", 1000, False))
        self.assertEqual(parse_number("12'd9")[4], 9)

    def test_huge_number_does_not_raise(self):
        n = parse_number("9" * 6000)
        self.assertIsNone(n[4]) if n[4] is None else self.assertGreater(n[4], 0)


class Tokens(unittest.TestCase):
    def test_identifiers_keywords(self):
        self.assertEqual(toks("module foo_1$ x"), [("module", "module"), ("ID", "foo_1$"), ("ID", "x")])

    def test_escaped_identifier(self):
        self.assertEqual(toks("\\bus[0] x"), [("ID", "bus[0]"), ("ID", "x")])

    def test_system_identifiers(self):
        self.assertEqual(toks("$clog2(N)"), [("SYSID", "$clog2"), ("(", "("), ("ID", "N"), (")", ")")])

    def test_operators_longest_match(self):
        src = "<<<= >>>= === !== ==? !=? <<< >>> << >> <= >= == != && || ** ~& ~| ~^ ^~ +: -: :: ++ -- += -= -> .*"
        self.assertEqual([t for _, t in toks(src)], src.split())

    def test_single_char_ops(self):
        self.assertEqual([t for _, t in toks("+ - * / % & | ^ ~ ! < > = ? : ; , . ( ) [ ] { } # @")],
                         "+ - * / % & | ^ ~ ! < > = ? : ; , . ( ) [ ] { } # @".split())

    def test_event_star(self):
        self.assertEqual([t for _, t in toks("@(*)")], ["@", "(", "*", ")"])
        self.assertEqual([t for _, t in toks("@*")], ["@", "*"])

    def test_strings(self):
        self.assertEqual(toks('"a\\"b" x'), [("STR", '"a\\"b"'), ("ID", "x")])

    def test_comments_skipped(self):
        self.assertEqual(toks("a // x\n b /* y\n z */ c"), [("ID", "a"), ("ID", "b"), ("ID", "c")])
        self.assertEqual(toks("a /* unterminated"), [("ID", "a")])

    def test_attributes_recorded_on_next_token(self):
        t = lex("(* full_case, parallel_case *) case (x) (* keep *) wire w; (* other *) reg r;")
        kinds = t.kinds
        self.assertEqual(t.attrs[kinds.index("case")], frozenset({"full_case", "parallel_case"}))
        self.assertEqual(t.attrs[kinds.index("wire")], frozenset({"keep"}))
        self.assertNotIn(kinds.index("reg"), t.attrs)
        self.assertNotIn("(*", t.texts)

    def test_illegal_character(self):
        t = lex("a ` b")
        self.assertEqual(t.stream(), [("ID", "a"), ("BAD", "`"), ("ID", "b")])
        self.assertEqual(len(t.errors), 1)
        self.assertIn("illegal character", t.errors[0][1])

    def test_offsets(self):
        t = lex("ab  cd")
        self.assertEqual(t.offs[:2], [0, 4])

    def test_eof_sentinels(self):
        t = lex("x")
        self.assertEqual(t.kinds[-4:], ["EOF"] * 4)

    def test_trailing_whitespace_no_error(self):
        t = lex("x \n\n  ")
        self.assertEqual(t.errors, [])
        self.assertEqual(t.stream(), [("ID", "x")])


if __name__ == "__main__":
    unittest.main()
