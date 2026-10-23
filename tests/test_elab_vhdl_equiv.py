"""Cross-language contract: VHDL descriptions elaborate to the same normalized IR dump as the hand-built
equivalent (tests/ir_dsl.py), which is what the Verilog elaborator must also produce for the mirrored source.

Each case: (VHDL source, builder of the expected module).  Comparison is on ``dump(m, normalize=True)`` after
``analyze_ir``, i.e. the same operation structure, widths, constants, hierarchy of wiring and attributes.
"""
import unittest

from gatecost.ir import Const, LowerBlock, SigSpec, analyze_ir, dump

from tests.ir_dsl import E, Mod
from tests.test_elab_vhdl_util import module, unit


def vhdl_dump(src, top="t", **kw):
    m, r, d = module(src, top, **kw)
    return dump(m, normalize=True)


def expected(mod):
    m = mod.done()
    return dump(m, normalize=True)


class Equivalence(unittest.TestCase):
    def check(self, src, mod, **kw):
        got = vhdl_dump(src, **kw)
        exp = expected(mod)
        self.assertEqual(got, exp)

    # ---- combinational ------------------------------------------------------------
    def test_adder(self):
        m = Mod("t")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", a + b)
        self.check(unit("a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "", "y <= a + b;"), m)

    def test_multiplier(self):
        m = Mod("t")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("p", a.mul(b, 16))
        self.check(unit("a, b : in unsigned(7 downto 0); p : out unsigned(15 downto 0)", "", "p <= a * b;"), m)

    def test_signed_multiplier_accumulate(self):
        m = Mod("t")
        a, b, c = m.inp("a", 8, True), m.inp("b", 8, True), m.inp("c", 16, True)
        m.out("y", a.mul(b, 16) .add(c, 16))
        self.check(unit("a, b : in signed(7 downto 0); c : in signed(15 downto 0); y : out signed(15 downto 0)", "", "y <= a * b + c;"), m)

    def test_adder_tree(self):
        m = Mod("t")
        a, b, c, d = (m.inp(n, 8) for n in "abcd")
        m.out("y", (a + b) + (c + d))
        self.check(unit("a, b, c, d : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "", "y <= (a + b) + (c + d);"), m)

    def test_ternary_mux(self):
        m = Mod("t")
        a, b, s = m.inp("a", 8), m.inp("b", 8), m.inp("s", 1)
        m.out("y", s.mux(b, a))
        self.check(unit("a, b : in std_logic_vector(7 downto 0); s : in std_logic; y : out std_logic_vector(7 downto 0)", "", "y <= a when s = '1' else b;"), m)

    def test_logic_and_compare(self):
        m = Mod("t")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", (a & b) | (a ^ b))
        m.out("lt", a < b)
        self.check(unit("a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0); lt : out std_logic", "",
                        "y <= (a and b) or (a xor b); lt <= '1' when a < b else '0';"), m)

    def test_concat_slice_extension(self):
        m = Mod("t")
        a = m.inp("a", 8)
        m.out("y", E(m, SigSpec.concat([SigSpec.const(0, 2), a.s[4:8], a.s[0:4]])))
        m.out("z", E(m, a.s.zext(12)))
        self.check(unit("a : in unsigned(7 downto 0); y : out unsigned(9 downto 0); z : out unsigned(11 downto 0)", "",
                        "y <= a(3 downto 0) & a(7 downto 4) & \"00\"; z <= resize(a, 12);"), m)

    def test_case_alu(self):
        m = Mod("t")
        a, b, op = m.inp("a", 8), m.inp("b", 8), m.inp("op", 2)
        y = m.wire("y", 8)
        blk = LowerBlock(m.b, "comb")

        def put(v):
            return lambda: blk.assign(y.s, v.s)
        blk.case_(op.s, [([SigSpec.const(0, 2)], put(a & b)), ([SigSpec.const(1, 2)], put(a | b)), ([SigSpec.const(2, 2)], put(a ^ b))],
                  put(a + b))
        blk.finish()
        m.out("y_o", y)
        src = unit("a, b : in std_logic_vector(7 downto 0); op : in std_logic_vector(1 downto 0); y_o : out std_logic_vector(7 downto 0)",
                   "signal y : std_logic_vector(7 downto 0);",
                   "process(all) begin case op is when \"00\" => y <= a and b; when \"01\" => y <= a or b; when \"10\" => y <= a xor b; "
                   "when others => y <= std_logic_vector(unsigned(a) + unsigned(b)); end case; end process; y_o <= y;")
        self.check(src, m)

    def test_priority_if_chain(self):
        m = Mod("t")
        a, c, b, d = m.inp("a", 1), m.inp("c", 1), m.inp("b", 4), m.inp("d", 4)
        y = m.wire("y", 4)
        blk = LowerBlock(m.b, "comb")
        blk.if_chain([(a.s, lambda: blk.assign(y.s, b.s)), (c.s, lambda: blk.assign(y.s, d.s))], lambda: blk.assign(y.s, SigSpec.const(0, 4)))
        blk.finish()
        m.out("o", y)
        src = unit("a, c : in std_logic; b, d : in std_logic_vector(3 downto 0); o : out std_logic_vector(3 downto 0)", "signal y : std_logic_vector(3 downto 0);",
                   "process(all) begin if a = '1' then y <= b; elsif c = '1' then y <= d; else y <= (others => '0'); end if; end process; o <= y;")
        self.check(src, m)

    def test_latch(self):
        m = Mod("t")
        en, d = m.inp("en", 1), m.inp("d", 4)
        q = m.wire("q", 4)
        blk = LowerBlock(m.b, "comb")
        blk.if_(en.s, lambda: blk.assign(q.s, d.s))
        blk.finish()
        m.out("o", q)
        src = unit("en : in std_logic; d : in std_logic_vector(3 downto 0); o : out std_logic_vector(3 downto 0)", "signal q : std_logic_vector(3 downto 0);",
                   "process(all) begin if en = '1' then q <= d; end if; end process; o <= q;")
        self.check(src, m)

    def test_tristate_driver(self):
        m = Mod("t")
        en, d = m.inp("en", 1), m.inp("d", 4)
        m.out("o", E(m, m.b.mux(en.s, SigSpec.x(4), d.s, tristate=True)))
        self.check(unit("en : in std_logic; d : in std_logic_vector(3 downto 0); o : out std_logic_vector(3 downto 0)", "",
                        "o <= d when en = '1' else (others => 'Z');"), m)

    def test_rom(self):
        m = Mod("t")
        a = m.inp("a", 2)
        m.out("y", E(m, m.b.rom(a.s, [1, 2, 4, 8], 8)))
        rom = "type rom_t is array (0 to 3) of std_logic_vector(7 downto 0); constant R : rom_t := (x\"01\", x\"02\", x\"04\", x\"08\");"
        self.check(unit("a : in unsigned(1 downto 0); y : out std_logic_vector(7 downto 0)", rom, "y <= R(to_integer(a));"), m)

    # ---- sequential -----------------------------------------------------------------
    def test_counter_with_sync_reset_and_enable(self):
        m = Mod("t", {"w": 8})
        clk, rst, en = m.inp("clk", 1), m.inp("rst", 1), m.inp("en", 1)
        c = m.wire("c", 8)
        blk = LowerBlock(m.b, "clocked", clock=clk.s)
        # the reset arm is peeled (as the Verilog elaborator does) so that SRST and EN are both recognised
        blk.if_chain([(rst.s, lambda: blk.assign(c.s, SigSpec.const(0, 8), nonblocking=True))],
                     lambda: blk.if_(en.s, lambda: blk.assign(c.s, m.b.add(c.s, SigSpec.const(1, 8), 8), nonblocking=True)))
        blk.finish()
        m.out("q", c)
        src = unit("clk, rst, en : in std_logic; q : out std_logic_vector(W - 1 downto 0)", "signal c : unsigned(W - 1 downto 0);",
                   "process(clk) begin if rising_edge(clk) then if rst = '1' then c <= (others => '0'); elsif en = '1' then c <= c + 1; end if; end if; end process;"
                   " q <= std_logic_vector(c);", generics="W : natural := 8")
        self.check(src, m)

    def test_async_reset_register(self):
        m = Mod("t")
        clk, rstn, d = m.inp("clk", 1), m.inp("rstn", 1), m.inp("d", 4)
        q = m.wire("q", 4)
        blk = LowerBlock(m.b, "clocked", clock=clk.s, async_resets=[(rstn.s, 0)])
        blk.if_(m.b.not_(rstn.s), lambda: blk.assign(q.s, SigSpec.const(5, 4), nonblocking=True),
                lambda: blk.assign(q.s, d.s, nonblocking=True))
        blk.finish()
        m.out("o", q)
        src = unit("clk, rstn : in std_logic; d : in std_logic_vector(3 downto 0); o : out std_logic_vector(3 downto 0)", "signal q : std_logic_vector(3 downto 0);",
                   "process(clk, rstn) begin if rstn = '0' then q <= x\"5\"; elsif rising_edge(clk) then q <= d; end if; end process; o <= q;")
        self.check(src, m)

    def test_fsm(self):
        m = Mod("t")
        clk, rst, go = m.inp("clk", 1), m.inp("rst", 1), m.inp("go", 1)
        st = m.wire("st", 2)
        blk = LowerBlock(m.b, "clocked", clock=clk.s)

        def nxt(v):
            return lambda: blk.assign(st.s, SigSpec.const(v, 2), nonblocking=True)

        def body():
            blk.case_(st.s, [([SigSpec.const(0, 2)], lambda: blk.if_(go.s, nxt(1))), ([SigSpec.const(1, 2)], nxt(2)), ([SigSpec.const(2, 2)], nxt(0))])
        blk.if_(rst.s, nxt(0), body)
        blk.finish()
        m.out("busy", st == 1)
        decl = "type st_t is (idle, run, done); signal st : st_t;"
        body_v = ("process(clk) begin if rising_edge(clk) then if rst = '1' then st <= idle; else case st is "
                  "when idle => if go = '1' then st <= run; end if; when run => st <= done; when done => st <= idle; end case; end if; end if; end process;\n"
                  "busy <= '1' when st = run else '0';")
        self.check(unit("clk, rst, go : in std_logic; busy : out std_logic", decl, body_v), m)

    def test_register_file(self):
        m = Mod("t")
        clk, we = m.inp("clk", 1), m.inp("we", 1)
        wa, ra = m.inp("wa", 4), m.inp("ra", 4)
        di = m.inp("di", 8)
        mem = m.b.memory("ram", 16, 8)
        blk = LowerBlock(m.b, "clocked", clock=clk.s)
        blk.if_(we.s, lambda: blk.mem_write(mem, wa.s, di.s))
        blk.finish()
        m.out("do", E(m, mem.read(ra.s)))
        decl = "type ram_t is array (0 to 15) of std_logic_vector(7 downto 0); signal ram : ram_t;"
        body = ("process(clk) begin if rising_edge(clk) then if we = '1' then ram(to_integer(wa)) <= di; end if; end if; end process;\n"
                "do <= ram(to_integer(ra));")
        self.check(unit("clk, we : in std_logic; wa, ra : in unsigned(3 downto 0); di : in std_logic_vector(7 downto 0); do : out std_logic_vector(7 downto 0)", decl, body), m)

    def test_shift_register(self):
        m = Mod("t")
        clk, d = m.inp("clk", 1), m.inp("d", 1)
        sr = m.wire("sr", 4)
        blk = LowerBlock(m.b, "clocked", clock=clk.s)
        blk.assign(sr.s, SigSpec.concat([d.s, sr.s[0:3]]), nonblocking=True)
        blk.finish()
        m.out("q", sr)
        src = unit("clk, d : in std_logic; q : out std_logic_vector(3 downto 0)", "signal sr : std_logic_vector(3 downto 0);",
                   "process(clk) begin if rising_edge(clk) then sr <= sr(2 downto 0) & d; end if; end process; q <= sr;")
        self.check(src, m)

    # ---- hierarchy ------------------------------------------------------------------
    def test_hierarchy_keys_and_instance_counts(self):
        from gatecost.ir import Design
        sub = ("entity leaf is generic (W : natural := 4); port (a : in std_logic_vector(W - 1 downto 0); y : out std_logic_vector(W - 1 downto 0)); end entity;\n"
               "architecture r of leaf is begin y <= not a; end architecture;\n")
        body = ("u1 : entity work.leaf generic map (W => 8) port map (a => x, y => z1);\n u2 : entity work.leaf generic map (W => 8) port map (a => z1, y => z2);")
        m, r, d = module(unit("x : in std_logic_vector(7 downto 0); z2 : out std_logic_vector(7 downto 0)",
                              "signal z1 : std_logic_vector(7 downto 0);", body + "\n", extra=sub), "t")
        self.assertEqual(sorted(d.modules), ["leaf(w=8)", "t"])
        self.assertEqual(d.instance_counts(), {"t": 1, "leaf(w=8)": 2})
        leaf = Mod("leaf", {"w": 8})
        a = leaf.inp("a", 8)
        leaf.out("y", ~a)
        self.assertEqual(dump(d.modules["leaf(w=8)"], normalize=True), expected(leaf))


if __name__ == "__main__":
    unittest.main()
