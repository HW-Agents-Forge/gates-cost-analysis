"""VHDL lexer fixtures: case folding, extended identifiers, every literal form, pragma regions, comments."""
import unittest

from gatecost.frontend.vhdl.lexer import BitStr, lex


def kinds(text):
    toks, ignored, errors = lex(text)
    return [(t.kind, t.val) for t in toks[:-1]], ignored, errors


class LexerTests(unittest.TestCase):
    def test_case_folding(self):
        toks, _, _ = kinds("ENTITY Foo_Bar IS Std_Logic")
        self.assertEqual(toks, [("kw", "entity"), ("id", "foo_bar"), ("kw", "is"), ("id", "std_logic")])

    def test_extended_identifier_is_case_sensitive_and_verbatim(self):
        toks, _, _ = kinds("signal \\Foo Bar\\ : \\A\\\\B\\ ;")
        self.assertEqual(toks[1], ("xid", "\\Foo Bar\\"))
        self.assertEqual(toks[3], ("xid", "\\A\\\\B\\"))

    def test_decimal_integers_and_reals(self):
        toks, _, _ = kinds("42 1_000 3.14 2.5e3 1e3 6.0E-2")
        self.assertEqual(toks, [("int", 42), ("int", 1000), ("real", 3.14), ("real", 2500.0), ("int", 1000),
                                ("real", 0.06)])

    def test_based_literals(self):
        t, _, _ = lex("16#FF# 2#1010_1010# 8#17# 16#1.8#E1 16#ff#")
        self.assertEqual([x.num for x in t[:-1]], [255, 170, 15, 24.0, 255])
        self.assertEqual(t[0].kind, "based")
        self.assertEqual(t[0].val[:2], (16, "FF"))

    def test_character_literals_and_ticks(self):
        toks, _, _ = kinds("'0' '1' 'Z' '-' a'event std_logic'('1') x(1)'length")
        self.assertEqual(toks[:4], [("char", "0"), ("char", "1"), ("char", "Z"), ("char", "-")])
        self.assertEqual(toks[4:7], [("id", "a"), ("op", "'"), ("id", "event")])
        self.assertEqual(toks[7:13], [("id", "std_logic"), ("op", "'"), ("op", "("), ("char", "1"), ("op", ")"),
                                      ("id", "x")])
        self.assertEqual(toks[-3:], [("op", ")"), ("op", "'"), ("id", "length")])

    def test_string_literals(self):
        toks, _, _ = kinds('"abc" "" "a""b" "1010"')
        self.assertEqual(toks, [("str", "abc"), ("str", ""), ("str", 'a"b'), ("str", "1010")])

    def test_bit_string_literals_every_base_and_prefix(self):
        cases = {
            'b"1010"': BitStr(None, "b", None, "1010"),
            'B"10_10"': BitStr(None, "b", None, "1010"),
            'o"7"': BitStr(None, "o", None, "7"),
            'x"ff"': BitStr(None, "x", None, "FF"),
            'X"A_b"': BitStr(None, "x", None, "AB"),
            'd"10"': BitStr(None, "d", None, "10"),
            '8x"FF"': BitStr(8, "x", None, "FF"),
            '12UB"1X1"': BitStr(12, "b", "u", "1X1"),
            '12sb"101"': BitStr(12, "b", "s", "101"),
            '16UX"F"': BitStr(16, "x", "u", "F"),
            '6SO"7"': BitStr(6, "o", "s", "7"),
            '10d"5"': BitStr(10, "d", None, "5"),
        }
        for text, expect in cases.items():
            toks, _, errors = kinds(text)
            self.assertEqual(toks, [("bitstr", expect)], text)
            self.assertEqual(errors, [])

    def test_identifier_ending_in_base_letter_is_not_bitstring(self):
        toks, _, _ = kinds('sub "x"')
        self.assertEqual(toks, [("id", "sub"), ("str", "x")])

    def test_operators_longest_match(self):
        toks, _, _ = kinds("<= >= /= := => ** <> ?= ?/= ?<= ?>= ?< ?> ?? << >> & | ( )")
        self.assertEqual([v for _, v in toks], ["<=", ">=", "/=", ":=", "=>", "**", "<>", "?=", "?/=", "?<=", "?>=",
                                                "?<", "?>", "??", "<<", ">>", "&", "|", "(", ")"])

    def test_comments_are_skipped(self):
        toks, _, _ = kinds("a -- comment with 'quote\nb /* block\n comment */ c")
        self.assertEqual([v for _, v in toks], ["a", "b", "c"])

    def test_line_and_column(self):
        t, _, _ = lex("a\n  b /* x\ny */ c")
        self.assertEqual([(x.line, x.col) for x in t[:3]], [(1, 1), (2, 3), (3, 6)])

    def test_pragma_regions_excluded(self):
        for off, on in (("-- synopsys translate_off", "-- synopsys translate_on"),
                        ("-- pragma translate_off", "-- pragma translate_on"),
                        ("-- synthesis translate_off", "-- synthesis translate_on"),
                        ("--SYNOPSYS TRANSLATE_OFF", "--SYNOPSYS TRANSLATE_ON")):
            toks, ignored, _ = kinds("a\n%s\nsim_only <= 1;\n%s\nb" % (off, on))
            self.assertEqual([v for _, v in toks], ["a", "b"], off)
            self.assertEqual(ignored, [(2, 4)])

    def test_unterminated_pragma_region_runs_to_eof(self):
        toks, ignored, _ = kinds("a\n-- synopsys translate_off\nb\nc")
        self.assertEqual([v for _, v in toks], ["a"])
        self.assertEqual(ignored, [(2, 4)])

    def test_bad_characters_yield_error_tokens(self):
        toks, _, errors = kinds("a ` b")
        self.assertEqual(len(errors), 1)
        self.assertIn(("error", "unexpected character '`'"), toks)
        toks, _, errors = kinds('x := "abc')
        self.assertEqual(len(errors), 1)

    def test_never_raises_on_binary_garbage(self):
        import random
        rnd = random.Random(1)
        alphabet = "ab_09'\"\\#.-/*()<>=;: \n\t\u00e9\x00"
        for _ in range(200):
            lex("".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 80))))


if __name__ == "__main__":
    unittest.main()
