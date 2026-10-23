// Parameterized FIFO using $clog2, a generate-selected output register and a first-word-fall-through option
module fifo_gen #(parameter DEPTH = 32, parameter WIDTH = 16, parameter FWFT = 1)(
  input  logic clk, rst, push, pop,
  input  logic [WIDTH-1:0] din,
  output logic [WIDTH-1:0] dout,
  output logic full, empty,
  output logic [$clog2(DEPTH):0] count);

  localparam AW = $clog2(DEPTH);
  logic [WIDTH-1:0] mem [DEPTH];
  logic [AW:0] wp, rp;

  assign count = wp - rp;
  assign empty = (wp == rp);
  assign full = (count == DEPTH);

  always_ff @(posedge clk) begin
    if (rst) begin
      wp <= '0;
      rp <= '0;
    end else begin
      if (push && !full) begin
        mem[wp[AW-1:0]] <= din;
        wp <= wp + 1'b1;
      end
      if (pop && !empty) rp <= rp + 1'b1;
    end
  end

  generate
    if (FWFT) begin : g_fwft
      assign dout = mem[rp[AW-1:0]];
    end else begin : g_reg
      always_ff @(posedge clk) if (pop && !empty) dout <= mem[rp[AW-1:0]];
    end
  endgenerate
endmodule
