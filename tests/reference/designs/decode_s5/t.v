module t(input [4:0] a, output [31:0] y);
  assign y = 32'd1 << a;
endmodule
