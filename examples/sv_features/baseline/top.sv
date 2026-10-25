// Constructs that used to be mis-handled: $bits(port) in a localparam, an array parameter of structs,
// elaboration system tasks in a generate, and real-valued constant expressions.
module top #(parameter int N = 4, parameter real RATE = 25.2e6, parameter int AUDIO = 48000)
           (input logic clk, input logic [7:0] x, input logic [1:0] sel, output logic [15:0] y, output logic lock_o);
  typedef enum logic [1:0] {OFF = 0, TOR = 1, NA4 = 2} mode_e;
  typedef struct packed { logic lock; mode_e mode; logic [3:0] val; } cfg_t;
  parameter cfg_t Rst[3] = '{ '{lock: 1'b0, mode: OFF, val: 4'd1}, '{lock: 1'b1, mode: TOR, val: 4'd2},
                              '{lock: 1'b1, mode: NA4, val: 4'd9} };
  localparam int XW = $bits(x);
  localparam int CNT_W = $clog2(int'(RATE / AUDIO) + 1);
  if (N < 2) begin : g_check
    $error("N must be at least 2");
  end
  logic [CNT_W-1:0] cnt;
  always_ff @(posedge clk) cnt <= cnt + 1;
  assign y = x * Rst[sel].val + XW + cnt;
  assign lock_o = Rst[sel].lock;
endmodule
