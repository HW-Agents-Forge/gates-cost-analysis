// AHB-Lite slave interface: address-phase decode, registered data phase, two-cycle ERROR response.
module ahb_if #(parameter ADDR_W = 8) (
  input  wire              HCLK,
  input  wire              HRESETn,
  input  wire              HSEL,
  input  wire [ADDR_W-1:0] HADDR,
  input  wire [1:0]        HTRANS,
  input  wire              HWRITE,
  input  wire [2:0]        HSIZE,
  input  wire              HREADY,
  input  wire              a_err,      // address-phase violation (from ahb_prot)
  output wire              HREADYOUT,
  output wire              HRESP,
  output reg               d_wr,       // data-phase write (never on error)
  output reg  [3:0]        d_idx,
  output reg  [3:0]        d_be,
  output wire              err_pulse   // first cycle of an error response
);
  wire a_valid = HSEL & HTRANS[1] & HREADY;
  wire [3:0] a_be = (HSIZE == 3'd0) ? (4'b0001 << HADDR[1:0]) :
                    (HSIZE == 3'd1) ? (HADDR[1] ? 4'b1100 : 4'b0011) : 4'b1111;
  reg err1, err2;
  assign HREADYOUT = ~err1;
  assign HRESP     = err1 | err2;
  assign err_pulse = err1;
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) begin
      d_wr <= 1'b0; d_idx <= 4'd0; d_be <= 4'd0; err1 <= 1'b0; err2 <= 1'b0;
    end else begin
      err2 <= err1;
      if (err1) err1 <= 1'b0;
      else if (a_valid & a_err) err1 <= 1'b1;
      if (HREADY & HREADYOUT) begin
        d_wr  <= a_valid & HWRITE & ~a_err;
        d_idx <= HADDR[5:2];
        d_be  <= a_be;
      end
    end
  end
endmodule
