module t(input [7:0] a, input [2:0] s, output [7:0] y);
  assign y = (a << s) | (a >> (8 - s));
endmodule
