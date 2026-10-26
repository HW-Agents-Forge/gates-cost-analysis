module t(input clk, rst_n, en, input [0:0] d, output reg [0:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (en) q <= d;
endmodule
