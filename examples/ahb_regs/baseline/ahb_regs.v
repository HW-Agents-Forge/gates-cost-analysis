// AHB-Lite slave register bank, BASELINE: no protections, no IRQ, never answers ERROR.
// Register map (word index = HADDR[5:2]): 0 ID(RO) 1 CTRL 2 STATUS(RO) 5 TIMER(RO) 6 RELOAD 8..8+NDATA-1 DATA.
module ahb_regs #(parameter ADDR_W = 8, parameter DATA_W = 32, parameter NDATA = 4) (
  input  wire              HCLK,
  input  wire              HRESETn,
  input  wire              HSEL,
  input  wire [ADDR_W-1:0] HADDR,
  input  wire [1:0]        HTRANS,
  input  wire              HWRITE,
  input  wire [2:0]        HSIZE,
  input  wire [3:0]        HPROT,
  input  wire [DATA_W-1:0] HWDATA,
  input  wire              HREADY,
  output reg  [DATA_W-1:0] HRDATA,
  output wire              HREADYOUT,
  output wire              HRESP
);
  assign HREADYOUT = 1'b1;
  assign HRESP     = 1'b0;

  // address phase -> registered data phase
  wire       a_valid = HSEL & HTRANS[1] & HREADY;
  reg        d_wr;
  reg        d_rd;
  reg  [3:0] d_idx;
  reg  [3:0] d_be;
  wire [3:0] a_be = (HSIZE == 3'd0) ? (4'b0001 << HADDR[1:0]) :
                    (HSIZE == 3'd1) ? (HADDR[1] ? 4'b1100 : 4'b0011) : 4'b1111;
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) begin
      d_wr <= 1'b0; d_rd <= 1'b0; d_idx <= 4'd0; d_be <= 4'd0;
    end else if (HREADY) begin
      d_wr  <= a_valid & HWRITE;
      d_rd  <= a_valid & ~HWRITE;
      d_idx <= HADDR[5:2];
      d_be  <= a_be;
    end
  end

  wire [DATA_W-1:0] wmask = {{8{d_be[3]}}, {8{d_be[2]}}, {8{d_be[1]}}, {8{d_be[0]}}};

  // registers
  reg  [7:0]        ctrl;
  reg  [DATA_W-1:0] reload;
  reg  [DATA_W-1:0] cnt;
  reg  [DATA_W-1:0] data [0:NDATA-1];
  wire              en = ctrl[0];
  integer i;
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) begin
      ctrl <= 8'd0; reload <= {DATA_W{1'b1}}; cnt <= {DATA_W{1'b1}};
      for (i = 0; i < NDATA; i = i + 1) data[i] <= {DATA_W{1'b0}};
    end else begin
      if (d_wr && d_idx == 4'd1) ctrl   <= (ctrl & ~wmask[7:0]) | (HWDATA[7:0] & wmask[7:0]);
      if (d_wr && d_idx == 4'd6) reload <= (reload & ~wmask) | (HWDATA & wmask);
      for (i = 0; i < NDATA; i = i + 1)
        if (d_wr && d_idx == 4'd8 + i) data[i] <= (data[i] & ~wmask) | (HWDATA & wmask);
      if (en) cnt <= (cnt == {DATA_W{1'b0}}) ? reload : cnt - 1'b1;
    end
  end

  // read mux
  integer j;
  always @* begin
    HRDATA = {DATA_W{1'b0}};
    case (d_idx)
      4'd0: HRDATA = 32'hA4B00103;
      4'd1: HRDATA = {24'd0, ctrl};
      4'd2: HRDATA = {31'd0, en};
      4'd5: HRDATA = cnt;
      4'd6: HRDATA = reload;
      default: for (j = 0; j < NDATA; j = j + 1) if (d_idx == 4'd8 + j) HRDATA = data[j];
    endcase
  end
endmodule
