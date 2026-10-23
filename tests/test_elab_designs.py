"""Elaborator: realistic multi-module designs checked structurally and by simulated semantics."""
import random
import unittest

from tests import elab_designs as D
from tests.elab_helpers import Sim, codes, comb, elab, ops, top_module


def sg(x, w):
    return x - (1 << w) if (x >> (w - 1)) & 1 else x


class Designs(unittest.TestCase):
    def test_every_design_elaborates_without_cost_affecting_diagnostics(self):
        for name, (src, top) in D.DESIGNS.items():
            with self.subTest(name):
                r = elab(src, top=top)
                self.assertEqual(codes(r, True), [], name)
                self.assertTrue(r.design.modules[r.top_key].nodes)

    def test_counter(self):
        m = top_module(elab(D.COUNTER, top="counter"))
        self.assertEqual(ops(m).get("DFF"), 1)
        dff = [n for n in m.nodes if n.op == "DFF"][0]
        self.assertEqual(dff.attrs["reset_kind"], "sync")
        s = Sim(m)
        rnd = random.Random(1)
        q = 0
        for _ in range(300):
            i = dict(rst=int(rnd.random() < 0.05), en=rnd.randrange(2), load=int(rnd.random() < 0.1),
                     down=rnd.randrange(2), din=rnd.randrange(256))
            out = s.step(i)
            self.assertEqual(out["q"], q)
            exp_wrap = int(i["en"] and not i["load"] and (q == 0 if i["down"] else q == 255))
            self.assertEqual(out["wrap"], exp_wrap)
            if i["rst"]:
                q = 0
            elif i["load"]:
                q = i["din"]
            elif i["en"]:
                q = (q - 1 if i["down"] else q + 1) & 255

    def test_fifo_ctrl(self):
        r = elab(D.FIFO_CTRL, top="fifo_ctrl")
        m = top_module(r)
        regs = {n.outputs[0].name: n for n in m.nodes if n.op == "DFF"}
        self.assertEqual(sorted(regs), ["count", "rptr", "wptr"])
        self.assertTrue(all(n.attrs["reset_kind"] == "async" and n.has_input("EN") or n.outputs[0].name == "count"
                            for n in regs.values()))
        s = Sim(m)
        rnd = random.Random(2)
        count = wp = rp = 0
        for _ in range(400):
            push, pop = rnd.randrange(2), rnd.randrange(2)
            out = s.step({"rst_n": 1, "push": push, "pop": pop})
            self.assertEqual(out["count"], count)
            self.assertEqual(out["full"], int(count == 8))
            self.assertEqual(out["empty"], int(count == 0))
            self.assertEqual((out["waddr"], out["raddr"]), (wp & 7, rp & 7))
            dpush = push and count != 8
            dpop = pop and count != 0
            wp = (wp + dpush) & 15
            rp = (rp + dpop) & 15
            count += int(bool(dpush)) - int(bool(dpop))

    def test_alu(self):
        m = top_module(elab(D.ALU, top="alu"))
        rnd = random.Random(4)
        for _ in range(500):
            a, b, op = rnd.randrange(256), rnd.randrange(256), rnd.randrange(11)
            o = comb(m, a=a, b=b, op=op)
            sh = b & 7
            exp = {0: a + b, 1: a - b, 2: a & b, 3: a | b, 4: a ^ b, 5: a << sh, 6: a >> sh,
                   7: sg(a, 8) >> sh, 8: ~a}.get(op, 0) & 255
            self.assertEqual(o["y"], exp, (a, b, op))
            self.assertEqual(o["zero"], int(exp == 0))
            self.assertEqual(o["lt_s"], int(sg(a, 8) < sg(b, 8)))
            self.assertEqual(o["lt_u"], int(a < b))

    def test_fsm_sequence_detector(self):
        r = elab(D.FSM, top="seq_detect")
        m = top_module(r)
        regs = [n for n in m.nodes if n.op == "DFF"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].outputs[0].width, 2)       # enum with 4 members: 2 bits
        self.assertEqual(regs[0].attrs["reset_kind"], "async")
        s = Sim(m)
        s.apply_async({"rst_n": 0})
        rnd = random.Random(7)
        hist = []
        for _ in range(300):
            x = rnd.randrange(2)
            out = s.step({"rst_n": 1, "x": x})
            hist.append(x)
            # y is high when the previous three inputs were 1,0,1 and the current is 1: state S101 & x
            if len(hist) >= 4:
                pass
            # compare against the reference automaton
            if len(hist) == 1:
                st = 0
            nxt = {0: (0, 1), 1: (2, 1), 2: (0, 3), 3: (2, 1)}[st][x]
            self.assertEqual(out["state_o"], st)
            self.assertEqual(out["y"], int(st == 3 and x == 1))
            st = nxt

    def test_regfile_is_a_memory_with_two_read_ports(self):
        r = elab(D.REGFILE, top="regfile")
        m = top_module(r)
        mem = [n for n in m.nodes if n.op == "MEM"][0]
        self.assertEqual((mem.attrs["depth"], mem.attrs["width"], mem.attrs["rports"], mem.attrs["wports"],
                          mem.attrs["read_style"]), (8, 8, 2, 1, "async"))

    def test_adder_tree(self):
        r = elab(D.ADDER_TREE, top="adder_tree", )
        keys = r.design.keys()
        self.assertIn("adder_tree(N=8,W=8)", keys)
        self.assertEqual(r.design.instance_counts()["adder_tree(N=1,W=8)"], 8)
        self.assertEqual(r.design.modules["adder_tree(N=8,W=8)"].port("sum").width, 11)
        # flat check: N=4 simulated through the hierarchy by elaborating a flat equivalent
        flat = """
module flat(input [31:0] in, output [9:0] sum);
  assign sum = in[7:0] + in[15:8] + in[23:16] + in[31:24];
endmodule"""
        m = top_module(elab(flat))
        self.assertEqual(comb(m, **{"in": 0x01020304})["sum"], 10)

    def test_sequential_multiplier(self):
        m = top_module(elab(D.MULTIPLIER, top="mult_seq"))
        s = Sim(m)
        s.apply_async({"rst_n": 0})
        rnd = random.Random(9)
        for _ in range(20):
            a, b = rnd.randrange(256), rnd.randrange(256)
            s.step({"rst_n": 1, "start": 1, "a": a, "b": b})
            for _c in range(9):
                out = s.step({"rst_n": 1, "start": 0, "a": 0, "b": 0})
            # done pulses for one cycle, together with the product
            self.assertEqual((out["done"], out["p"]), (1, a * b), (a, b))

    def test_combinational_signed_multiplier(self):
        r = elab(D.MULTIPLIER, top="mult_comb")
        m = top_module(r)
        self.assertEqual(ops(m), {"MUL": 1})
        mul = m.nodes[0]
        # operands are sign-extended wiring: the extension is visible as sign_rep, not as an attribute
        self.assertGreaterEqual(dict(mul.inputs)["A"].sign_rep, 8)
        self.assertGreaterEqual(dict(mul.inputs)["B"].sign_rep, 8)
        rnd = random.Random(2)
        for _ in range(100):
            a, b = rnd.randrange(256), rnd.randrange(256)
            self.assertEqual(comb(m, a=a, b=b)["p"], (sg(a, 8) * sg(b, 8)) & 0xFFFF)


