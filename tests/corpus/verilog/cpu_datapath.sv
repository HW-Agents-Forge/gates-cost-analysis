// Small RISC-style datapath: package of opcodes, register file, ALU function, branch compare, PC
package cpu_pkg;
  typedef enum logic [3:0] {
    ALU_ADD, ALU_SUB, ALU_AND, ALU_OR, ALU_XOR, ALU_SLL, ALU_SRL, ALU_SRA, ALU_SLT, ALU_SLTU
  } alu_op_t;
  typedef struct packed {
    logic        reg_write;
    logic        use_imm;
    logic        branch;
    logic        mem_read;
    alu_op_t     op;
  } ctrl_t;
endpackage

module cpu_datapath import cpu_pkg::*; #(parameter XLEN = 32)(
  input  logic             clk, rst,
  input  ctrl_t            ctrl,
  input  logic [4:0]       rs1, rs2, rd,
  input  logic [XLEN-1:0]  imm,
  input  logic [XLEN-1:0]  mem_rdata,
  output logic [XLEN-1:0]  mem_addr, alu_out,
  output logic [XLEN-1:0]  pc,
  output logic             taken);

  logic [XLEN-1:0] regs [0:31];
  logic [XLEN-1:0] a, b, result, wb;

  assign a = (rs1 == 5'd0) ? '0 : regs[rs1];
  assign b = ctrl.use_imm ? imm : ((rs2 == 5'd0) ? '0 : regs[rs2]);

  function automatic logic [XLEN-1:0] alu(input alu_op_t op, input logic [XLEN-1:0] x, y);
    case (op)
      ALU_ADD:  alu = x + y;
      ALU_SUB:  alu = x - y;
      ALU_AND:  alu = x & y;
      ALU_OR:   alu = x | y;
      ALU_XOR:  alu = x ^ y;
      ALU_SLL:  alu = x << y[4:0];
      ALU_SRL:  alu = x >> y[4:0];
      ALU_SRA:  alu = $signed(x) >>> y[4:0];
      ALU_SLT:  alu = {{(XLEN-1){1'b0}}, $signed(x) < $signed(y)};
      default:  alu = {{(XLEN-1){1'b0}}, x < y};
    endcase
  endfunction

  assign result = alu(ctrl.op, a, b);
  assign alu_out = result;
  assign mem_addr = result;
  assign wb = ctrl.mem_read ? mem_rdata : result;
  assign taken = ctrl.branch && (a == b);

  always_ff @(posedge clk) begin
    if (rst) pc <= '0;
    else if (taken) pc <= pc + imm;
    else pc <= pc + XLEN'(4);
  end

  always_ff @(posedge clk)
    if (ctrl.reg_write && rd != 5'd0) regs[rd] <= wb;
endmodule
