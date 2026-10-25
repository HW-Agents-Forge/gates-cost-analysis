module t(input clk, input [7:0] d, output [7:0] q);
  reg [7:0] r [0:7];
  integer i;
  always @(posedge clk) begin
    r[0] <= d;
    for (i = 1; i < 8; i = i + 1) r[i] <= r[i-1];
  end
  assign q = r[7];
endmodule
