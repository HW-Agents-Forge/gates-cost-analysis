module t(input [2:0] a, output [7:0] y);
  assign y = 8'd1 << a;
endmodule