class RealIdioms(unittest.TestCase):
    """UART transmitter, CRC function, loop-based priority encoder, generate-based barrel shifter, LFSR, Gray counter."""

    def setUp(self):
        self.r = elab(D.REAL_IDIOMS, top="uart_tx")

    def test_uart_transmit_waveform(self):
        m = top_module(self.r)
        s = Sim(m)
        s.apply_async({"rst_n": 0})
        data = 0xA5
        wave = []
        s.step({"rst_n": 1, "tx_start": 1, "tx_data": data})
        for _ in range(16 * 10 + 20):
            wave.append(s.step({"rst_n": 1, "tx_start": 0, "tx_data": 0}))
        # one start bit, eight data bits LSB first, one stop bit, each CLKS_PER_BIT cycles long
        first = [i for i, o in enumerate(wave) if o["tx"] == 0][0]
        bits = [wave[first + 16 * k + 8]["tx"] for k in range(10)]
        self.assertEqual(bits[0], 0)
        self.assertEqual(sum(b << i for i, b in enumerate(bits[1:9])), data)
        self.assertEqual(bits[9], 1)
        self.assertEqual(sum(o["done"] for o in wave), 1)
        self.assertTrue(any(o["busy"] for o in wave))

    def test_priority_encoder_loop(self):
        m = elab(D.REAL_IDIOMS, top="prio_enc").design.modules["prio_enc(N=8)"]
        for req in range(256):
            o = comb(m, req=req)
            self.assertEqual((o["idx"], o["valid"]), (((req & -req).bit_length() - 1) if req else 0, int(req != 0)))

    def test_barrel_shifter_generate(self):
        m = elab(D.REAL_IDIOMS, top="barrel").design.modules["barrel(W=16)"]
        rnd = random.Random(3)
        for _ in range(100):
            i, sh = rnd.randrange(1 << 16), rnd.randrange(16)
            self.assertEqual(comb(m, **{"in": i, "sh": sh})["out"], (i << sh) & 0xFFFF)

    def test_crc_function(self):
        m = top_module(elab(D.REAL_IDIOMS, top="crc16"))

        def crc(d, c):
            x = c
            for i in range(8):
                x = ((x << 1) & 0xFFFF) ^ (0x1021 if ((x >> 15) & 1) ^ ((d >> (7 - i)) & 1) else 0)
            return x
        rnd = random.Random(5)
        for _ in range(100):
            d, c = rnd.randrange(256), rnd.randrange(1 << 16)
            self.assertEqual(comb(m, d=d, c=c)["n"], crc(d, c))

    def test_lfsr_and_gray_counter(self):
        m = top_module(elab(D.REAL_IDIOMS, top="lfsr"))
        s = Sim(m)
        q = 0
        out = s.step({"rst": 1})
        q = 0xACE1
        for _ in range(50):
            out = s.step({"rst": 0})
            self.assertEqual(out["q"], q)
            fb = ((q >> 15) ^ (q >> 13) ^ (q >> 12) ^ (q >> 10)) & 1
            q = ((q << 1) | fb) & 0xFFFF
        g = Sim(top_module(elab(D.REAL_IDIOMS, top="gray")))
        g.step({"rst": 1})
        for k in range(20):
            out = g.step({"rst": 0})
            kk = k & 15
            self.assertEqual(out["g"], kk ^ (kk >> 1))


if __name__ == "__main__":
    unittest.main()
