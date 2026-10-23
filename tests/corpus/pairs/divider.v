// Iterative restoring divider, 8-bit operands, one quotient bit per clock
module divider(input clk, input rst, input start, input [7:0] num, input [7:0] den,
               output reg [7:0] quo, output reg [7:0] remd, output reg done, output busy);
  reg [7:0] q;
  reg [8:0] r;
  reg [7:0] d;
  reg [3:0] cnt;
  reg run;
  wire [8:0] shifted = {r[7:0], q[7]};
  wire [8:0] diff = shifted - {1'b0, d};
  assign busy = run;
  always @(posedge clk) begin
    done <= 1'b0;
    if (rst) begin
      run <= 1'b0;
      q <= 8'd0;
      r <= 9'd0;
      d <= 8'd0;
      cnt <= 4'd0;
      quo <= 8'd0;
      remd <= 8'd0;
    end else if (!run) begin
      if (start) begin
        run <= 1'b1;
        q <= num;
        d <= den;
        r <= 9'd0;
        cnt <= 4'd0;
      end
    end else begin
      if (diff[8]) begin
        r <= shifted;
        q <= {q[6:0], 1'b0};
      end else begin
        r <= diff;
        q <= {q[6:0], 1'b1};
      end
      cnt <= cnt + 4'd1;
      if (cnt == 4'd7) begin
        run <= 1'b0;
        done <= 1'b1;
        quo <= diff[8] ? {q[6:0], 1'b0} : {q[6:0], 1'b1};
        remd <= diff[8] ? shifted[7:0] : diff[7:0];
      end
    end
  end
endmodule
