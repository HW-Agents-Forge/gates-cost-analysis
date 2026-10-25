module t(input clk, rst, input [7:0] d, output reg [7:0] q);
  always @(posedge clk) if (rst) q <= 0; else q <= d;
endmodule
