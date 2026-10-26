module ahb_rdmux #(parameter DATA_W = 32, parameter NDATA = 4) (
  input  wire [3:0]        d_idx,
  input  wire [7:0]        ctrl,
  input  wire [DATA_W-1:0] reload,
  input  wire [DATA_W-1:0] cnt,
  input  wire [1:0]        st_w1c,
  input  wire              locked,
  input  wire [3:0]        irq_en,
  input  wire [3:0]        irq_stat,
  input  wire [NDATA*DATA_W-1:0] data_flat,
  output reg  [DATA_W-1:0] HRDATA
);
  integer j;
  always @* begin
    HRDATA = {DATA_W{1'b0}};
    case (d_idx)
      4'd0: HRDATA = 32'hA4B00103;
      4'd1: HRDATA = {24'd0, ctrl};
      4'd2: HRDATA = {28'd0, st_w1c[1], locked, st_w1c[0], ctrl[0]};
      4'd3: HRDATA = {28'd0, irq_en};
      4'd4: HRDATA = {28'd0, irq_stat};
      4'd5: HRDATA = cnt;
      4'd6: HRDATA = reload;
      4'd7: HRDATA = {31'd0, locked};
      default: for (j = 0; j < NDATA; j = j + 1) if (d_idx == 4'd8 + j) HRDATA = data_flat[j*DATA_W +: DATA_W];
    endcase
  end
endmodule
