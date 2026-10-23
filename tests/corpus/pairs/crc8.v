// Bytewise CRC-8 (poly 0x07), serial-in-byte update through a function
module crc8(input clk, input rst, input init, input valid, input [7:0] data, output [7:0] crc);
  reg [7:0] c;
  function [7:0] step(input [7:0] cur, input [7:0] d);
    integer i;
    reg [7:0] x;
    begin
      x = cur ^ d;
      for (i = 0; i < 8; i = i + 1)
        if (x[7]) x = {x[6:0], 1'b0} ^ 8'h07;
        else x = {x[6:0], 1'b0};
      step = x;
    end
  endfunction
  always @(posedge clk) begin
    if (rst || init) c <= 8'h00;
    else if (valid) c <= step(c, data);
  end
  assign crc = c;
endmodule
