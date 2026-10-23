// SPI master, mode 0, 8-bit frames, clock divided by an even divisor
module spi_master(input clk, input rst, input start, input [7:0] tx_data, input [7:0] div,
                  input miso, output mosi, output reg sclk, output reg cs_n,
                  output reg [7:0] rx_data, output reg done);
  reg [7:0] sh_out;
  reg [7:0] sh_in;
  reg [7:0] cnt;
  reg [3:0] bits;
  reg active;
  assign mosi = sh_out[7];
  always @(posedge clk) begin
    done <= 1'b0;
    if (rst) begin
      active <= 1'b0;
      sclk <= 1'b0;
      cs_n <= 1'b1;
      cnt <= 8'd0;
      bits <= 4'd0;
      sh_out <= 8'd0;
      sh_in <= 8'd0;
      rx_data <= 8'd0;
    end else if (!active) begin
      if (start) begin
        active <= 1'b1;
        cs_n <= 1'b0;
        sh_out <= tx_data;
        cnt <= div;
        bits <= 4'd0;
      end
    end else begin
      if (cnt == 8'd0) begin
        cnt <= div;
        sclk <= ~sclk;
        if (!sclk) begin
          sh_in <= {sh_in[6:0], miso};
        end else begin
          sh_out <= {sh_out[6:0], 1'b0};
          bits <= bits + 4'd1;
          if (bits == 4'd7) begin
            active <= 1'b0;
            cs_n <= 1'b1;
            rx_data <= sh_in;
            done <= 1'b1;
          end
        end
      end else cnt <= cnt - 8'd1;
    end
  end
endmodule
