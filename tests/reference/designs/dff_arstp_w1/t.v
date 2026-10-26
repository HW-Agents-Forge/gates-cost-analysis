module t(input clk, rst, input [0:0] d, output reg [0:0] q);
  always @(posedge clk or posedge rst) if (rst) q <= 0; else q <= d;
endmodule
