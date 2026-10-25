module t(input clk, we, input [5:0] wa, ra, input [7:0] wd, output [7:0] rd);
  reg [7:0] m [0:63];
  always @(posedge clk) if (we) m[wa] <= wd;
  assign rd = m[ra];
endmodule
