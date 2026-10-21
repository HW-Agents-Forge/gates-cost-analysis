"""Robustness: seeded token-level mutation of the fixtures must terminate, never raise, and
return well-formed diagnostics (FRONTEND.md section 9)."""
import random
import time
import unittest

from gatecost.frontend.verilog import ast as A
from gatecost.frontend.verilog import parse_text
from gatecost.frontend.verilog.lexer import lex
from gatecost.result import Diagnostic
from tests.verilog_fixtures import CONSTRUCTS

EXTRA = {
    "macros": """
`define W 8
`define ADD(a,b) ((a)+(b))
module m(input [`W-1:0] a);
`ifdef FOO
  wire x = `ADD(a, 1);
`else
  wire y = `UNDEFINED;
`endif
// synopsys translate_off
  wire hidden;
// synopsys translate_on
endmodule
""",
    "errors": "module a(input x,, output y);\n assign y = x\n always @(posedge clk begin y <= ; end\nendmodule\n",
}
CORPUS = {**CONSTRUCTS, **EXTRA}
MUTATIONS_PER_FIXTURE = 30


def spans(text):
    t = lex(text)
    out = []
    for off, s in zip(t.offs, t.texts):
        if s:
            out.append((off, off + len(s)))
    return out


def mutate(rng, text, sp):
    if not sp:
        return text
    n = len(sp)
    op = rng.choice(("delete", "swap", "dup", "truncate", "delete", "swap"))
    i = rng.randrange(n)
    s, e = sp[i]
    if op == "delete":
        return text[:s] + text[e:]
    if op == "dup":
        return text[:e] + " " + text[s:e] + " " + text[e:]
    if op == "truncate":
        return text[:s]
    if i + 1 >= n:
        return text[:s]
    s2, e2 = sp[i + 1]
    return text[:s] + text[s2:e2] + text[e:s2] + text[s:e] + text[e2:]


def check(testcase, text):
    t0 = time.perf_counter()
    d = parse_text(text, "fuzz.sv")
    dt = time.perf_counter() - t0
    testcase.assertLess(dt, 5.0, "front-end took too long")
    for x in d.diagnostics:
        testcase.assertIsInstance(x, Diagnostic)
        testcase.assertTrue(x.code and x.message)
        testcase.assertNotIn("internal front-end error", x.message)
        if x.line is not None:
            testcase.assertGreaterEqual(x.line, 1)
    for m in d.modules + d.interfaces + d.packages:
        testcase.assertIsInstance(m.loc, tuple)
        testcase.assertEqual(len(m.digest()), 64)
        for n in A.walk(m):
            testcase.assertIsNotNone(n.loc)
            testcase.assertGreaterEqual(n.loc[1], 1)
    return d


class Fuzz(unittest.TestCase):
    def test_baseline_fixtures(self):
        for name, text in CORPUS.items():
            check(self, text)

    def test_token_mutations(self):
        rng = random.Random(20240601)
        count = 0
        for name, text in sorted(CORPUS.items()):
            sp = spans(text)
            for k in range(MUTATIONS_PER_FIXTURE):
                m = text
                for _ in range(rng.choice((1, 1, 2, 3))):
                    m = mutate(rng, m, spans(m))
                try:
                    check(self, m)
                except Exception:
                    print("FAILED fixture", name, "mutation", k)
                    print(m)
                    raise
                count += 1
        self.assertGreater(count, 800)

    def test_character_mutations(self):
        rng = random.Random(7)
        alphabet = "`'\"\\(){}[];:,.#@$*/\n\t xyz019"
        for name, text in sorted(CORPUS.items()):
            for _ in range(10):
                chars = list(text)
                for _ in range(rng.randint(1, 4)):
                    p = rng.randrange(len(chars))
                    op = rng.choice(("del", "ins", "rep"))
                    if op == "del":
                        del chars[p]
                    elif op == "ins":
                        chars.insert(p, rng.choice(alphabet))
                    else:
                        chars[p] = rng.choice(alphabet)
                check(self, "".join(chars))

    def test_every_prefix_of_a_fixture(self):
        text = CORPUS["control"] + CORPUS["loops_generate"]
        sp = spans(text)
        for s, _ in sp[::7]:
            check(self, text[:s])

    def test_pathological_inputs(self):
        cases = [
            "module m;" + "begin " * 3000,
            "module m; always @(*) " + "if (a) " * 3000 + "x = 1; endmodule",
            "module m; assign x = " + "{" * 3000 + "a" + "}" * 3000 + "; endmodule",
            "module m; assign x = " + "a ? b : " * 3000 + "c; endmodule",
            "module m; assign x = " + "-" * 5000 + "a; endmodule",
            "`define A `A\n" * 200 + "module m; wire `A; endmodule",
            "module m;" + "wire w;" * 50000 + "endmodule",
            "`ifdef A\n" * 3000 + "module m; endmodule\n",
            "module " * 5000,
            "/*" * 1000,
            '"' * 1001,
            "(* " * 1000,
            "module m; case (x) " + "1: y = 1; " * 50000 + " endcase endmodule",
            "\x00\x01\x02 module m; endmodule \x7f",
        ]
        for text in cases:
            check(self, text)


if __name__ == "__main__":
    unittest.main()
