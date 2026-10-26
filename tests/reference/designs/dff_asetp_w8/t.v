module t(input clk, rst, input [7:0] d, output reg [7:0] q);
  always @(posedge clk or posedge rst) if (rst) q <= 8'hFF; else q <= d;
endmodule
