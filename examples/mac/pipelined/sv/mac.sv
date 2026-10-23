// Small multiply-accumulate unit: acc <= acc + a*b, with enable and synchronous clear.
module mac #(
    parameter int W   = 8,    // operand width
    parameter int ACC_W = 24    // accumulator width
) (
    input  logic           clk,
    input  logic           rst_n,
    input  logic           clear,
    input  logic           en,
    input  logic [W-1:0]   a,
    input  logic [W-1:0]   b,
    output logic [ACC_W-1:0] acc
);
    // Variant: the product is registered (pipeline stage) to shorten the critical path.
    logic [2*W-1:0] prod_q;
    always_ff @(posedge clk) if (en) prod_q <= a * b;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)      acc <= '0;
        else if (clear)  acc <= '0;
        else if (en)     acc <= acc + prod_q;
    end
endmodule
