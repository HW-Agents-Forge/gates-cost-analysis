// DMA descriptor decoder: packed structs, a union view, enum states and unique case
typedef struct packed {
  logic        irq;
  logic        last;
  logic [1:0]  burst;
  logic [11:0] length;
  logic [15:0] src_hi;
} desc_t;

typedef enum logic [2:0] {S_IDLE, S_FETCH, S_READ, S_WRITE, S_DONE} state_e;

module dma_desc(input logic clk, rst, start, input logic [31:0] word, input logic ready,
                output logic [31:0] addr, output logic busy, irq, output logic [11:0] remaining);
  desc_t desc;
  state_e state;
  logic [11:0] left;
  logic [31:0] ptr;

  assign desc = word;
  assign busy = state != S_IDLE;
  assign remaining = left;
  assign addr = ptr;

  always_ff @(posedge clk) begin
    irq <= 1'b0;
    if (rst) begin
      state <= S_IDLE;
      left <= '0;
      ptr <= '0;
    end else begin
      unique case (state)
        S_IDLE:  if (start) state <= S_FETCH;
        S_FETCH: begin
          left <= desc.length;
          ptr <= {desc.src_hi, 16'h0};
          state <= S_READ;
        end
        S_READ:  if (ready) state <= S_WRITE;
        S_WRITE: begin
          ptr <= ptr + (32'd4 << desc.burst);
          left <= left - 12'd1;
          state <= (left == 12'd1) ? S_DONE : S_READ;
        end
        S_DONE: begin
          irq <= desc.irq;
          state <= desc.last ? S_IDLE : S_FETCH;
        end
        default: state <= S_IDLE;
      endcase
    end
  end
endmodule
