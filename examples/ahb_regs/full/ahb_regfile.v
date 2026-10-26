// CTRL, STATUS (W1C bits), RELOAD, LOCK (magic key 0xA5 unlocks) and NDATA byte-enabled data registers.
module ahb_regfile #(parameter DATA_W = 32, parameter NDATA = 4) (
  input  wire              HCLK,
  input  wire              HRESETn,
  input  wire              d_wr,
  input  wire [3:0]        d_idx,
  input  wire [3:0]        d_be,
  input  wire [DATA_W-1:0] HWDATA,
  input  wire              err_pulse,
  input  wire              ovf,
  output reg  [7:0]        ctrl,
  output reg  [DATA_W-1:0] reload,
  output reg  [1:0]        st_w1c,     // {ovf_seen, err_seen}
  output reg               locked,
  output wire [NDATA*DATA_W-1:0] data_flat
);
  wire [DATA_W-1:0] wmask = {{8{d_be[3]}}, {8{d_be[2]}}, {8{d_be[1]}}, {8{d_be[0]}}};
  wire open_ok = ~locked;
  reg  [DATA_W-1:0] data [0:NDATA-1];
  genvar g;
  generate for (g = 0; g < NDATA; g = g + 1) begin : pack
    assign data_flat[g*DATA_W +: DATA_W] = data[g];
  end endgenerate
  integer i;
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) begin
      ctrl <= 8'd0; reload <= {DATA_W{1'b1}}; st_w1c <= 2'b00; locked <= 1'b1;
      for (i = 0; i < NDATA; i = i + 1) data[i] <= {DATA_W{1'b0}};
    end else begin
      if (d_wr && open_ok && d_idx == 4'd1) ctrl   <= (ctrl & ~wmask[7:0]) | (HWDATA[7:0] & wmask[7:0]);
      if (d_wr && open_ok && d_idx == 4'd6) reload <= (reload & ~wmask) | (HWDATA & wmask);
      if (d_wr && d_idx == 4'd7 && d_be[0]) locked <= (HWDATA[7:0] != 8'hA5);
      for (i = 0; i < NDATA; i = i + 1)
        if (d_wr && open_ok && d_idx == 4'd8 + i) data[i] <= (data[i] & ~wmask) | (HWDATA & wmask);
      st_w1c[0] <= (st_w1c[0] & ~(d_wr & d_idx == 4'd2 & d_be[0] & HWDATA[1])) | err_pulse;
      st_w1c[1] <= (st_w1c[1] & ~(d_wr & d_idx == 4'd2 & d_be[0] & HWDATA[3])) | ovf;
    end
  end
endmodule
