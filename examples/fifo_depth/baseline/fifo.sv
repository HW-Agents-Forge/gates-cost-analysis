// Synchronous FIFO built on a register array.
module fifo #(parameter W = 8, parameter D = 16)(input logic clk, rst, wr, rd, input logic [W-1:0] din,
                                                 output logic [W-1:0] dout, output logic full, empty);
  logic [W-1:0] mem [0:D-1];
  logic [$clog2(D):0] wp, rp;
  assign full  = (wp - rp) == D;
  assign empty = (wp == rp);
  always_ff @(posedge clk) begin
    if (rst) begin wp <= 0; rp <= 0; end
    else begin
      if (wr && !full) begin mem[wp[$clog2(D)-1:0]] <= din; wp <= wp + 1; end
      if (rd && !empty) rp <= rp + 1;
    end
  end
  assign dout = mem[rp[$clog2(D)-1:0]];
endmodule
