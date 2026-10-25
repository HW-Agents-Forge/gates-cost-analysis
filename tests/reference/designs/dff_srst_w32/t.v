module t(input clk, rst, input [31:0] d, output reg [31:0] q);
  always @(posedge clk) if (rst) q <= 0; else q <= d;
endmodule
