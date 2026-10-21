"""parse_files: libraries, architecture selection, duplicates, MISSING_UNIT, content hashes, docs."""
import os
import tempfile
import unittest

import gatecost.frontend.vhdl as vhdl
from gatecost.frontend.vhdl import nodes as N
from gatecost.frontend.vhdl import parse_files, parse_text


class Files:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())
        return p

    def close(self):
        self.dir.cleanup()


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.f = Files()
        self.addCleanup(self.f.close)

    def test_parse_files_in_order_with_library_map(self):
        a = self.f.write("rtl/a.vhd", "entity a is end; architecture r of a is begin end;")
        b = self.f.write("vendor/b.vhd", "package pb is constant c : integer := 1; end package;")
        c = self.f.write("vendor/sub/c.vhd", "entity c is end;")
        r = parse_files([a, b, c], {os.path.join(self.f.root, "vendor"): "vend",
                                    os.path.join(self.f.root, "vendor", "sub"): "deep", a: "mine"})
        self.assertIn(("mine", "a"), r.entities)
        self.assertIn(("vend", "pb"), r.packages)
        self.assertIn(("deep", "c"), r.entities)
        self.assertEqual([u.name for u in r.units], ["a", "r", "pb", "c"])
        self.assertEqual(r.entities[("mine", "a")].file, a)
        self.assertEqual(r.entities[("mine", "a")].library, "mine")

    def test_default_library_is_work_and_lookup_helpers(self):
        a = self.f.write("a.vhd", "entity Foo is end;")
        r = parse_files([a])
        self.assertIs(r.find_entity("FOO"), r.entities[("work", "foo")])
        self.assertIsNone(r.find_entity("bar"))
        self.assertIs(r.find_entity("foo", "work"), r.entities[("work", "foo")])

    def test_compile_order_is_not_required_for_parsing(self):
        a = self.f.write("a.vhd", "architecture r of e is begin end;")
        b = self.f.write("b.vhd", "entity e is end;")
        r = parse_files([a, b])
        self.assertEqual(len(r.architectures[("work", "e")]), 1)
        self.assertIn(("work", "e"), r.entities)

    def test_multiple_architectures_last_wins_with_diagnostic(self):
        r = parse_text("entity e is end; architecture a1 of e is begin end; architecture a2 of e is begin end;")
        self.assertEqual(r.selected[("work", "e")].name, "a2")
        amb = [d for d in r.diagnostics if d.code == "AMBIGUOUS_ARCHITECTURE"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0].severity, "warning")
        self.assertIs(r.architecture_of("e"), r.selected[("work", "e")])

    def test_single_architecture_has_no_diagnostic(self):
        r = parse_text("entity e is end; architecture a1 of e is begin end;")
        self.assertEqual([d.code for d in r.diagnostics], [])
        self.assertEqual(r.selected[("work", "e")].name, "a1")

    def test_project_file_selects_architecture(self):
        r = parse_text("entity e is end; architecture a1 of e is begin end; architecture a2 of e is begin end;",
                       architectures={"E": "A1"})
        self.assertEqual(r.selected[("work", "e")].name, "a1")
        d = [d for d in r.diagnostics if d.code == "AMBIGUOUS_ARCHITECTURE"]
        self.assertEqual([x.severity for x in d], ["info"])

    def test_configuration_selects_architecture(self):
        r = parse_text("entity e is end; architecture a1 of e is begin end; architecture a2 of e is begin end;"
                       "configuration c of e is for a1 end for; end configuration;")
        self.assertEqual(r.selected[("work", "e")].name, "a1")
        self.assertEqual([d.severity for d in r.diagnostics if d.code == "AMBIGUOUS_ARCHITECTURE"], ["info"])

    def test_duplicate_entity_last_wins(self):
        r = parse_text("entity e is port (a : in bit); end; entity e is port (b : in bit); end;")
        self.assertEqual(r.entities[("work", "e")].ports[0].name, "b")
        self.assertEqual([d.code for d in r.diagnostics], ["DUPLICATE_MODULE"])

    def test_missing_unit_for_unknown_packages(self):
        r = parse_text("library ieee, vendor;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n"
                       "use ieee.math_real.all; use ieee.std_logic_arith.all; use std.textio.all;\n"
                       "use vendor.vcomp.all;\nuse work.missing_pkg.all;\nuse ieee.nonexistent.all;\n"
                       "entity e is end;")
        missing = sorted(d.message.split("'")[1] for d in r.diagnostics if d.code == "MISSING_UNIT")
        self.assertEqual(missing, ["ieee.nonexistent", "vendor.vcomp", "work.missing_pkg"])
        self.assertTrue(all(d.affects_cost and d.severity == "warning" for d in r.diagnostics if d.code == "MISSING_UNIT"))

    def test_work_package_in_sources_is_found(self):
        r = parse_text("package p is end; use work.p.all; entity e is end;")
        self.assertEqual([d.code for d in r.diagnostics if d.code == "MISSING_UNIT"], [])

    def test_package_found_in_another_library_via_library_clause(self):
        a = self.f.write("a.vhd", "package pk is end package;")
        b = self.f.write("b.vhd", "library util; use util.pk.all; entity e is end;")
        r = parse_files([a, b], {a: "util"})
        self.assertEqual([d.code for d in r.diagnostics], [])

    def test_content_hash_is_stable_and_detects_changes(self):
        a = self.f.write("a.vhd", "entity e is end;\r\n")
        h1 = parse_files([a]).file_hashes[a]
        h2 = parse_files([a]).file_hashes[a]
        self.assertEqual(h1, h2)
        self.f.write("a.vhd", "entity e is end; \r\n")
        self.assertNotEqual(h1, parse_files([a]).file_hashes[a])

    def test_encoding_fallback_and_line_endings(self):
        a = self.f.write("a.vhd", b"-- caf\xe9\r\nentity e is end;\r\nentity f is end;\r")
        r = parse_files([a])
        self.assertEqual(sorted(k[1] for k in r.entities), ["e", "f"])
        self.assertEqual(r.entities[("work", "f")].line, 3)

    def test_hashes_dict_keys(self):
        r = parse_text("entity e is end; architecture a of e is begin end; package p is end; package body p is end;")
        self.assertEqual(set(r.hashes), {("entity", "work", "e"), ("architecture", "work", "e", "a"),
                                         ("package", "work", "p"), ("package_body", "work", "p")})
        self.assertEqual(r.hashes[("entity", "work", "e")], r.entities[("work", "e")].hash)


class DocumentationTests(unittest.TestCase):
    def test_every_node_class_is_documented_in_the_package_docstring(self):
        doc = vhdl.__doc__
        missing = [c.__name__ for c in N.ALL_NODE_CLASSES if c.__name__ not in doc]
        self.assertEqual(missing, [])

    def test_all_nodes_use_slots(self):
        for c in N.ALL_NODE_CLASSES:
            self.assertFalse(hasattr(c(), "__dict__"), c.__name__)


if __name__ == "__main__":
    unittest.main()
