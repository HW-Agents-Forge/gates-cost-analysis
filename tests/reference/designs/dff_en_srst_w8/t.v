module t(input clk, rst, en, input [7:0] d, output reg [7:0] q);
  always @(posedge clk) if (rst) q <= 0; else if (en) q <= d;
endmodule
