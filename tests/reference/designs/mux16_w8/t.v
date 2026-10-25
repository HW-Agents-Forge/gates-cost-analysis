module t(input [3:0] s, input [127:0] d, output [7:0] y);
  wire [7:0] v0 = d[0 +: 8];
  wire [7:0] v1 = d[8 +: 8];
  wire [7:0] v2 = d[16 +: 8];
  wire [7:0] v3 = d[24 +: 8];
  wire [7:0] v4 = d[32 +: 8];
  wire [7:0] v5 = d[40 +: 8];
  wire [7:0] v6 = d[48 +: 8];
  wire [7:0] v7 = d[56 +: 8];
  wire [7:0] v8 = d[64 +: 8];
  wire [7:0] v9 = d[72 +: 8];
  wire [7:0] v10 = d[80 +: 8];
  wire [7:0] v11 = d[88 +: 8];
  wire [7:0] v12 = d[96 +: 8];
  wire [7:0] v13 = d[104 +: 8];
  wire [7:0] v14 = d[112 +: 8];
  wire [7:0] v15 = d[120 +: 8];
  reg [7:0] y_r;
  assign y = y_r;
  always @* case (s)
      4'd0: y_r = v0;
      4'd1: y_r = v1;
      4'd2: y_r = v2;
      4'd3: y_r = v3;
      4'd4: y_r = v4;
      4'd5: y_r = v5;
      4'd6: y_r = v6;
      4'd7: y_r = v7;
      4'd8: y_r = v8;
      4'd9: y_r = v9;
      4'd10: y_r = v10;
      4'd11: y_r = v11;
      4'd12: y_r = v12;
      4'd13: y_r = v13;
      4'd14: y_r = v14;
      4'd15: y_r = v15;
      default: y_r = v0;
  endcase
endmodule
