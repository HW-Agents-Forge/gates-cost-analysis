module t(input clk, rst_n, input [31:0] d, output reg [31:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= {32{1'b1}}; else q <= d;
endmodule
