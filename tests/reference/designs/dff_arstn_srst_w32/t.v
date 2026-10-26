module t(input clk, rst_n, srst, input [31:0] d, output reg [31:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (srst) q <= 0; else q <= d;
endmodule
