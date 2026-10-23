// Direct-mapped cache tag array: 64 sets, valid bit, tag compare and fill
module cache_tag(input clk, input rst, input [25:0] addr, input fill, input invalidate,
                 output hit, output [19:0] victim_tag, output victim_valid);
  reg [19:0] tags [0:63];
  reg [63:0] valid;
  wire [5:0] index = addr[5:0];
  wire [19:0] tag = addr[25:6];
  wire [19:0] stored = tags[index];
  assign hit = valid[index] && (stored == tag);
  assign victim_tag = stored;
  assign victim_valid = valid[index];
  always @(posedge clk) begin
    if (rst) valid <= 64'd0;
    else begin
      if (fill) begin
        tags[index] <= tag;
        valid[index] <= 1'b1;
      end else if (invalidate) valid[index] <= 1'b0;
    end
  end
endmodule
