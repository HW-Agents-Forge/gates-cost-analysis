// Radix-4 Booth multiplier, 8x8 signed, combinational partial-product array
module booth(input signed [7:0] a, input signed [7:0] b, output reg signed [15:0] p);
  reg signed [15:0] acc;
  reg signed [15:0] pp;
  reg [2:0] grp;
  integer i;
  always @* begin
    acc = 16'sd0;
    for (i = 0; i < 4; i = i + 1) begin
      grp = (i == 0) ? {b[1:0], 1'b0} : b[2*i+1 -: 3];
      case (grp)
        3'b001, 3'b010: pp = {{8{a[7]}}, a};
        3'b011: pp = {{7{a[7]}}, a, 1'b0};
        3'b100: pp = -{{7{a[7]}}, a, 1'b0};
        3'b101, 3'b110: pp = -{{8{a[7]}}, a};
        default: pp = 16'sd0;
      endcase
      acc = acc + (pp <<< (2*i));
    end
    p = acc;
  end
endmodule
