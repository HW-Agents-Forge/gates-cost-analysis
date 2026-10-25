module t(input signed [63:0] a, input [5:0] s, output signed [63:0] y);
  assign y = a >>> s;
endmodule
