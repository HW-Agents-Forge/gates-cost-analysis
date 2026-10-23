"""Realistic SystemVerilog designs, one per language feature (counter, FIFO control, ALU, FSM,
register file, adder tree, multiplier).  Shared by the elaborator tests and, as a corpus, by the
cross-language equivalence tests (FRONTEND.md section 10.7)."""

COUNTER = """
module counter #(parameter W = 8)(
  input clk, input rst, input en, input load, input down, input [W-1:0] din,
  output reg [W-1:0] q, output wrap);
  assign wrap = en & ~load & (down ? (q == {W{1'b0}}) : (q == {W{1'b1}}));
  always @(posedge clk) begin
    if (rst) q <= {W{1'b0}};
    else if (load) q <= din;
    else if (en) q <= down ? q - 1'b1 : q + 1'b1;
  end
endmodule
"""

FIFO_CTRL = """
module fifo_ctrl #(parameter AW = 3)(
  input clk, input rst_n, input push, input pop,
  output full, output empty, output [AW-1:0] waddr, output [AW-1:0] raddr, output reg [AW:0] count);
  reg [AW:0] wptr, rptr;
  wire do_push = push & ~full;
  wire do_pop  = pop & ~empty;
  assign full  = (count == (1 << AW));
  assign empty = (count == 0);
  assign waddr = wptr[AW-1:0];
  assign raddr = rptr[AW-1:0];
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wptr <= 0; rptr <= 0; count <= 0;
    end else begin
      if (do_push) wptr <= wptr + 1;
      if (do_pop)  rptr <= rptr + 1;
      case ({do_push, do_pop})
        2'b10: count <= count + 1;
        2'b01: count <= count - 1;
        default: count <= count;
      endcase
    end
  end
endmodule
"""

ALU = """
module alu #(parameter W = 8)(
  input [W-1:0] a, input [W-1:0] b, input [3:0] op,
  output reg [W-1:0] y, output zero, output lt_s, output lt_u);
  always @* begin
    case (op)
      4'd0: y = a + b;
      4'd1: y = a - b;
      4'd2: y = a & b;
      4'd3: y = a | b;
      4'd4: y = a ^ b;
      4'd5: y = a << b[2:0];
      4'd6: y = a >> b[2:0];
      4'd7: y = $signed(a) >>> b[2:0];
      4'd8: y = ~a;
      default: y = {W{1'b0}};
    endcase
  end
  assign zero = (y == 0);
  assign lt_s = $signed(a) < $signed(b);
  assign lt_u = a < b;
endmodule
"""

FSM = """
module seq_detect (input clk, input rst_n, input x, output y, output [1:0] state_o);
  typedef enum logic [1:0] {S0, S1, S10, S101} state_t;
  state_t state, next;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) state <= S0;
    else state <= next;
  always_comb begin
    next = state;
    case (state)
      S0:   next = x ? S1 : S0;
      S1:   next = x ? S1 : S10;
      S10:  next = x ? S101 : S0;
      S101: next = x ? S1 : S10;
      default: next = S0;
    endcase
  end
  assign y = (state == S101) & x;
  assign state_o = state;
endmodule
"""

REGFILE = """
module regfile #(parameter AW = 3, parameter DW = 8)(
  input clk, input we, input [AW-1:0] waddr, input [AW-1:0] raddr1, input [AW-1:0] raddr2,
  input [DW-1:0] wdata, output [DW-1:0] rdata1, output [DW-1:0] rdata2);
  reg [DW-1:0] mem [0:(1<<AW)-1];
  always @(posedge clk) if (we && waddr != 0) mem[waddr] <= wdata;
  assign rdata1 = (raddr1 == 0) ? {DW{1'b0}} : mem[raddr1];
  assign rdata2 = (raddr2 == 0) ? {DW{1'b0}} : mem[raddr2];
endmodule
"""

ADDER_TREE = """
module adder_tree #(parameter N = 8, parameter W = 8)(
  input [N*W-1:0] in, output [W+$clog2(N)-1:0] sum);
  localparam OW = W + $clog2(N);
  generate
    if (N == 1) begin : leaf
      assign sum = in;
    end else begin : node
      localparam H = N / 2;
      wire [W+$clog2(H)-1:0] ls, rs;
      adder_tree #(.N(H), .W(W)) l (.in(in[H*W-1:0]), .sum(ls));
      adder_tree #(.N(N-H), .W(W)) r (.in(in[N*W-1:H*W]), .sum(rs));
      assign sum = ls + rs;
    end
  endgenerate
endmodule
"""

