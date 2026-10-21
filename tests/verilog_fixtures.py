"""Shared Verilog/SystemVerilog fixtures: one per construct row of FRONTEND.md section 3."""

CONSTRUCTS = {
    "module_ansi": """
module m #(parameter W = 8, parameter int D = 4) (input wire clk, input [W-1:0] a, b, output reg [W-1:0] q, inout wire io);
  localparam L = W * 2;
endmodule
""",
    "module_nonansi": """
module m(a, b, q);
  parameter W = 8;
  input [W-1:0] a, b;
  output reg [W-1:0] q;
  always @(posedge a) q <= b;
endmodule
""",
    "data_types": """
module m;
  wire w; wire signed [7:0] ws; reg [3:0] r; reg signed [3:0] rs; logic l; logic [7:0][3:0] pk;
  bit b; bit signed [2:0] bs; byte by; shortint si; int i; longint li; integer ig; time t;
  tri [3:0] tr; wand wa; supply0 g0; supply1 g1;
endmodule
""",
    "typedef_enum_struct": """
package p;
  typedef enum logic [1:0] {A, B = 2, C} st_t;
  typedef enum {X, Y} anon_t;
  typedef struct packed { logic [3:0] hi; logic [3:0] lo; } byte_t;
  typedef struct packed signed { logic a; } sp_t;
  typedef union packed { logic [7:0] raw; byte_t f; } u_t;
  typedef logic [7:0] vec_t;
endpackage
module m; p::st_t s; p::byte_t bb; vec_t v; endmodule
""",
    "unpacked_arrays": """
module m;
  logic [7:0] mem [0:255];
  logic [7:0] rf [32];
  logic [3:0] two [2][3];
  always @(posedge clk) begin mem[addr] <= d; q <= mem[addr]; end
endmodule
""",
    "assign_always": """
module m(input clk, input a, output y, output reg z);
  assign y = a;
  assign #1 y = ~a, z = a;
  always @(posedge clk) z <= a;
  always @* z = a;
  always @(*) z = a;
  always @(a or clk) z = a;
  always_ff @(posedge clk, negedge a) z <= a;
  always_comb z = a;
  always_latch if (clk) z = a;
endmodule
""",
    "control": """
module m;
  always_comb begin
    if (a) y = 1; else if (b) y = 2; else y = 3;
    unique if (a) y = 1;
    priority if (a) y = 1; else y = 0;
    case (s) 0: y = 1; 1, 2: y = 2; default: y = 3; endcase
    casez (s) 3'b1??: y = 1; default: y = 0; endcase
    casex (s) 3'b1xx: y = 1; endcase
    unique case (s) 0: y = 1; endcase
    priority case (s) 0: y = 1; default: y = 2; endcase
    case (s) inside [0:3]: y = 1; 8, 9: y = 2; endcase
  end
endmodule
""",
    "loops_generate": """
module m #(parameter N = 4) (input [N-1:0] a, output reg [N-1:0] y);
  genvar i;
  always_comb for (int k = 0; k < N; k++) y[k] = a[N-1-k];
  generate
    for (i = 0; i < N; i = i + 1) begin : g
      assign y[i] = a[i];
    end
    if (N > 2) begin : big assign y[0] = 1; end else begin : small assign y[0] = 0; end
    case (N) 1: begin : c1 end 2, 3: begin : c2 end default: begin : cd end endcase
  endgenerate
  for (genvar j = 0; j < 2; j++) begin : g2 end
  if (N == 1) assign y[0] = a[0];
  always_comb begin while (a) y = 1; repeat (2) y = 0; end
endmodule
""",
    "functions_tasks": """
module m;
  function automatic [7:0] f(input [7:0] x, input [7:0] y);
    f = x + y;
  endfunction
  function integer clog(input integer v);
    integer r;
    begin r = 0; while (v > 1) begin v = v >> 1; r = r + 1; end clog = r; end
  endfunction
  function logic g(logic a); return a; endfunction
  function void h(); endfunction
  function pkg::t_t k(input pkg::t_t a); k = a; endfunction
  task automatic tk(input a, output b); b = a; endtask
  function [3:0] old;
    input [3:0] a;
    old = a;
  endfunction
endmodule
""",
    "packages_import": """
package pk;
  localparam int W = 8;
  parameter P = 3;
  function int f(int x); return x; endfunction
endpackage
import pk::*;
module m import pk::*; (input logic [W-1:0] a);
  import pk::W;
  import pk::f, pk::P;
  localparam Q = pk::W + 1;
  wire [pk::W-1:0] w = pk::f(3);
endmodule
""",
    "system_calls": """
module m #(parameter N = 8);
  localparam A = $clog2(N), B = $bits(logic [N-1:0]), C = $signed(3), D = $unsigned(-1), E = $countones(5);
  localparam F = $max(1,2) + $min(3,4);
  wire [$bits(N)-1:0] w;
endmodule
""",
    "streaming_inside_patterns": """
module m;
  assign x = {<<8{a}};
  assign y = {>>{a, b}};
  assign z = {<<{a with [3:0]}};
  assign i1 = a inside {1, 2, [4:8]};
  logic [3:0] arr [0:3] = '{default: 0};
  logic [3:0] arr2 [0:3] = '{4'd1, 4'd2, 4'd3, 4'd4};
  logic [3:0] arr3 [0:3] = '{2{4'd1, 4'd2}};
  s_t st = '{a: 1, b: 2};
  s_t st2 = s_t'{a: 1, b: 2};
  logic [7:0] fill = '1;
endmodule
""",
    "casts": """
module m;
  assign a = 8'(x);
  assign b = signed'(x);
  assign c = unsigned'(x);
  assign d = int'(x);
  assign e = my_t'(x);
  assign f = W'(x + 1);
  assign g = (W+1)'(x);
  assign h = logic'(x);
  assign i = pkg::t_t'(x);
endmodule
""",
    "interfaces_modports": """
interface bus_if #(parameter AW = 4) (input logic clk);
  logic [AW-1:0] addr;
  logic valid, ready;
  modport mst (output addr, valid, input ready, clk);
  modport slv (input addr, valid, output ready);
  function logic ok(); return valid & ready; endfunction
endinterface
module top(input logic clk);
  bus_if #(.AW(8)) b (clk);
  child u (.bus(b.mst), .clk);
endmodule
module child(bus_if.mst bus, bus_if other, interface.slv gen, input logic clk);
endmodule
""",
    "parameterized_types": """
module m #(parameter type T = logic [7:0], parameter T INIT = '0) (input T a, output T y);
  typedef T [1:0] pair_t;
  pair_t p;
  sub #(.T(logic [3:0])) u (.a(a));
  sub #(logic [3:0]) u2 (.a(a));
endmodule
""",
    "tristate": """
module m(inout wire [7:0] io, input en, input [7:0] d);
  assign io = en ? d : 8'bz;
  assign io = en ? d : {8{1'bz}};
  bufif1 (strong0, weak1) bt (io[0], d[0], en);
  tri1 pu;
endmodule
""",
    "defparam": """
module m;
  sub u1 (.a(1));
  defparam u1.W = 16;
  defparam u1.inner.X = 3, u1.Y = 4;
endmodule
""",
    "gates": """
module m;
  and a1 (o, a, b);
  nand (o2, a, b, c);
  or #1 o1 (o3, a, b), o2x (o4, c, d);
  xor (o5, a, b);
  not (o6, a);
  buf b1 (o7, a);
  notif0 (o8, a, e);
  nmos (o9, a, e);
  pullup (p1);
  and ga[3:0] (oo, aa, bb);
endmodule
""",
    "instances": """
module m;
  sub u0 (a, b, c);
  sub #(8) u1 (.a(a), .b(b), .c());
  sub #(.W(8), .D(4)) u2 (.*);
  sub u3 (.a, .b);
  sub u4 [3:0] (.a(a), .b(b));
  sub u5 (a, , c), u6 (d, e, f);
endmodule
""",
    "hier_refs": """
module m;
  assign x = top.u1.sig;
  assign y = u1.inner[3].out;
endmodule
""",
    "skipped": """
module m;
  initial begin x = 0; #5 x = 1; end
  final $display("done");
  property p; a ##1 b; endproperty
  sequence s; a ##1 b; endsequence
  assert property (@(posedge clk) a |-> b);
  assume property (@(posedge clk) a);
  cover property (@(posedge clk) a);
  a1: assert (x == 0) else $error("bad");
  covergroup cg @(posedge clk); coverpoint x; endgroup
  clocking cb @(posedge clk); endclocking
  specify (a => b) = 1; endspecify
  class c; int x; function void f(); endfunction endclass
  program pr; endprogram
  checker ck; endchecker
  import "DPI-C" function int dpi(input int a);
  bind other chk u (.*);
  always #5 clk = ~clk;
  always @(posedge clk) begin
    $display("x=%d", x);
    $error("bad");
    q <= #1 d;
    assert (q == d);
  end
endmodule
primitive udp (o, a, b);
  output o; input a, b;
  table 0 0 : 0; 0 1 : 1; endtable
endprimitive
""",
    "attributes": """
module m;
  (* keep *) wire w;
  always @(*) (* parallel_case *) case (s) 0: y = 1; default: y = 0; endcase
  always @(*) case (s) // synopsys full_case parallel_case
    0: y = 1;
  endcase
  (* synthesis, keep = 1 *) reg r;
endmodule
""",
    "numbers": """
module m;
  localparam A = 8'hFF, B = 4'b10_10, C = 'o77, D = 32'd1_000, E = 4'sb1010, F = 8'hx_z, G = 'h0;
  wire [3:0] w = 4'b1?0z;
  localparam H = 1.5, I = 2e3, J = 10ns;
endmodule
""",
    "ops": """
module m;
  assign y = a ** b * c / d % e + f - g << 1 >> 2 <<< 3 >>> 4;
  assign z = a < b <= c > d >= e == f != g === h !== i ==? j !=? k;
  assign w = a & b ^ c ~^ d ^~ e | f && g || h;
  assign v = ~a + !b - -c + &d + |e + ^f + ~&g + ~|h + ~^i;
  assign u = a ? b : c ? d : e;
  assign t = (a + b) * c;
  assign s = a[3:0] + b[i +: 4] + c[j -: 2] + d[k];
endmodule
""",
    "stmts_misc": """
module m;
  always_comb begin : named
    automatic int t = 0;
    integer k;
    y += 1; y -= 1; y *= 2; y <<= 1; y >>>= 1; y |= 1; y &= 1; y ^= 1;
    i++; --j;
    foreach (arr[i, j]) y = 0;
    do y = y + 1; while (y < 3);
    forever begin break; end
    {a, b} = {c, d};
    pkg::fn(x);
    obj.method(1, .named(2));
  end
endmodule
""",
    "legacy_verilog": """
(* top *) module t(input clk, input [3:0] a, output reg [3:0] y, (* keep *) output wire o, );
  (* keep *) reg [3:0] z;
  always @(posedge clk)
    if (a == 4'd1) y <= #1 y + 1;
    else if (a[0]) y <= 0;
    else y <= y;
  wire [3:0] n = ~a;
  genvar i;
  generate for (i = 0; i < 4; i = i + 1) begin : g
     if (i == 0) begin : b0 assign n[0] = a[0]; end
  end endgenerate
  lbl: for (i = 0; i < 2; i = i + 1) begin end
  integer j;
  always @* for (j = 0; j < 4; j = j + 1) z[j] = a[3-j];
  function [3:0] inc; input [3:0] v; inc = v + 1; endfunction
  assign x = (a == 3) ? 1'b1 : (a == 4) ? 1'b0 : 1'bz;
  always @(posedge clk) begin : blk
     reg [1:0] tmp;
     tmp = 2'b11;
  end
  always @(posedge clk) y <= a ? {a[2:0], 1'b0} : $unsigned(a) >>> 1;
endmodule
""",
}

# Smaller inputs with deliberate mistakes: name -> (text, expected [(line, col, message)], modules complete?)
