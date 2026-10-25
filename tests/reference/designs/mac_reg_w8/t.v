module t(input clk, rn, en, input [7:0] a, b, output reg [23:0] acc);
  always @(posedge clk or negedge rn) if (!rn) acc <= 0; else if (en) acc <= acc + a * b;
endmodule
