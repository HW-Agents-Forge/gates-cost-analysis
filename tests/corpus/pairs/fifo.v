// Synchronous FIFO, 16 x 8, binary pointers with a wrap bit, asynchronous read
module fifo(input clk, input rst, input push, input pop, input [7:0] din,
            output [7:0] dout, output full, output empty, output [4:0] level);
  reg [7:0] mem [0:15];
  reg [4:0] wp, rp;
  assign empty = wp == rp;
  assign full = (wp[4] != rp[4]) && (wp[3:0] == rp[3:0]);
  assign level = wp - rp;
  assign dout = mem[rp[3:0]];
  always @(posedge clk) begin
    if (rst) begin
      wp <= 5'd0;
      rp <= 5'd0;
    end else begin
      if (push && !full) begin
        mem[wp[3:0]] <= din;
        wp <= wp + 5'd1;
      end
      if (pop && !empty) rp <= rp + 5'd1;
    end
  end
endmodule
