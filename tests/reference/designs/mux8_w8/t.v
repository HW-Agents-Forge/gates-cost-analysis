module t(input [2:0] s, input [63:0] d, output [7:0] y);
  wire [7:0] v0 = d[0 +: 8];
  wire [7:0] v1 = d[8 +: 8];
  wire [7:0] v2 = d[16 +: 8];
  wire [7:0] v3 = d[24 +: 8];
  wire [7:0] v4 = d[32 +: 8];
  wire [7:0] v5 = d[40 +: 8];
  wire [7:0] v6 = d[48 +: 8];
  wire [7:0] v7 = d[56 +: 8];
  reg [7:0] y_r;
  assign y = y_r;
  always @* case (s)
      3'd0: y_r = v0;
      3'd1: y_r = v1;
      3'd2: y_r = v2;
      3'd3: y_r = v3;
      3'd4: y_r = v4;
      3'd5: y_r = v5;
      3'd6: y_r = v6;
      3'd7: y_r = v7;
      default: y_r = v0;
  endcase
endmodule
