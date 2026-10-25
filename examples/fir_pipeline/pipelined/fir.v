// Same FIR with the products registered before the sum.
module fir(input clk, input signed [15:0] x, output reg signed [31:0] y);
  reg signed [15:0] d0, d1, d2, d3;
  reg signed [31:0] c0, c1, c2, c3;
  always @(posedge clk) begin
    d0 <= x; d1 <= d0; d2 <= d1; d3 <= d2;
    c0 <= d0*3; c1 <= d1*5; c2 <= d2*5; c3 <= d3*3;
    y <= c0 + c1 + c2 + c3;
  end
endmodule
