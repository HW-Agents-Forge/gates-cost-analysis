module t(input [15:0] a, input [3:0] s, output [15:0] y);
  assign y = (a << s) | (a >> (16 - s));
endmodule
