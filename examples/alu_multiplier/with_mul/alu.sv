// 16-bit ALU, eight operations, zero flag.
module alu #(parameter W = 16)(input logic [W-1:0] a, b, input logic [2:0] op,
                               output logic [W-1:0] y, output logic zero);
  always_comb begin
    case (op)
      3'd0: y = a + b;
      3'd1: y = a - b;
      3'd2: y = a & b;
      3'd3: y = a | b;
      3'd4: y = a ^ b;
      3'd5: y = a << b[3:0];
      3'd6: y = a >> b[3:0];
      3'd7: y = a * b;
      default: y = (a < b) ? 1 : 0;
    endcase
  end
  assign zero = (y == 0);
endmodule
