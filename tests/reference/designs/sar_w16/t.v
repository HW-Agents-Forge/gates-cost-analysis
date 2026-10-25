module t(input signed [15:0] a, input [3:0] s, output signed [15:0] y);
  assign y = a >>> s;
endmodule
