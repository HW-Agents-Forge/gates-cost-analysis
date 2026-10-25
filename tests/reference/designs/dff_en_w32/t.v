module t(input clk, en, input [31:0] d, output reg [31:0] q);
  always @(posedge clk) if (en) q <= d;
endmodule
