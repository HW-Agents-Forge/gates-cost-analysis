"""Documentation sync and code-level lint (DISTRIBUTION.md sections 6 and 10, CLI.md section 6).

* every `gatecost ...` command shown in SKILL.md, README.md and docs/ is accepted by the argument parser;
* the documented `estimate` examples really run;
* SKILL.md frontmatter and size contract; the trigger prompt set is populated;
* the `agent` output examples of OUTPUT.md parse with a simple line grammar, and so does real output;
* the source tree uses only the standard library and syntax/APIs of Python 3.11.
"""
from __future__ import annotations

import ast
import io
import os
import re
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from gatecost import REPO_ROOT
from gatecost.cli import main
from gatecost.cli.parser import COMMANDS, build_parser, validate_args
from gatecost.errors import GatecostError

DOC_FILES = [REPO_ROOT / "SKILL.md", REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
LAUNCHER_RE = re.compile(r"^(?:python3 \S*scripts/gatecost|gatecost)\s+(\S.*)$")


def _fenced_blocks(text: str):
    for m in re.finditer(r"^```([\w-]*)\n(.*?)^```", text, re.S | re.M):
        yield m.group(1), m.group(2)


def documented_commands() -> list[tuple[str, str, list[str]]]:
    """(file, line, argv) for every command line in a fenced block that starts with the launcher or `gatecost`."""
    found = []
    for path in DOC_FILES:
        for lang, body in _fenced_blocks(path.read_text(encoding="utf-8")):
            if lang not in ("", "bash", "sh", "shell", "console"):
                continue
            for raw in body.splitlines():
                line = raw.strip().replace("<skill-dir>", "SKILLDIR")
                m = LAUNCHER_RE.match(line)
                if not m:
                    continue
                rest = re.split(r"\s+#\s", m.group(1))[0]  # trailing shell comment
                if re.search(r"\s{2,}[^-\s\"']", rest):
                    rest = re.split(r"\s{2,}", rest)[0]  # prose after a column gap (usage tables)
                if re.search(r"[<>\[\]|]|\.\.\.", rest):  # placeholders and synopsis syntax
                    continue
                argv = shlex.split(rest)
                if any(re.fullmatch(r"[A-Z]|ID|N|GE", a) for a in argv):  # metavariables in synopses
                    continue
                found.append((path.name, line, argv))
    return found


def parses(argv: list[str]) -> None:
    validate_args(build_parser().parse_args(argv))


class DocumentedCommands(unittest.TestCase):
    def test_commands_were_found(self):
        cmds = documented_commands()
        self.assertGreater(len(cmds), 10)
        self.assertTrue(any(f == "README.md" for f, _, _ in cmds))

    def test_every_documented_command_parses(self):
        for fname, line, argv in documented_commands():
            with self.subTest(file=fname, line=line):
                if argv[0] in COMMANDS or argv[0] == "--version":
                    try:
                        parses(argv)
                    except GatecostError as e:
                        self.fail(f"{e.code}: {e.message}")

    def test_inline_commands_in_skill_md_parse(self):
        text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        seen = 0
        for span in re.findall(r"`([^`\n]+)`", text):
            span = span.replace("<dir>", "d")
            words = span.split()
            if not words or words[0] not in COMMANDS or span in ("estimate", "help"):
                continue
            seen += 1
            with self.subTest(span=span):
                parses(shlex.split(span))
        self.assertGreater(seen, 5)

    def test_documented_estimate_examples_run(self):
        ran = 0
        for fname, line, argv in documented_commands():
            if argv[:1] != ["estimate"]:
                continue
            out = io.StringIO()
            with self.subTest(file=fname, line=line), redirect_stdout(out):
                code = main(argv)
            ran += 1
            self.assertEqual(code, 0, out.getvalue())
            self.assertRegex(out.getvalue(), r"^(ESTIMATE|COMPARE)", out.getvalue())
        self.assertGreater(ran, 4)


class SkillFile(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n(.*)", self.text, re.S)
        self.assertIsNotNone(m, "frontmatter missing")
        self.front, self.body = m.group(1), m.group(2)

    def test_frontmatter_has_exactly_name_and_description(self):
        keys = re.findall(r"^([A-Za-z_-]+):", self.front, re.M)
        self.assertEqual(keys, ["name", "description"])
        self.assertEqual(re.search(r"^name: (.*)$", self.front, re.M).group(1), "gatecost")

    def test_description_limits_and_vocabulary(self):
        desc = re.search(r"^description: (.*)$", self.front, re.M).group(1)
        self.assertLess(len(desc), 1024)
        low = desc.lower()
        for word in ("gate", "ge", "nand2", "verilog", "vhdl", "systemverilog", "synthesis", "timing", "power"):
            self.assertIn(word, low)

    def test_body_size(self):
        self.assertLessEqual(len(self.text.splitlines()), 120)

    def test_no_hard_coded_mount_path(self):
        for bad in (".claude", ".github", ".opencode", "/Users/", "skills/gatecost"):
            self.assertNotIn(bad, self.text)
        self.assertIn("<skill-dir>/scripts/gatecost", self.text)

    def test_docs_pointers_exist(self):
        for name in re.findall(r"`((?:[A-Z_]+)\.md)`", self.body):
            self.assertTrue((REPO_ROOT / "docs" / name).is_file() or (REPO_ROOT / name).is_file(), name)

    def test_trigger_prompt_set(self):
        text = (REPO_ROOT / "tests" / "skill" / "prompts.md").read_text(encoding="utf-8")
        yes, _, no = text.partition("## Should not activate")
        self.assertGreaterEqual(len(re.findall(r"^\d+\. ", yes, re.M)), 10)
        self.assertGreaterEqual(len(re.findall(r"^\d+\. ", no, re.M)), 10)


# --- line grammar of the `agent` format (OUTPUT.md section 2.2) -------------------------------------------

_N = r"[+-]?\d+"
_LEVEL = r"(?:high|medium|low)"
LINE_GRAMMAR = [
    re.compile(p) for p in (
        rf"^DELTA (?:{_N} GE \({_N}\.\.{_N}\) (?:{_N}(?:\.\d+)?%|n/a)|n/a) verdict=[A-Z_]+ confidence={_LEVEL}$",
        r"^TOP \S+(?: before=\d+ after=\d+)?$",
        rf"^{_N} \S+:\d+ \S+ \S.*$",                       # contributor
        rf"^OTHERS \d+ net={_N}$",
        rf"^WHY {_LEVEL}: \S.*$",
        r"^WARN [A-Z_]+ \S+:\d+ \S.*$",
        r"^ASSUMED \S.*$",
        r"^ERROR [A-Z_]+ \S.*$",
        rf"^COST \d+ GE \(\d+\.\.\d+\) confidence={_LEVEL}$",
        r"^\d+ \S+(?: x\d+)? \d+%$",                       # module line of `cost`
        r"^CATEGORIES (?:[a-z]+=\d+ ?)+$",
        rf"^ESTIMATE \d+ GE \(\d+\.\.\d+\) confidence={_LEVEL}$",
        r"^OP \S.*$",
        r"^ARCH min=\S+ typ=\S+ max=\S+$",
        r"^BREAKDOWN (?:\w+=\d+ ?)+$",
        r"^COMPARE \d+ alternatives basis=\w+$",
        rf"^[A-Z] \d+ GE \(\d+\.\.\d+\) \S.*$",
        r"^BEST [A-Z] saves \d+ GE vs [A-Z] ranges=(?:overlap|disjoint)$",
        r"^SNAPSHOT saved id=\w+ files=\d+ modules=\d+$",
    )
]


def bad_agent_lines(text: str) -> list[str]:
    bad = []
    for line in text.splitlines():
        if not line.isascii() or not any(g.match(line) for g in LINE_GRAMMAR):
            bad.append(line)
    return bad


class AgentGrammar(unittest.TestCase):
    def output_examples(self) -> list[str]:
        text = (REPO_ROOT / "docs" / "OUTPUT.md").read_text(encoding="utf-8")
        blocks = [b for lang, b in _fenced_blocks(text) if lang == "" and re.match(r"(DELTA|ERROR) ", b)]
        return [b for b in blocks if "<" not in b]

    def test_documented_examples_parse(self):
        examples = self.output_examples()
        self.assertGreaterEqual(len(examples), 5)
        for ex in examples:
            with self.subTest(example=ex.splitlines()[0]):
                self.assertEqual(bad_agent_lines(ex), [])

    def test_grammar_rejects_malformed_lines(self):
        for line in ("DELTA +5 GE", "WHY maybe: nope", "ERROR lowercase x", "OTHERS two net=0", "garbage",
                     "DELTA +342 GE (+281..+410) +1.2% verdict=INCREASE confidence=medium →"):
            with self.subTest(line=line):
                self.assertEqual(bad_agent_lines(line), [line])

    def test_real_output_parses(self):
        runs = [["estimate", "mul 32x32"], ["estimate", "--compare", "mul 32x32", "mul 32 const=10"],
                ["estimate", "--rtl", "assign y = a*b + c;", "--widths", "a=16,b=16,c=32"]]
        for argv in runs:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(argv), 0)
            with self.subTest(argv=argv):
                self.assertEqual(bad_agent_lines(out.getvalue()), [])

    def test_real_cost_output_parses(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "alu.v").write_text(
                "module alu(input [15:0] a, input [15:0] b, output [15:0] y);\n  assign y = a + b;\nendmodule\n")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["cost", "--root", d, "--no-cache"])
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(bad_agent_lines(out.getvalue()), [])


