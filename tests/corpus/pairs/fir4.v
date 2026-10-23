// 4-tap FIR filter: delay line and per-tap products built in a generate loop
module fir4 #(parameter W = 8)(input clk, input rst, input en, input signed [W-1:0] x,
                               output signed [2*W+1:0] y);
  parameter signed [7:0] C0 = 8'sd3, C1 = -8'sd5, C2 = 8'sd7, C3 = 8'sd2;
  reg signed [W-1:0] taps [0:3];
  wire signed [2*W-1:0] prod [0:3];
  genvar i;
  generate
    for (i = 0; i < 4; i = i + 1) begin : g_tap
      wire signed [7:0] c = (i == 0) ? C0 : (i == 1) ? C1 : (i == 2) ? C2 : C3;
      assign prod[i] = taps[i] * c;
      if (i > 0) begin : g_shift
        always @(posedge clk) begin
          if (rst) taps[i] <= {W{1'b0}};
          else if (en) taps[i] <= taps[i-1];
        end
      end
    end
  endgenerate
  always @(posedge clk) begin
    if (rst) taps[0] <= {W{1'b0}};
    else if (en) taps[0] <= x;
  end
  assign y = prod[0] + prod[1] + prod[2] + prod[3];
endmodule
