module t(input [2:0] s, input [15:0] a, b, c, d, e, output reg [15:0] y);
  always @* case (s)
    3'd0: y = a; 3'd1: y = b; 3'd2: y = c; 3'd3: y = d; default: y = e;
  endcase
endmodule
