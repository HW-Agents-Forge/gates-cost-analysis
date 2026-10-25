module t(input signed [31:0] a, input [4:0] s, output signed [31:0] y);
  assign y = a >>> s;
endmodule
