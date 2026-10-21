"""Profile loading, ``extends``, overrides, validation and dump (COST_MODEL.md sections 1 and 6)."""
import tempfile
import tomllib
import unittest
from pathlib import Path

from gatecost.cost import cost_op, dump_profile, list_profiles, load_profile
from gatecost.cost.profile import PRIMITIVES
from gatecost.errors import GatecostError


def write(d, name, text):
    p = Path(d) / name
    p.write_text(text, encoding="utf-8")
    return str(p)


class Builtin(unittest.TestCase):
    def test_generic_asic_defaults(self):
        p = load_profile()
        self.assertEqual(p.name, "generic-asic")
        self.assertEqual(p.nand2_area_um2, 0.798)
        want = {"INV": 0.67, "BUF": 1.0, "NAND2": 1.0, "NAND3": 1.33, "NAND4": 1.67, "AND2": 1.33, "AOI21": 1.33, "AOI22": 1.67,
                "AO22": 2.0, "XOR2": 2.0, "MUX2": 2.33, "MUX4": 4.67, "MAJ3": 2.33, "HA": 3.33, "FA": 5.33, "DLAT": 3.33,
                "DFF": 5.67, "DFFR": 6.67, "DFFRS": 7.67, "EDFF": 7.33, "SDFF_ADD": 1.0, "ICG": 5.0}
        for k, v in want.items():
            self.assertEqual(p.P[k], round(v * 1000), k)
        self.assertEqual(set(p.P), set(PRIMITIVES))
        self.assertEqual((p.sigma_arith, p.sigma_other, p.overhead_typ, p.overhead_max), (1300, 1150, 1000, 1150))
        self.assertEqual((p.alpha_add, p.alpha_mul), (400, 400))
        self.assertEqual(p.add_arch, ("ripple", "brent_kung", "kogge_stone"))
        self.assertEqual(p.macro, {"c0": 3_000_000, "c_bit": 600, "c_row": 4_000, "c_col": 30_000})

    def test_list(self):
        self.assertIn("generic-asic", [n for n, _ in list_profiles()])

    def test_toml_file_is_valid_and_ascii(self):
        from gatecost.cost.profile import PROFILE_DIR

        text = (PROFILE_DIR / "generic-asic.toml").read_text(encoding="utf-8")
        text.encode("ascii")
        tomllib.loads(text)

    def test_dump_round_trips(self):
        p = load_profile()
        with tempfile.TemporaryDirectory() as d:
            q = load_profile(write(d, "dump.toml", dump_profile(p)))
        self.assertEqual((q.P, q.C, q.F, q.macro), (p.P, p.C, p.F, p.macro))
        self.assertEqual(dump_profile(q), dump_profile(p))


class Extends(unittest.TestCase):
    def test_override_a_primitive_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "acme.toml", 'extends = "generic-asic"\n[meta]\nname = "acme"\nnand2_area_um2 = 0.47\n[primitives]\nFA = 6.0\n')
            p = load_profile(f)
        base = load_profile()
        self.assertEqual((p.name, p.nand2_area_um2, p.P["FA"], p.P["HA"]), ("acme", 0.47, 6000, 3330))
        # every operation that uses a full adder follows
        self.assertGreater(cost_op("ADD", {"n": 32}, p).min, cost_op("ADD", {"n": 32}, base).min)
        self.assertEqual(cost_op("MUX", {"k": 4, "w": 8}, p).typ, cost_op("MUX", {"k": 4, "w": 8}, base).typ)
        self.assertEqual(cost_op("ADD", {"n": 32}, p).min - cost_op("ADD", {"n": 32}, base).min, 31 * 670)

    def test_chain_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a.toml", 'extends = "generic-asic"\n[primitives]\nINV = 0.5\n')
            write(d, "b.toml", 'extends = "a.toml"\n[primitives]\nBUF = 2.0\n')
            p = load_profile(write(d, "c.toml", 'extends = "b.toml"\n[meta]\nname = "c"\n'))
            self.assertEqual((p.P["INV"], p.P["BUF"], p.name), (500, 2000, "c"))
            self.assertEqual(load_profile("b.toml", base_dir=Path(d)).P["INV"], 500)

    def test_extends_inside_meta_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            p = load_profile(write(d, "a.toml", '[meta]\nextends = "generic-asic"\n[primitives]\nINV = 0.5\n'))
        self.assertEqual(p.P["INV"], 500)

    def test_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a.toml", 'extends = "b.toml"\n')
            f = write(d, "b.toml", 'extends = "a.toml"\n')
            with self.assertRaises(GatecostError) as cm:
                load_profile(f)
        self.assertEqual(cm.exception.code, "BAD_CONFIG")

    def test_cost_override_table(self):
        ov = {"policy": {"enable_style": "edff"}, "primitives": {"DFF": 6.0}, "memory": {"macro": {"c0": 1000}}}
        p = load_profile("generic-asic", ov)
        self.assertEqual((p.enable_style, p.P["DFF"], p.macro["c0"], p.macro["c_bit"]), ("edff", 6000, 1_000_000, 600))
        self.assertEqual(p.sigma_arith, 1300)  # untouched sibling keys survive
        self.assertEqual(load_profile().enable_style, "mux")  # the base is not mutated

    def test_blackboxes(self):
        p = load_profile("generic-asic", {"blackbox": {"pll": 4200, "sync": {"typ": 13.5, "min": 12}}})
        self.assertEqual(p.blackbox_cost("pll"), (4_200_000,) * 3)
        self.assertEqual(p.blackbox_cost("sync"), (12_000, 13_500, 13_500))
        self.assertIsNone(p.blackbox_cost("x"))

    def test_scaled(self):
        p = load_profile().scaled(3, 2)
        self.assertEqual(p.P["FA"], 7995)
        self.assertEqual(p.macro["c0"], 4_500_000)


