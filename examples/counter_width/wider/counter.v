// Enable counter with synchronous reset.
module counter #(parameter W = 32)(input clk, input rst, input en, output reg [W-1:0] q);
  always @(posedge clk) if (rst) q <= 0; else if (en) q <= q + 1;
endmodule
