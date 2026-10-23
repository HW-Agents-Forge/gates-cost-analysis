// Multiply-accumulate array with a generate loop, clock-enable pipeline and saturation
module mac_array #(parameter N = 4, parameter W = 8)(
  input  logic clk, rst, en, clear,
  input  logic signed [W-1:0] a [N],
  input  logic signed [W-1:0] b [N],
  output logic signed [2*W+$clog2(N)-1:0] sum,
  output logic saturated);

  localparam SW = 2*W + $clog2(N);
  logic signed [2*W-1:0] prod [N];
  logic signed [SW-1:0] acc, tot;

  genvar i;
  generate
    for (i = 0; i < N; i++) begin : g_mul
      always_ff @(posedge clk) if (en) prod[i] <= a[i] * b[i];
    end
  endgenerate

  always_comb begin
    tot = '0;
    for (int k = 0; k < N; k++) tot = tot + prod[k];
  end

  localparam signed [SW-1:0] MAXV = {1'b0, {(SW-1){1'b1}}};
  logic signed [SW:0] wide;
  assign wide = acc + tot;
  assign saturated = wide > MAXV;

  always_ff @(posedge clk) begin
    if (rst || clear) acc <= '0;
    else if (en) acc <= saturated ? MAXV : wide[SW-1:0];
  end
  assign sum = acc;
endmodule
