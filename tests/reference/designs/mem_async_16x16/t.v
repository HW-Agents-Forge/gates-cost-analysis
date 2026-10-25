module t(input clk, we, input [3:0] wa, ra, input [15:0] wd, output [15:0] rd);
  reg [15:0] m [0:15];
  always @(posedge clk) if (we) m[wa] <= wd;
  assign rd = m[ra];
endmodule