class Errors(unittest.TestCase):
    def bad(self, text=None, overrides=None, spec="generic-asic"):
        with tempfile.TemporaryDirectory() as d:
            if text is not None:
                spec = write(d, "p.toml", text)
            with self.assertRaises(GatecostError) as cm:
                load_profile(spec, overrides)
        self.assertEqual(cm.exception.code, "BAD_CONFIG")
        self.assertEqual(cm.exception.exit_code, 2)
        return cm.exception.message

    def test_unknown_profile(self):
        self.assertIn("generic-asic", self.bad(spec="no-such-profile"))

    def test_malformed_toml(self):
        self.assertIn("invalid TOML", self.bad("[primitives\nFA = 1"))

    def test_missing_nand2_area(self):
        # a profile that does not extend anything must be complete and name nand2_area_um2
        full = load_profile()
        lines = ["[primitives]"] + [f"{k} = {v / 1000}" for k, v in full.P.items()]
        self.assertIn("nand2_area_um2", self.bad("\n".join(lines)))

    def test_missing_primitive_without_extends(self):
        self.assertIn("lacks", self.bad("[meta]\nnand2_area_um2 = 1.0\n[primitives]\nINV = 1.0\n"))

    def test_invalid_values(self):
        for ov in ({"primitives": {"FA": -1}}, {"primitives": {"FA": "x"}}, {"policy": {"enable_style": "magic"}},
                   {"policy": {"timing": "slow"}}, {"policy": {"scan": "yes"}}, {"policy": {"icg_min_width": -2}},
                   {"policy": {"sizing_factor": {"arith": 0.5, "other": 1.15}}}, {"policy": {"overhead": 3}},
                   {"arch": {"add": {"min": "ripple", "mid": "carry_select", "max": "kogge_stone"}}},
                   {"arch": {"add": {"min": "ripple", "mid": "brent_kung", "max": "kogge_stone", "alpha": 1.5}}},
                   {"blackbox": {"x": {"typ": 5, "min": 9}}}, {"memory": {"macro_table": [{"d": 1}]}},
                   {"meta": {"nand2_area_um2": 0}}):
            with self.subTest(ov=ov):
                self.bad(overrides=ov)

    def test_unknown_keys_are_informational(self):
        p = load_profile("generic-asic", {"policy": {"frobnicate": 1}, "weird": {}})
        self.assertEqual({d.code for d in p.diagnostics}, {"UNKNOWN_PROFILE_KEY"})
        self.assertTrue(all(d.severity == "info" for d in p.diagnostics))

    def test_architecture_names_are_selectable(self):
        ov = {"arch": {"add": {"min": "ripple", "mid": "kogge_stone", "max": "kogge_stone"}}}
        p = load_profile("generic-asic", ov)
        base = load_profile()
        self.assertGreater(cost_op("ADD", {"n": 32}, p).typ, cost_op("ADD", {"n": 32}, base).typ)


if __name__ == "__main__":
    unittest.main()
