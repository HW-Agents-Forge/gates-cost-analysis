// 4-tap FIR, products summed combinationally.
module fir(input clk, input signed [15:0] x, output reg signed [31:0] y);
  reg signed [15:0] d0, d1, d2, d3;
  always @(posedge clk) begin
    d0 <= x; d1 <= d0; d2 <= d1; d3 <= d2;
    y <= d0*3 + d1*5 + d2*5 + d3*3;
  end
endmodule
