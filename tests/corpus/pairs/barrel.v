// 32-bit logical right shifter / rotator built from five mux stages
module barrel(input [31:0] a, input [4:0] sh, input rot, output [31:0] y);
  wire [31:0] s0 = sh[0] ? {rot & a[0], a[31:1]} : a;
  wire [31:0] s1 = sh[1] ? {{2{rot}} & s0[1:0], s0[31:2]} : s0;
  wire [31:0] s2 = sh[2] ? {{4{rot}} & s1[3:0], s1[31:4]} : s1;
  wire [31:0] s3 = sh[3] ? {{8{rot}} & s2[7:0], s2[31:8]} : s2;
  wire [31:0] s4 = sh[4] ? {{16{rot}} & s3[15:0], s3[31:16]} : s3;
  assign y = s4;
endmodule
