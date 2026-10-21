"""Error-recovery fixtures: a syntax error leaves neighbouring code parsed and yields the expected diagnostics."""
import unittest

from gatecost.frontend.vhdl import nodes as N
from tests._vhdl_helpers import errors, parse

GOOD = "entity good is port (a : in bit); end entity good;\n"
WRAP = "architecture a of e is\n%s\nbegin\n%s\nend;\n"


def err_lines(r):
    return [d.line for d in errors(r)]


def kinds(stmts):
    return [type(s).__name__ for s in stmts]


class UnitLevelRecovery(unittest.TestCase):
    def test_error_in_entity_ports_keeps_entity_and_next_unit(self):
        r = parse("entity bad is port (a : in ); end entity;\n" + GOOD)
        self.assertEqual(err_lines(r), [1])
        self.assertTrue(r.entities[("work", "bad")].incomplete)
        self.assertFalse(r.entities[("work", "good")].incomplete)
        self.assertEqual([p.name for p in r.entities[("work", "good")].ports], ["a"])

    def test_entity_missing_is(self):
        r = parse("entity bad port (a : in bit); end entity;\n" + GOOD)
        self.assertEqual(err_lines(r), [1])
        self.assertEqual([p.name for p in r.entities[("work", "bad")].ports], ["a"])
        self.assertIn(("work", "good"), r.entities)

    def test_broken_architecture_header_keeps_unit_and_neighbours(self):
        r = parse("architecture rtl e is begin end;\n" + GOOD)
        self.assertEqual(err_lines(r), [1])
        self.assertTrue(r.units[0].incomplete)
        self.assertIn(("work", "good"), r.entities)

    def test_broken_architecture_keeps_entity_and_prior_items(self):
        r = parse("entity e is port (x : out bit); end;\narchitecture a of e is\nsignal s : bit;\nbegin\n"
                  "s <= '1';\nx <= ;\nend;\n")
        self.assertEqual(err_lines(r), [6])
        self.assertIn(("work", "e"), r.entities)
        a = r.architectures[("work", "e")][0]
        self.assertTrue(a.incomplete)
        self.assertEqual([d.name for d in a.decls], ["s"])
        self.assertEqual(len(a.stmts), 1)

    def test_error_in_package_item(self):
        r = parse("package p is\n type t is (a,,b);\n constant c : integer := 1;\nend package;\n" + GOOD)
        self.assertEqual(err_lines(r), [2])
        p = r.packages[("work", "p")]
        self.assertEqual([d.name for d in p.decls], ["c"])
        self.assertIn(("work", "good"), r.entities)

    def test_truncated_file(self):
        r = parse("entity e is port (a : in bit);\narchitecture a of e is begin x <= ")
        self.assertTrue(errors(r))
        self.assertIn(("work", "e"), r.entities)
        self.assertIn(("work", "e"), r.architectures)
        self.assertTrue(r.entities[("work", "e")].incomplete)

    def test_unit_keyword_resynchronizes_after_missing_end(self):
        r = parse("entity e is port (a : bit\narchitecture a of e is begin x <= a; end;\nentity f is end;")
        self.assertEqual(err_lines(r), [2])
        self.assertEqual(len(r.architectures[("work", "e")][0].stmts), 1)
        self.assertIn(("work", "f"), r.entities)

    def test_garbage_between_units(self):
        r = parse("entity e is end;\n)))) ;;; garbage\narchitecture a of e is begin end;\n")
        self.assertTrue(errors(r))
        self.assertIn(("work", "e"), r.architectures)

    def test_empty_and_whitespace_files(self):
        for text in ("", "   \n\n", "-- only a comment\n"):
            r = parse(text)
            self.assertEqual((r.units, r.diagnostics), ([], []))


