module t(input [15:0] a, output reg [4:0] y);
  integer i;
  always @* begin y = 0; for (i = 0; i < 16; i = i + 1) y = y + a[i]; end
endmodule
