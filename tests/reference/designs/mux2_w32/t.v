module t(input s, input [31:0] a, b, output [31:0] y);
  assign y = s ? a : b;
endmodule
