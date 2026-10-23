"""Word-level IR with bit-level facts (docs/IR.md).

Front-end API tour
==================

Everything an elaborator needs is re-exported here.  Typical flow::

    from gatecost.ir import Builder, LowerBlock, SigSpec, Const, Origin, Design, analyze_ir

    b = Builder("alu", params={"W": 8})              # one Builder per (module, params, const ports)
    a   = b.port("a",   "in",  8)                    # ports are nets; returns a Net
    sel = b.port("sel", "in",  1)
    clk = b.port("clk", "in",  1)
    y   = b.port("y",   "out", 8)
    q   = b.net(8, name="q")                         # named wire/reg (hint used for keys)

Nets and wiring (never create nodes, cost nothing)
--------------------------------------------------
* ``Net``: ``.width .signed .name``; ``net.sig`` is the full ``SigSpec``; ``net[lo:hi]``
  (Python half-open, LSB indexed) and ``net[i]`` give slices as SigSpecs.
* ``SigSpec`` is an immutable ordered list of chunks, LEAST significant first.
  ``SigSpec.of(net, lo, width)``, ``SigSpec.const(value, width, xmask=0)``,
  ``SigSpec.x(width)`` (all don't-care), ``SigSpec.concat([lsb_part, ..., msb_part])``,
  ``sig.slice(lo, width)``, ``sig[lo:hi]``, ``sig.cat(higher...)`` (self is the lowest part),
  ``sig.repeat(n)``, ``sig.zext(w)``, ``sig.sext(w)``, ``sig.trunc(w)``,
  ``sig.resize(w, signed)``, ``sig.sign_rep`` (count of top bits that copy the bit below).
  Verilog ``{a, b}`` is ``SigSpec.concat([b, a])`` (the first operand is the MOST significant).
* ``Const(value, width, xmask)``: ``xmask`` marks don't-care bits (``x``, ``z``, ``-``, ``?``).
* ``b.connect(dst, src)``: continuous assignment (``assign``, port connections to wires).
  ``dst`` is a net or a SigSpec of net slices (concatenation lvalues allowed); widths must
  match.  Two drivers on one bit give ``MULTIPLE_DRIVERS`` and the first driver is kept.

Operations (create nodes; return a SigSpec)
-------------------------------------------
All operations are methods of the builder and return a ``SigSpec`` (a node output, a constant
or plain wiring when the builder folded the operation)::

    b.add(a, c, width, sa=False, sb=False, origin=..., name=...)    # also sub mul div mod
    b.neg(a, width, sa=False);  b.pow(a, exp, width, sa=False)
    b.shl(a, amount, width);    b.shr / b.sar / b.rol / b.ror        # constant amounts are wiring
    b.and_ / or_ / xor / xnor (a, c, width=None);   b.not_(a)
    b.red_and / red_or / red_xor (a);    b.eq / ne (a, c);   b.lt / le / gt / ge (a, c, sa, sb)
    b.mux(sel, a_when0, b_when1);        b.pmux(default, cases, selects);  b.priority(...)
    b.decode(a);  b.encode(a);  b.rom(addr, contents, width)
    b.to_bool(x); b.lnot(x); b.land(a, c); b.lor(a, c)               # ! && || on vectors
    b.index_mux([arm0, arm1, ...], index)                            # variable-index read
    b.op("ADD", a, c, width=8, sa=..., group=..., **attrs)           # generic form

``width`` is the width of the result net (the front-end applies the language sizing rules).
``sa``/``sb`` say whether each operand is signed *in the operation's context* (Verilog: pass
the expression's signedness for both operands; mixed signed/unsigned operands are unsigned, so
pass ``False``).  Operands are normalised by the builder: ADD/SUB/MUL/NEG/shifts/bitwise
operands are extended (sign-extended when the flag is set, else zero-extended) or truncated
to the result width; comparisons and DIV/MOD extend the narrower operand to the common width.
The flags are stored canonically (``sa`` only if the operand was narrower than the result and
signed; for compares/DIV/MOD both are ``sa and sb``).  Hash-consing, constant folding, the identity table
of IR.md section 4.3 and don't-care handling happen inside; do not pre-simplify.
``origin=Origin(file, line, col)`` per call, or set ``b.origin`` as the default.
Tri-state: ``b.mux(en, SigSpec.x(w), data, tristate=True)`` (write it as an expression, also
inside procedural code: ``blk.assign(y, b.mux(...))``); the flag keeps the high-impedance arm.

State
-----
``b.dff(d, clk, en=None, srst=None, arst=None, srst_value=, arst_value=, srst_polarity=1,
arst_polarity=1, clk_edge="pos", init=None, q=net)`` and ``b.latch(d, en, ..., q=net)`` build
state elements directly (``q=`` drives an existing net).  Identical flops are merged and
counted in ``merge_count``.  Usually you do not call them: ``LowerBlock`` does.
Memories: ``mem = b.memory("ram", depth, width)``; ``data = mem.read(addr[, clk=..])``
(async without clk); ``mem.write(addr, data, en=, clk=)``.  Inside a ``LowerBlock`` use
``blk.mem_write(mem, addr, data)`` so the path condition becomes the write enable.  A partial-word write
``blk.mem_write(mem, addr, lane_data, lane=(lo, width))`` (byte enables) is merged at ``finalize`` with the other
disjoint lanes of the same address/clock into one write port with a per-lane enable input ``w<i>_be`` and the
attribute ``wlanes`` (tuple of ``(lo, width)`` per port).

Procedural code (always / process)
----------------------------------
::

    blk = LowerBlock(b, "clocked", clock=clk, clk_edge="pos",
                     async_resets=[(rst_n, 0)],     # (signal, active level) from the sensitivity list
                     origin=Origin("alu.v", 12, 1))
    # or LowerBlock(b, "comb")
    blk.assign(lhs, rhs, nonblocking=False, signed=False)   # lhs: net / SigSpec of net slices
    blk.read(sig)                                           # value a read sees now (blocking updates)
    blk.if_(cond, then_fn, else_fn)                         # callbacks run in a forked environment
    blk.if_chain([(c0, f0), (c1, f1)], else_fn)             # if/elsif chains -> PRIORITY (or PMUX)
    blk.case_(sel, [([label, ...], fn), ...], default_fn)   # labels may carry xmask wildcards
    # case_/if_chain also take parallel=True (PMUX: unique / parallel_case) or False (PRIORITY) and full=True
    # (no default feedback: the last arm is the default; full_case / unique / priority)
    blk.assign_indexed(net, index, elem_width, count, value)
    blk.mark_variable(net)      # VHDL variable / block-local temporary in a clocked block
    blk.finish()                # connect comb results, infer latches, create flops

``finish`` does IR.md section 5: combinational targets are connected, an incomplete path
becomes a ``LATCH`` (``inferred=True``, diagnostic ``INFERRED_LATCH``); clocked targets become
``DFF`` nodes with, in this order, asynchronous reset/set (the leading branch on a signal of
``async_resets``), synchronous reset (outermost constant branch), clock enable (``D = MUX(en,
q, x)`` becomes ``EN = en``); the recognised MUXes are removed.  Conditions of an if-chain that
are ``EQ(x, const)`` with distinct constants, and ``case`` items with disjoint constant labels,
give a ``PMUX`` (exhaustive cases need no default); otherwise ``PRIORITY``.  All MUX-like nodes
of one statement share a ``group`` tag.  Constant conditions execute only the taken branch.
Reads of non-blocking targets see the register output (the net itself).  Write ``else if``
ladders with ``if_chain`` (flat) rather than nested ``if_`` calls in the else branch: it gives
the PRIORITY/PMUX form and avoids deep recursion for ladders of hundreds of arms.  A branch on
an asynchronous-reset signal keeps its MUX over the full register width so that the reset is
recognised even for bits whose reset and normal values coincide.  Constant-data MUXes created
by lowering are turned into wires/inverters by the analysis (``analyze_ir``).

Hierarchy
---------
::

    key = Design.make_key("child", {"W": 8}, {"en": Const(1, 1)})   # canonical string key
    outs = b.inst(key, "u_child", inputs={"a": sig}, outputs={"y": y_net_or_width}, count=1)
    outs = b.blackbox("acme_pll", "u_pll", inputs={...}, outputs={...})

Constant-port propagation: ``cp = Design.const_port_signature(inputs)`` lists the ports tied to
constants; ``key = Design.make_key(name, params, cp)`` is the uniquified key; build that child
with ``Builder(name, params, const_ports=cp)``: the port's known bits are preset and the
analysis folds the child (known select of MUX, dead cones, constant registers).
``Design.add(module)``, ``Design.top``, ``Design.instance_counts()`` (multiplied along the
hierarchy; ``count`` multiplies for instance arrays), ``Design.detect_recursion()``
(``RECURSIVE_INSTANCE``, cut at the second occurrence).

Finishing
---------
``m = b.finalize()`` cuts combinational loops (``COMBINATIONAL_LOOP``), checks the invariants
of IR.md section 12 (raises ``IRInvariantError``; ``check="diag"`` turns them into
diagnostics) and returns the ``ModuleIR``.  If a limit was hit the module is a black box
(``m.is_blackbox``) and carries ``LIMIT_REACHED``.  ``analyze_ir(m)`` runs the passes of
section 6; ``dump(m)`` gives the stable text; ``export_module(m)`` gives plain data for the
cost-tree builder; ``m.canonical_hash`` / ``node.shape`` / ``node.named_key`` are the keys.
"""
from __future__ import annotations

from .analyze import analyze_ir
from .builder import Builder, IRBuildError, MemHandle
from .check import IRInvariantError, check_module, cut_loops
from .core import (
    ALL_OPS, COMMUTATIVE, Conn, IRDiagnostic, ModuleIR, Net, Node, Origin, module_key,
)
from .design import Design, analyze_design, export_design
from .dump import dump
from .evalops import eval_const
from .export import export_module, export_node
from .limits import DEFAULT_LIMITS, Limits
from .lowering import LowerBlock
from .sigspec import Const, SigSpec, Slice, mask

__all__ = [
    "Builder", "LowerBlock", "MemHandle", "SigSpec", "Const", "Slice", "Net", "Node", "Conn",
    "Origin", "ModuleIR", "Design", "IRDiagnostic", "IRBuildError", "IRInvariantError", "Limits",
    "DEFAULT_LIMITS", "analyze_ir", "analyze_design", "export_design", "dump", "export_module", "export_node",
    "check_module", "cut_loops", "module_key", "eval_const", "ALL_OPS", "COMMUTATIVE", "mask",
]