class StatementLevelRecovery(unittest.TestCase):
    def check(self, body, lines, expect_kinds=None, decls=""):
        r = parse(WRAP % (decls, body))
        self.assertEqual(err_lines(r), lines, [(d.line, d.message) for d in r.diagnostics])
        a = r.architectures[("work", "e")][0]
        if expect_kinds is not None:
            self.assertEqual(kinds(a.stmts), expect_kinds)
        return a

    def test_concurrent_assignment_error(self):
        a = self.check("x <= a and ;\ny <= b;\nz <= c;", [4], ["SignalAssign", "SignalAssign"])
        self.assertEqual([s.target.name for s in a.stmts], ["y", "z"])

    def test_missing_semicolon_keeps_the_statement(self):
        a = self.check("x <= a\ny <= b;\nz <= c;", [5], ["SignalAssign"] * 3)

    def test_if_condition_error_keeps_body_and_neighbours(self):
        a = self.check("process begin\nif a = then x <= 1; end if;\ny <= 2;\nend process;\nz <= 1;", [5],
                       ["ProcessStmt", "SignalAssign"])
        body = a.stmts[0].body
        self.assertEqual(kinds(body), ["IfStmt", "SignalAssign"])
        self.assertEqual(len(body[0].branches[0].body), 1)

    def test_case_selector_error(self):
        a = self.check("process begin\ncase is when others => x <= 1; end case;\ny <= 2;\nend process;\nz <= 1;", [5],
                       ["ProcessStmt", "SignalAssign"])
        self.assertEqual(kinds(a.stmts[0].body), ["CaseStmt", "SignalAssign"])

    def test_case_alternative_choice_error(self):
        a = self.check("process begin\ncase s is when => x <= 1; when others => y <= 2; end case;\nend process;\nz <= 1;",
                       [5], ["ProcessStmt", "SignalAssign"])
        self.assertEqual(len(a.stmts[0].body[0].alts), 2)

    def test_for_and_while_header_errors(self):
        self.check("process begin\nfor i in loop x <= 1; end loop;\ny <= 2;\nend process;\nz <= 1;", [5],
                   ["ProcessStmt", "SignalAssign"])
        self.check("process begin\nwhile loop x <= 1; end loop;\ny <= 2;\nend process;\nz <= 1;", [5],
                   ["ProcessStmt", "SignalAssign"])

    def test_generate_header_errors(self):
        a = self.check("g: for i in generate x(i) <= 1; end generate;\nz <= 1;", [4], ["ForGenerate", "SignalAssign"])
        self.assertEqual(len(a.stmts[0].stmts), 1)
        self.check("g: if generate x <= 1; end generate;\nz <= 1;", [4], ["IfGenerate", "SignalAssign"])

    def test_block_item_error(self):
        a = self.check("b: block is begin x <= ; y <= 1; end block;\nz <= 1;", [4], ["BlockStmt", "SignalAssign"])
        self.assertEqual(len(a.stmts[0].stmts), 1)

    def test_instance_association_errors(self):
        a = self.check("u1: comp port map (a => , b => c);\nz <= 1;", [4], ["SignalAssign"])
        self.check("u1: comp port map (a => b c);\nz <= 1;", [4], ["SignalAssign"])

    def test_declaration_errors(self):
        a = self.check("z <= 1;", [2], ["SignalAssign"],
                       decls="component c is port (x : in ; y : out bit); end component;\nsignal s : bit;")
        self.assertEqual([d.name for d in a.decls], ["c", "s"])
        a = self.check("z <= 1;", [2], ["SignalAssign"], decls="type r is record a : ; b : bit; end record;\nsignal s : bit;")
        self.assertEqual([d.name for d in a.decls][-1], "s")
        a = self.check("z <= 1;", [2],
                       decls="function f (x : integer) return is begin return 1; end;\nsignal s : bit;")
        self.assertEqual([d.name for d in a.decls], ["f", "s"])

    def test_error_in_function_body_statement(self):
        a = self.check("z <= 1;", [2],
                       decls="function f (x : integer) return integer is begin return +; end;\nsignal s : bit;")
        self.assertEqual([d.name for d in a.decls], ["f", "s"])

    def test_selected_assignment_error(self):
        self.check("with s select x <= a when ;\nz <= 1;", [4], ["SignalAssign"])

    def test_missing_end_if_is_attributed_to_the_if(self):
        r = parse(WRAP % ("", "process begin\nif a then if b then x <= 1; end if;\ny <= 2;\nend process;\nz <= 1;"))
        self.assertEqual(err_lines(r), [7])
        self.assertIn("end if", errors(r)[0].message)
        a = r.architectures[("work", "e")][0]
        self.assertEqual(kinds(a.stmts), ["ProcessStmt", "SignalAssign"])

    def test_stray_end_if_is_skipped(self):
        r = parse(WRAP % ("", "process begin\nx <= 1; end if; y <= 2;\nend process;\nz <= 1;"))
        self.assertEqual(err_lines(r), [5])
        a = r.architectures[("work", "e")][0]
        self.assertEqual(kinds(a.stmts), ["ProcessStmt", "SignalAssign"])
        self.assertEqual(len(a.stmts[0].body), 2)

    def test_lexical_error_reported_once_and_parsing_continues(self):
        r = parse(WRAP % ("", "x <= a ` b;\nz <= 1;"))
        self.assertEqual(err_lines(r), [4])
        a = r.architectures[("work", "e")][0]
        self.assertGreaterEqual(len(a.stmts), 1)
        self.assertEqual(a.stmts[-1].target.name, "z")

    def test_each_diagnostic_has_location_and_stable_code(self):
        r = parse(WRAP % ("", "x <= ;"))
        d = errors(r)[0]
        self.assertEqual((d.code, d.severity, d.affects_cost, d.line, d.column), ("PARSE_ERROR", "error", True, 4, 6))
        self.assertEqual(d.file, "<string>")

    def test_many_errors_are_capped(self):
        r = parse(WRAP % ("", "x <= ;\n" * 1000))
        self.assertLessEqual(len(r.diagnostics), 301)
        self.assertTrue(any(d.code == "LIMIT_REACHED" for d in r.diagnostics))


class OtherDiagnostics(unittest.TestCase):
    def test_unreadable_file(self):
        from gatecost.frontend.vhdl import parse_files
        r = parse_files(["/nonexistent/dir/x.vhd"])
        self.assertEqual([d.code for d in r.diagnostics], ["PARSE_ERROR"])
        self.assertEqual(r.units, [])


if __name__ == "__main__":
    unittest.main()
