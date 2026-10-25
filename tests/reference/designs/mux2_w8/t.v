module t(input s, input [7:0] a, b, output [7:0] y);
  assign y = s ? a : b;
endmodule
