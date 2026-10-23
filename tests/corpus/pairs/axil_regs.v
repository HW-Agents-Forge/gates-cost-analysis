// AXI4-Lite style register block: four 32-bit registers with byte strobes and a read mux
module axil_regs(input clk, input rstn,
                 input [3:0] awaddr, input awvalid, input [31:0] wdata, input [3:0] wstrb, input wvalid,
                 input [3:0] araddr, input arvalid,
                 output reg [31:0] rdata, output reg rvalid, output [31:0] ctrl, output [31:0] thresh);
  reg [31:0] r0, r1, r2, r3;
  wire wr = awvalid && wvalid;
  integer i;
  always @(posedge clk) begin
    if (!rstn) begin
      r0 <= 32'd0;
      r1 <= 32'd0;
      r2 <= 32'hFFFF;
      r3 <= 32'd0;
    end else if (wr) begin
      for (i = 0; i < 4; i = i + 1) begin
        if (wstrb[i]) begin
          case (awaddr[3:2])
            2'd0: r0[8*i +: 8] <= wdata[8*i +: 8];
            2'd1: r1[8*i +: 8] <= wdata[8*i +: 8];
            2'd2: r2[8*i +: 8] <= wdata[8*i +: 8];
            default: r3[8*i +: 8] <= wdata[8*i +: 8];
          endcase
        end
      end
    end
  end
  always @(posedge clk) begin
    if (!rstn) begin
      rdata <= 32'd0;
      rvalid <= 1'b0;
    end else begin
      rvalid <= arvalid;
      if (arvalid) begin
        case (araddr[3:2])
          2'd0: rdata <= r0;
          2'd1: rdata <= r1;
          2'd2: rdata <= r2;
          default: rdata <= r3;
        endcase
      end
    end
  end
  assign ctrl = r0;
  assign thresh = r2;
endmodule
