module t(input s, input [63:0] a, b, output [63:0] y);
  assign y = s ? a : b;
endmodule
