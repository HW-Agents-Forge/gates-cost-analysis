// UART transmitter. `bit` is a SystemVerilog keyword but a legal identifier in plain Verilog: gatecost accepts it.
module uart_tx #(parameter DIV = 434)(input clk, rst, start, input [7:0] data, output reg tx, output busy);
  reg [8:0] cnt; reg [3:0] bit; reg [9:0] sh; reg act;
  assign busy = act;
  always @(posedge clk) begin
    if (rst) begin act <= 0; tx <= 1; cnt <= 0; bit <= 0; end
    else if (!act) begin if (start) begin act <= 1; sh <= {1'b1, data, 1'b0}; cnt <= 0; bit <= 0; end end
    else begin
      if (cnt == DIV-1) begin cnt <= 0; tx <= sh[0]; sh <= {1'b1, sh[9:1]}; bit <= bit + 1; if (bit == 9) act <= 0; end
      else cnt <= cnt + 1;
    end
  end
endmodule
