module t(input clk, rst_n, input [0:0] d, output reg [0:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= d;
endmodule
