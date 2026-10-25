module t(input [2:0] s, input [255:0] d, output [31:0] y);
  assign y = d[s*32 +: 32];
endmodule