MULTIPLIER = """
module mult_seq #(parameter W = 8)(
  input clk, input rst_n, input start, input [W-1:0] a, input [W-1:0] b,
  output reg done, output reg [2*W-1:0] p);
  reg [2*W-1:0] acc, mc;
  reg [W-1:0] mr;
  reg [$clog2(W+1)-1:0] n;
  reg busy;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin acc <= 0; mc <= 0; mr <= 0; n <= 0; busy <= 0; done <= 0; p <= 0; end
    else begin
      done <= 1'b0;
      if (start && !busy) begin
        acc <= 0; mc <= {{W{1'b0}}, a}; mr <= b; n <= W; busy <= 1'b1;
      end else if (busy) begin
        if (mr[0]) acc <= acc + mc;
        mc <= mc << 1;
        mr <= mr >> 1;
        n <= n - 1;
        if (n == 1) begin busy <= 1'b0; done <= 1'b1; p <= mr[0] ? acc + mc : acc; end
      end
    end
  end
endmodule

module mult_comb #(parameter W = 8)(input signed [W-1:0] a, input signed [W-1:0] b, output signed [2*W-1:0] p);
  assign p = a * b;
endmodule
"""

DESIGNS = {
    "counter": (COUNTER, "counter"), "fifo_ctrl": (FIFO_CTRL, "fifo_ctrl"), "alu": (ALU, "alu"),
    "fsm": (FSM, "seq_detect"), "regfile": (REGFILE, "regfile"), "adder_tree": (ADDER_TREE, "adder_tree"),
    "multiplier": (MULTIPLIER, "mult_seq"),
}

REAL_IDIOMS = r"""
module uart_tx #(parameter CLKS_PER_BIT = 16)(
  input wire clk, input wire rst_n, input wire tx_start, input wire [7:0] tx_data,
  output reg tx, output wire busy, output reg done);
  localparam IDLE = 3'd0, START = 3'd1, DATA = 3'd2, STOP = 3'd3, CLEANUP = 3'd4;
  reg [2:0] state;
  reg [$clog2(CLKS_PER_BIT)-1:0] clk_count;
  reg [2:0] bit_idx;
  reg [7:0] data;
  assign busy = (state != IDLE);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; tx <= 1'b1; done <= 1'b0; clk_count <= 0; bit_idx <= 0; data <= 0;
    end else begin
      done <= 1'b0;
      case (state)
        IDLE: begin
          tx <= 1'b1; clk_count <= 0; bit_idx <= 0;
          if (tx_start) begin data <= tx_data; state <= START; end
        end
        START: begin
          tx <= 1'b0;
          if (clk_count < CLKS_PER_BIT-1) clk_count <= clk_count + 1;
          else begin clk_count <= 0; state <= DATA; end
        end
        DATA: begin
          tx <= data[bit_idx];
          if (clk_count < CLKS_PER_BIT-1) clk_count <= clk_count + 1;
          else begin
            clk_count <= 0;
            if (bit_idx < 7) bit_idx <= bit_idx + 1;
            else begin bit_idx <= 0; state <= STOP; end
          end
        end
        STOP: begin
          tx <= 1'b1;
          if (clk_count < CLKS_PER_BIT-1) clk_count <= clk_count + 1;
          else begin done <= 1'b1; clk_count <= 0; state <= CLEANUP; end
        end
        CLEANUP: state <= IDLE;
        default: state <= IDLE;
      endcase
    end
  end
endmodule

module crc16 (input wire [7:0] d, input wire [15:0] c, output wire [15:0] n);
  function [15:0] nxt;
    input [7:0] data; input [15:0] crc;
    reg [15:0] x; integer i;
    begin
      x = crc;
      for (i = 0; i < 8; i = i + 1) begin
        if ((x[15] ^ data[7-i]) == 1'b1) x = {x[14:0], 1'b0} ^ 16'h1021;
        else x = {x[14:0], 1'b0};
      end
      nxt = x;
    end
  endfunction
  assign n = nxt(d, c);
endmodule

module prio_enc #(parameter N = 8)(input wire [N-1:0] req, output reg [$clog2(N)-1:0] idx, output reg valid);
  integer i;
  always @* begin
    idx = 0; valid = 1'b0;
    for (i = N-1; i >= 0; i = i - 1)
      if (req[i]) begin idx = i[$clog2(N)-1:0]; valid = 1'b1; end
  end
endmodule

module barrel #(parameter W = 16)(input wire [W-1:0] in, input wire [$clog2(W)-1:0] sh, output wire [W-1:0] out);
  wire [W-1:0] stage [0:$clog2(W)];
  assign stage[0] = in;
  genvar s;
  generate for (s = 0; s < $clog2(W); s = s + 1) begin : st
    assign stage[s+1] = sh[s] ? {stage[s][W-1-(1<<s):0], {(1<<s){1'b0}}} : stage[s];
  end endgenerate
  assign out = stage[$clog2(W)];
endmodule

module lfsr (input wire clk, input wire rst, output reg [15:0] q);
  wire fb = q[15] ^ q[13] ^ q[12] ^ q[10];
  always @(posedge clk) if (rst) q <= 16'hACE1; else q <= {q[14:0], fb};
endmodule

module gray #(parameter W = 4)(input wire clk, input wire rst, output wire [W-1:0] g);
  reg [W-1:0] b;
  always @(posedge clk) b <= rst ? {W{1'b0}} : b + 1'b1;
  assign g = b ^ (b >> 1);
endmodule

"""
