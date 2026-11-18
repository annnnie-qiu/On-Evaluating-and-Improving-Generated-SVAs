module i2c_master_top_assertions (
    input logic wb_clk_i,
    input logic wb_rst_i,
    input logic rst_i,
    input logic irq_flag,
    input logic ien,
    input logic wb_inta_o
);

  a_interrupt_request_generation: assert property (
    @(posedge wb_clk_i) disable iff (!rst_i)
    (irq_flag && ien) |-> ##1 (wb_inta_o == 1'b1)
  );

endmodule
