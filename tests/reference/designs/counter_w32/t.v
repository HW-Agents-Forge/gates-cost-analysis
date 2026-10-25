module t(input clk, rn, en, output reg [31:0] q);
  always @(posedge clk or negedge rn) if (!rn) q <= 0; else if (en) q <= q + 1;
endmodule