# --- lint: stdlib only, Python 3.11 ------------------------------------------------------------------------

SRC = REPO_ROOT / "src" / "gatecost"
POST_311_ATTRS = {"batched", "sumprod", "process_cpu_count"}          # itertools/math/os additions of 3.12-3.13
POST_311_IMPORTS = {("typing", n) for n in ("override", "TypeAliasType", "ReadOnly", "TypeIs", "get_protocol_members")}
POST_311_IMPORTS |= {("warnings", "deprecated"), ("copy", "replace"), ("itertools", "batched")}


def source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


class SourceLint(unittest.TestCase):
    def test_sources_found(self):
        self.assertGreater(len(source_files()), 20)

    def test_parse_as_python_311(self):
        for path in source_files():
            with self.subTest(file=str(path.relative_to(REPO_ROOT))):
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=(3, 11))
                for node in ast.walk(tree):
                    # PEP 695 (3.12): `type X = ...` and generic syntax are not valid in 3.11.
                    line = getattr(node, "lineno", 0)
                    self.assertNotIsInstance(node, getattr(ast, "TypeAlias", ()), f"line {line}")
                    self.assertFalse(getattr(node, "type_params", None), f"line {line}: PEP 695 generics")

    def test_no_post_311_apis(self):
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                where = f"{path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', 0)}"
                if isinstance(node, ast.Attribute) and node.attr in POST_311_ATTRS:
                    self.fail(f"{where}: .{node.attr} needs Python > 3.11")
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    for a in node.names:
                        self.assertNotIn((node.module, a.name), POST_311_IMPORTS, f"{where}: needs Python > 3.11")

    def test_standard_library_only(self):
        allowed = set(sys.stdlib_module_names) | {"gatecost"}
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for m in mods:
                    with self.subTest(file=str(path.relative_to(REPO_ROOT)), module=m):
                        self.assertIn(m.split(".")[0], allowed)

    def test_launcher_parses_with_old_grammar(self):
        text = (REPO_ROOT / "scripts" / "gatecost").read_text(encoding="utf-8")
        ast.parse(text, "gatecost", feature_version=(3, 7))
        self.assertNotIn("f\"", text)  # no f-strings: the version check must run on very old interpreters
        self.assertNotIn("f'", text)

    def test_pyproject_has_no_dependencies(self):
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["dependencies"], [])
        self.assertEqual(data["project"]["requires-python"], ">=3.11")
        self.assertEqual(data["project"]["scripts"]["gatecost"], "gatecost.cli:main")


if __name__ == "__main__":
    unittest.main()
