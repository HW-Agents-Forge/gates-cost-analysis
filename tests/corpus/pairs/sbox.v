// 4-bit S-box lookup table with a registered output (case-based ROM)
module sbox(input clk, input en, input [3:0] x, output reg [3:0] y, output [3:0] y_comb);
  reg [3:0] t;
  always @* begin
    case (x)
      4'h0: t = 4'hC;  4'h1: t = 4'h5;  4'h2: t = 4'h6;  4'h3: t = 4'hB;
      4'h4: t = 4'h9;  4'h5: t = 4'h0;  4'h6: t = 4'hA;  4'h7: t = 4'hD;
      4'h8: t = 4'h3;  4'h9: t = 4'hE;  4'hA: t = 4'hF;  4'hB: t = 4'h8;
      4'hC: t = 4'h4;  4'hD: t = 4'h7;  4'hE: t = 4'h1;  default: t = 4'h2;
    endcase
  end
  assign y_comb = t;
  always @(posedge clk) if (en) y <= t;
endmodule
