// Four-requester round-robin arbiter using a rotating mask
module arbiter(input clk, input rst, input [3:0] req, output [3:0] grant, output any_req);
  reg [3:0] mask;
  wire [3:0] mreq = req & mask;
  wire [3:0] sel = (mreq != 4'd0) ? mreq : req;
  assign grant = sel & (~sel + 4'd1);
  assign any_req = req != 4'd0;
  always @(posedge clk) begin
    if (rst) mask <= 4'b1111;
    else if (grant != 4'd0) mask <= ~((grant << 1) - 4'd1);
  end
endmodule
