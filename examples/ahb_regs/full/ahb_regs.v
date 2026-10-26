// AHB-Lite slave register bank, FULL: protections (lock key 0xA5, HPROT privileged regions, alignment,
// range, RO-write errors), IRQ_EN/IRQ_STAT, two-cycle ERROR response.
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
  output wire [DATA_W-1:0] HRDATA,
  output wire              HREADYOUT,
  output wire              HRESP,
  output wire              irq
);
  wire a_err, d_wr, err_pulse, ovf, locked;
  wire [3:0] d_idx, d_be, irq_en, irq_stat;
  wire [7:0] ctrl;
  wire [DATA_W-1:0] reload, cnt;
  wire [1:0] st_w1c;
  wire [NDATA*DATA_W-1:0] data_flat;

  ahb_prot #(.ADDR_W(ADDR_W), .NDATA(NDATA)) u_prot (.HADDR(HADDR), .HWRITE(HWRITE), .HSIZE(HSIZE), .HPROT(HPROT), .err(a_err));
  ahb_if #(.ADDR_W(ADDR_W)) u_if (.HCLK(HCLK), .HRESETn(HRESETn), .HSEL(HSEL), .HADDR(HADDR), .HTRANS(HTRANS),
    .HWRITE(HWRITE), .HSIZE(HSIZE), .HREADY(HREADY), .a_err(a_err), .HREADYOUT(HREADYOUT), .HRESP(HRESP),
    .d_wr(d_wr), .d_idx(d_idx), .d_be(d_be), .err_pulse(err_pulse));
  ahb_regfile #(.DATA_W(DATA_W), .NDATA(NDATA)) u_rf (.HCLK(HCLK), .HRESETn(HRESETn), .d_wr(d_wr), .d_idx(d_idx),
    .d_be(d_be), .HWDATA(HWDATA), .err_pulse(err_pulse), .ovf(ovf), .ctrl(ctrl), .reload(reload),
    .st_w1c(st_w1c), .locked(locked), .data_flat(data_flat));
  ahb_timer #(.DATA_W(DATA_W)) u_tmr (.HCLK(HCLK), .HRESETn(HRESETn), .en(ctrl[0]), .reload(reload), .cnt(cnt), .ovf(ovf));
  ahb_irq #(.DATA_W(DATA_W)) u_irq (.HCLK(HCLK), .HRESETn(HRESETn), .en_wr(d_wr & d_idx == 4'd3),
    .stat_wr(d_wr & d_idx == 4'd4), .d_be(d_be), .HWDATA(HWDATA), .events({2'b00, err_pulse, ovf}),
    .irq_en(irq_en), .irq_stat(irq_stat), .irq(irq));
  ahb_rdmux #(.DATA_W(DATA_W), .NDATA(NDATA)) u_mux (.d_idx(d_idx), .ctrl(ctrl), .reload(reload), .cnt(cnt),
    .st_w1c(st_w1c), .locked(locked), .irq_en(irq_en), .irq_stat(irq_stat), .data_flat(data_flat), .HRDATA(HRDATA));
endmodule
