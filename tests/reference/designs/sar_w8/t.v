module t(input signed [7:0] a, input [2:0] s, output signed [7:0] y);
  assign y = a >>> s;
endmodule
