// IRQ_EN / IRQ_STAT (write-1-to-clear) with level interrupt output.
module ahb_irq #(parameter DATA_W = 32) (
  input  wire              HCLK,
  input  wire              HRESETn,
  input  wire              en_wr,      // write strobe to IRQ_EN
  input  wire              stat_wr,    // write strobe to IRQ_STAT
  input  wire [3:0]        d_be,
  input  wire [DATA_W-1:0] HWDATA,
  input  wire [3:0]        events,
  output reg  [3:0]        irq_en,
  output reg  [3:0]        irq_stat,
  output wire              irq
);
  assign irq = |(irq_en & irq_stat);
  always @(posedge HCLK or negedge HRESETn) begin
    if (!HRESETn) begin
      irq_en <= 4'd0; irq_stat <= 4'd0;
    end else begin
      if (en_wr & d_be[0]) irq_en <= HWDATA[3:0];
      irq_stat <= (irq_stat & ~(stat_wr & d_be[0] ? HWDATA[3:0] : 4'd0)) | events;
    end
  end
endmodule
