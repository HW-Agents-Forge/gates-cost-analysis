// Multiply-accumulate (signed): acc <= acc + a*b with enable and synchronous clear
module mac_s #(parameter W = 8, parameter ACC_W = 24)(input clk, input rst_n, input clear, input en,
    input signed [W-1:0] a, input signed [W-1:0] b, output reg signed [ACC_W-1:0] acc);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) acc <= 0;
    else if (clear) acc <= 0;
    else if (en) acc <= acc + a * b;
  end
endmodule
