module t(input clk, rn, input [7:0] d, output reg [7:0] q);
  always @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= d;
endmodule
