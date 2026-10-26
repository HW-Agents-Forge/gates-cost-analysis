module t(input clk, rst_n, input [7:0] d, output reg [7:0] q);
  always @(posedge clk or negedge rst_n) if (!rst_n) q <= {8{1'b1}}; else q <= d;
endmodule
