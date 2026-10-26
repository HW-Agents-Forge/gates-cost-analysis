module t(input clk, rst, en, input [0:0] d, output reg [0:0] q);
  always @(posedge clk or posedge rst) if (rst) q <= 0; else if (en) q <= d;
endmodule
