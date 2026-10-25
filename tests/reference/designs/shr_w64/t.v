module t(input [63:0] a, input [5:0] s, output [63:0] y);
  assign y = a >> s;
endmodule
