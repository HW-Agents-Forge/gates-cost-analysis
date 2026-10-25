module t(input clk, we, input [3:0] wa, ra, input [7:0] wd, output reg [7:0] rd);
  reg [7:0] m [0:15];
  always @(posedge clk) begin if (we) m[wa] <= wd; rd <= m[ra]; end
endmodule
