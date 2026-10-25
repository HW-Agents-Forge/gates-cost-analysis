module t(input clk, rn, input [31:0] d, output reg [31:0] q);
  always @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= d;
endmodule
