module ahb_timer #(parameter DATA_W = 32) (
  input  wire              HCLK,
  input  wire              HRESETn,
  input  wire              en,
  input  wire [DATA_W-1:0] reload,
  output reg  [DATA_W-1:0] cnt,
  output wire              ovf
);
  assign ovf = en & (cnt == {DATA_W{1'b0}});
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) cnt <= {DATA_W{1'b1}};
    else if (en) cnt <= ovf ? reload : cnt - 1'b1;
  end
endmodule
