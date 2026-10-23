"""The reference parser itself: accepts the documented examples, rejects malformed outputs."""
import re
import unittest
from pathlib import Path

from tests.agent_grammar import GrammarError, parse

DOC = Path(__file__).resolve().parents[1] / "docs" / "OUTPUT.md"


class Grammar(unittest.TestCase):
    def test_documented_diff_examples_parse(self):
        text = DOC.read_text(encoding="utf-8")
        blocks = re.findall(r"```\n(.*?)```", text, flags=re.S)
        n = 0
        for b in blocks:
            if b.startswith(("DELTA", "ERROR NO_SOURCES")) and "<" not in b:
                parse(b)
                n += 1
        self.assertGreaterEqual(n, 5)

    def test_rejects_malformed(self):
        good = "DELTA +5 GE (+1..+9) +1.0% verdict=INCREASE confidence=high\nTOP t before=10 after=15\n"
        parse(good)
        bad = [
            good.replace("+5 GE", "+05 GE"),
            good.replace("verdict=INCREASE", "verdict=BIGGER"),
            good.replace("(+1..+9)", "(+6..+9)"),                 # range must contain the delta
            good + "garbage line\n",
            good + "+3 alu.sv:x added ADD\n",
            good.rstrip("\n"),                                      # missing newline
            good + "+3 alu.sv:4 invented ADD w8\n",                 # unknown reason
            good + "OTHERS 2 net=+0\n",                              # +0 is never printed
            good + "+3 a.sv:1 added X\n+9 a.sv:2 added Y\n",       # unsorted
            "DELTA +5 GE (+1..+9) +1.0% verdict=NO_CHANGE confidence=high\nTOP t before=1 after=1\n",
            good + "WHY low: x\n+3 a.sv:1 added X\n",              # out of order
            good + "café\n",
            "ERROR NO_SOURCES x\nTOP t\n",
        ]
        for b in bad:
            with self.subTest(b=b):
                with self.assertRaises(GrammarError):
                    parse(b)

    def test_other_commands(self):
        parse("COST 100 GE (90..130) confidence=medium\nTOP top\n60 alu x2 60%\nCATEGORIES seq=1 mul=2\nASSUMED x\n")
        parse("ESTIMATE 5 GE (4..6) confidence=high\nOP ADD w8\nARCH min=a typ=b max=c\nBREAKDOWN FA=8\n")
        parse("COMPARE 2 alternatives basis=typ\nA 1 GE (1..2) mul 8x8\nB 2 GE (1..3) add 8\nBEST A saves 1 GE vs B ranges=overlap\n")
        parse("SNAPSHOT saved id=0123abcd files=3 modules=2\n")
        parse("DELTA n/a verdict=NOT_COMPARABLE confidence=low\nWHY low: baseline has no module named x\n")


if __name__ == "__main__":
    unittest.main()
