module t(input [1:0] s, input [31:0] d, output [7:0] y);
  wire [7:0] v0 = d[0 +: 8];
  wire [7:0] v1 = d[8 +: 8];
  wire [7:0] v2 = d[16 +: 8];
  wire [7:0] v3 = d[24 +: 8];
  reg [7:0] y_r;
  assign y = y_r;
  always @* case (s)
      2'd0: y_r = v0;
      2'd1: y_r = v1;
      2'd2: y_r = v2;
      2'd3: y_r = v3;
      default: y_r = v0;
  endcase
endmodule
