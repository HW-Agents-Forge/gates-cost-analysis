// Address-phase protection checks: out of range, misaligned, write to RO, privileged-only regions.
module ahb_prot #(parameter ADDR_W = 8, parameter NDATA = 4) (
  input  wire [ADDR_W-1:0] HADDR,
  input  wire              HWRITE,
  input  wire [2:0]        HSIZE,
  input  wire [3:0]        HPROT,
  output wire              err
);
  wire [3:0] idx   = HADDR[5:2];
  wire oor         = (HADDR[ADDR_W-1:6] != 0) | (idx >= 4'd8 + NDATA);
  wire misaligned  = (HSIZE > 3'd2) | ((HSIZE == 3'd1) & HADDR[0]) | ((HSIZE == 3'd2) & (HADDR[1:0] != 2'b00));
  wire ro_write    = HWRITE & ((idx == 4'd0) | (idx == 4'd5));
  wire priv_only   = (idx == 4'd1) | (idx == 4'd6) | (idx == 4'd7) | (idx >= 4'd12);
  wire priv_viol   = priv_only & ~HPROT[1];
  assign err = oor | misaligned | ro_write | priv_viol;
endmodule
