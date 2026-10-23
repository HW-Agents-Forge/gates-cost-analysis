// UART transmitter: 8N1, programmable baud divider, start/stop framing
module uart_tx(input clk, input rst, input start, input [7:0] data, input [15:0] div,
               output reg tx, output busy);
  localparam [1:0] IDLE = 2'd0, START = 2'd1, DATA = 2'd2, STOP = 2'd3;
  reg [1:0] state;
  reg [15:0] baud_cnt;
  reg [2:0] bit_cnt;
  reg [7:0] shreg;
  wire tick = baud_cnt == 16'd0;
  assign busy = state != IDLE;
  always @(posedge clk) begin
    if (rst) begin
      state <= IDLE;
      tx <= 1'b1;
      baud_cnt <= 16'd0;
      bit_cnt <= 3'd0;
      shreg <= 8'd0;
    end else begin
      case (state)
        IDLE: begin
          tx <= 1'b1;
          if (start) begin
            shreg <= data;
            baud_cnt <= div;
            state <= START;
          end
        end
        START: begin
          tx <= 1'b0;
          if (tick) begin
            baud_cnt <= div;
            bit_cnt <= 3'd0;
            state <= DATA;
          end else baud_cnt <= baud_cnt - 16'd1;
        end
        DATA: begin
          tx <= shreg[0];
          if (tick) begin
            baud_cnt <= div;
            shreg <= {1'b0, shreg[7:1]};
            bit_cnt <= bit_cnt + 3'd1;
            if (bit_cnt == 3'd7) state <= STOP;
          end else baud_cnt <= baud_cnt - 16'd1;
        end
        default: begin
          tx <= 1'b1;
          if (tick) state <= IDLE;
          else baud_cnt <= baud_cnt - 16'd1;
        end
      endcase
    end
  end
endmodule
