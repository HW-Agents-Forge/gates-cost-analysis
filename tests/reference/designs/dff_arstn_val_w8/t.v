module t(input clk, rst_n, input [7:0] d, output reg [7:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 8'h5A; else q <= d;
endmodule
