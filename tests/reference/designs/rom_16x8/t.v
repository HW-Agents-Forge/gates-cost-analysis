module t(input [3:0] a, output reg [7:0] y);
  always @* case (a)
    4'd0: y = 8'h16;
    4'd1: y = 8'h27;
    4'd2: y = 8'h1c;
    4'd3: y = 8'h96;
    4'd4: y = 8'h1c;
    4'd5: y = 8'h62;
    4'd6: y = 8'hcf;
    4'd7: y = 8'had;
    4'd8: y = 8'h77;
    4'd9: y = 8'h33;
    4'd10: y = 8'hc4;
    4'd11: y = 8'haf;
    4'd12: y = 8'hb2;
    4'd13: y = 8'h36;
    4'd14: y = 8'h69;
    4'd15: y = 8'h99;
    default: y = 8'h00;
  endcase
endmodule
