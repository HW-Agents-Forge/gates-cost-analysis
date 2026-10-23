// 16-bit Fibonacci LFSR with load and enable (corpus design, written for gatecost tests)
module lfsr #(parameter W = 16)(
  input clk, input rst, input en, input load, input [W-1:0] seed,
  output [W-1:0] q, output bit_out);
  reg [W-1:0] r;
  wire fb = r[15] ^ r[13] ^ r[12] ^ r[10];
  always @(posedge clk) begin
    if (rst) r <= 16'hACE1;
    else if (load) r <= seed;
    else if (en) r <= {r[W-2:0], fb};
  end
  assign q = r;
  assign bit_out = r[W-1];
endmodule
